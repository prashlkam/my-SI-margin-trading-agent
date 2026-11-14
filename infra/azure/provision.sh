#!/usr/bin/env bash
# =============================================================================
# Provision the trading agent on Azure using FREE services only (Azure.md §2a).
#
#   App Service plan  : Linux, Free F1, 1 instance            (always free)
#   Web App           : Python 3.11, HTTPS only, TLS 1.2       (free)
#   Secrets           : App Settings (encrypted at rest)       (free; Key Vault is metered)
#   Access control    : Easy Auth + Microsoft Entra ID          (free)
#   Health check      : /api/health/live                        (free)
#   CI/CD (optional)  : GitHub OIDC federated credential        (free)
#
# Idempotent: safe to re-run. Never overwrites a setting you've changed (e.g. DRY_RUN).
#
# Usage:
#   az login
#   AZ_APP_NAME=mywebapp07 ./infra/azure/provision.sh
#
# Optional env vars (secrets are read from the environment, never printed):
#   AZ_RESOURCE_GROUP   (default rg-margin-agent)   AZ_LOCATION  (default centralindia)
#   AZ_PLAN_NAME        (default plan-margin-agent-free)
#   ENABLE_EASY_AUTH=1  (default 1)                 SETUP_GITHUB_OIDC=1 (default 0)
#   GITHUB_REPO         (default prashlkam/my-SI-margin-trading-agent)
#   DOWNGRADE_TO_FREE=1 move an existing paid plan to F1 (disables Always On first)
#   FYERS_APP_ID FYERS_SECRET_ID FYERS_PIN FYERS_REFRESH_TOKEN FYERS_ACCESS_TOKEN GEMINI_API_KEY
# =============================================================================
set -euo pipefail

RG="${AZ_RESOURCE_GROUP:-rg-margin-agent}"
LOCATION="${AZ_LOCATION:-centralindia}"
APP="${AZ_APP_NAME:?Set AZ_APP_NAME (globally unique web app name, e.g. mywebapp07)}"
PLAN="${AZ_PLAN_NAME:-plan-margin-agent-free}"
ENABLE_EASY_AUTH="${ENABLE_EASY_AUTH:-1}"
SETUP_GITHUB_OIDC="${SETUP_GITHUB_OIDC:-0}"
GITHUB_REPO="${GITHUB_REPO:-prashlkam/my-SI-margin-trading-agent}"
HOST="${APP}.azurewebsites.net"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

command -v az >/dev/null || die "Azure CLI not found: https://learn.microsoft.com/cli/azure/install-azure-cli"
az account show -o none 2>/dev/null || die "Run 'az login' first."
SUB_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"
say "Subscription $(az account show --query name -o tsv) | RG $RG | $LOCATION | app $APP"

# --- Resource group -----------------------------------------------------------
az group create -n "$RG" -l "$LOCATION" -o none

# --- App Service: reuse existing app if present, but only on the Free tier -----
if az webapp show -g "$RG" -n "$APP" -o none 2>/dev/null; then
  PLAN_ID="$(az webapp show -g "$RG" -n "$APP" --query serverFarmId -o tsv)"
  PLAN="$(basename "$PLAN_ID")"
  SKU="$(az appservice plan show --ids "$PLAN_ID" --query sku.name -o tsv)"
  say "Web app exists on plan $PLAN (SKU $SKU)"
  if [[ "$SKU" != "F1" ]]; then
    if [[ "${DOWNGRADE_TO_FREE:-0}" == "1" ]]; then
      say "Downgrading plan $PLAN from $SKU to F1 (free)"
      az webapp config set -g "$RG" -n "$APP" --always-on false -o none
      az appservice plan update --ids "$PLAN_ID" --sku F1 --number-of-workers 1 -o none
    else
      die "Plan $PLAN is $SKU (paid). Re-run with DOWNGRADE_TO_FREE=1 to move it to Free F1."
    fi
  fi
else
  say "Creating Linux Free (F1) plan $PLAN"
  az appservice plan create -g "$RG" -n "$PLAN" -l "$LOCATION" --is-linux --sku F1 --number-of-workers 1 -o none
  say "Creating web app $APP (Python 3.11)"
  az webapp create -g "$RG" -n "$APP" -p "$PLAN" --runtime "PYTHON:3.11" -o none
fi

# --- Site configuration ------------------------------------------------------
say "Configuring startup command, HTTPS-only, TLS 1.2, health check"
az webapp config set -g "$RG" -n "$APP" \
  --startup-file "bash startup.sh" \
  --min-tls-version 1.2 \
  --ftps-state Disabled \
  --http20-enabled true \
  --generic-configurations '{"healthCheckPath": "/api/health/live"}' \
  -o none
az webapp update -g "$RG" -n "$APP" --https-only true --client-affinity-enabled false -o none

# --- App settings ------------------------------------------------------------
# Defaults are set only when missing, so re-running never undoes your changes
# (e.g. flipping DRY_RUN=false after validation).
EXISTING="$(az webapp config appsettings list -g "$RG" -n "$APP" --query '[].name' -o tsv)"
has_setting() { grep -qx "$1" <<<"$EXISTING"; }

DEFAULTS=(
  "DATA_DIR=/home/site/data"
  "WEBSITES_ENABLE_APP_SERVICE_STORAGE=true"
  "SCM_DO_BUILD_DURING_DEPLOYMENT=true"
  "TZ=Asia/Kolkata"
  "AZURE_FREE_TIER=true"
  "DEFAULT_BROKER=fyers"
  "DRY_RUN=true"
  "FYERS_REDIRECT_URI=https://${HOST}/api/fyers/callback"
)
TO_SET=()
for kv in "${DEFAULTS[@]}"; do
  has_setting "${kv%%=*}" || TO_SET+=("$kv")
done
# Secrets: only from the environment, only when provided.
for name in FYERS_APP_ID FYERS_SECRET_ID FYERS_PIN FYERS_REFRESH_TOKEN FYERS_ACCESS_TOKEN GEMINI_API_KEY; do
  if [[ -n "${!name:-}" ]]; then TO_SET+=("${name}=${!name}"); fi
done
if (( ${#TO_SET[@]} )); then
  say "Setting ${#TO_SET[@]} app setting(s): $(printf '%s ' "${TO_SET[@]%%=*}")"
  az webapp config appsettings set -g "$RG" -n "$APP" --settings "${TO_SET[@]}" -o none
fi

# --- Easy Auth (Microsoft Entra ID): only you can reach the dashboard -----------
if [[ "$ENABLE_EASY_AUTH" == "1" ]]; then
  say "Configuring Easy Auth with Microsoft Entra ID"
  AUTH_APP_NAME="${APP}-easyauth"
  CLIENT_ID="$(az ad app list --display-name "$AUTH_APP_NAME" --query '[0].appId' -o tsv)"
  if [[ -z "$CLIENT_ID" ]]; then
    CLIENT_ID="$(az ad app create --display-name "$AUTH_APP_NAME" \
      --sign-in-audience AzureADMyOrg \
      --web-redirect-uris "https://${HOST}/.auth/login/aad/callback" \
      --enable-id-token-issuance true --query appId -o tsv)"
  fi
  SP_ID="$(az ad sp list --filter "appId eq '$CLIENT_ID'" --query '[0].id' -o tsv)"
  [[ -n "$SP_ID" ]] || SP_ID="$(az ad sp create --id "$CLIENT_ID" --query id -o tsv)"

  # Only explicitly assigned users may sign in; assign the signed-in user.
  az ad sp update --id "$SP_ID" --set appRoleAssignmentRequired=true -o none
  ME="$(az ad signed-in-user show --query id -o tsv)"
  if ! az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
        --query "value[?principalId=='$ME'] | [0].id" -o tsv | grep -q .; then
    az rest --method POST --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
      --body "{\"principalId\":\"$ME\",\"resourceId\":\"$SP_ID\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}" -o none
  fi

  if ! has_setting MICROSOFT_PROVIDER_AUTHENTICATION_SECRET; then
    SECRET="$(az ad app credential reset --id "$CLIENT_ID" --append --display-name easyauth --years 1 --query password -o tsv)"
    az webapp config appsettings set -g "$RG" -n "$APP" --settings "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET=$SECRET" -o none
    unset SECRET
    echo "    Easy Auth client secret created (expires in 1 year - re-run with the setting deleted to rotate)."
  fi

  AUTH_BODY="$(mktemp)"
  trap 'rm -f "$AUTH_BODY"' EXIT
  cat >"$AUTH_BODY" <<JSON
{
  "properties": {
    "platform": { "enabled": true },
    "globalValidation": {
      "requireAuthentication": true,
      "unauthenticatedClientAction": "RedirectToLoginPage",
      "redirectToProvider": "azureactivedirectory",
      "excludedPaths": ["/api/health", "/api/health/live"]
    },
    "identityProviders": {
      "azureActiveDirectory": {
        "enabled": true,
        "registration": {
          "openIdIssuer": "https://login.microsoftonline.com/${TENANT_ID}/v2.0",
          "clientId": "${CLIENT_ID}",
          "clientSecretSettingName": "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET"
        },
        "validation": { "allowedAudiences": ["api://${CLIENT_ID}", "${CLIENT_ID}"] }
      }
    },
    "login": { "tokenStore": { "enabled": true } },
    "httpSettings": { "requireHttps": true }
  }
}
JSON
  az rest --method PUT \
    --url "https://management.azure.com/subscriptions/${SUB_ID}/resourceGroups/${RG}/providers/Microsoft.Web/sites/${APP}/config/authsettingsV2?api-version=2022-03-01" \
    --body @"$AUTH_BODY" -o none
fi

# --- GitHub Actions OIDC (no publish-profile secret needed) ---------------------
if [[ "$SETUP_GITHUB_OIDC" == "1" ]]; then
  say "Configuring GitHub OIDC deploy identity for $GITHUB_REPO"
  GH_APP_NAME="${APP}-github-deploy"
  GH_CLIENT_ID="$(az ad app list --display-name "$GH_APP_NAME" --query '[0].appId' -o tsv)"
  [[ -n "$GH_CLIENT_ID" ]] || GH_CLIENT_ID="$(az ad app create --display-name "$GH_APP_NAME" --query appId -o tsv)"
  GH_SP_ID="$(az ad sp list --filter "appId eq '$GH_CLIENT_ID'" --query '[0].id' -o tsv)"
  [[ -n "$GH_SP_ID" ]] || GH_SP_ID="$(az ad sp create --id "$GH_CLIENT_ID" --query id -o tsv)"
  if ! az ad app federated-credential list --id "$GH_CLIENT_ID" --query "[?name=='github-main'] | [0].name" -o tsv | grep -q .; then
    az ad app federated-credential create --id "$GH_CLIENT_ID" --parameters "{
      \"name\": \"github-main\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"repo:${GITHUB_REPO}:ref:refs/heads/main\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }" -o none
  fi
  APP_ID="$(az webapp show -g "$RG" -n "$APP" --query id -o tsv)"
  az role assignment create --assignee-object-id "$GH_SP_ID" --assignee-principal-type ServicePrincipal \
    --role "Website Contributor" --scope "$APP_ID" -o none 2>/dev/null || true
  cat <<EOF

    Add these GitHub repository secrets (Settings > Secrets and variables > Actions):
      AZURE_CLIENT_ID       = $GH_CLIENT_ID
      AZURE_TENANT_ID       = $TENANT_ID
      AZURE_SUBSCRIPTION_ID = $SUB_ID
    e.g.  gh secret set AZURE_CLIENT_ID --body $GH_CLIENT_ID
EOF
fi

say "Done. App URL: https://${HOST}"
cat <<EOF

Next steps (Azure.md §13):
  1. GitHub repo variable for the monitor workflow:
       gh variable set AZURE_WEBAPP_URL --body https://${HOST}
  2. Deploy (push to main, or run the deploy workflow manually outside market hours).
  3. Fyers app at myapi.fyers.in: set redirect URI to https://${HOST}/api/fyers/callback
  4. Open https://${HOST}, sign in, then 'Login with Fyers'. DRY_RUN=true until validated.
  5. Portal > App Service > Quotas: watch 'CPU (Day)' during the first DRY_RUN sessions.
EOF

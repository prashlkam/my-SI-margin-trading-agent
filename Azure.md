# Azure Hosting Plan — Production Deployment

This is the complete plan to host the app on **your Azure subscription** in a way that is safe for **live, always-on intraday trading**.

Written against the current code:
- Entry point: [`main.py`](main.py) (FastAPI + background `trading_agent_loop`)
- Startup script: [`startup.sh`](startup.sh) (gunicorn, 1 worker, reads `PORT`, `DATA_DIR`)
- Dependencies: [`requirements.txt`](requirements.txt)
- Existing CI: an Azure App Service GitHub Actions workflow already exists (commit `63ee197`).

> Companion doc: **FyersAPI.md** covers the trading integration. This doc covers *where and how it runs*. The app is "100% ready for trading" only when **both** checklists are green.

---

## 0. Two things to know before you start

1. **This app must run as exactly ONE instance.** The trading loop is a singleton with in-memory state (`active_broker`, `is_running`, `credentials_cache`, simulation clock, sessions — see [`main.py`](main.py:42)) and [`startup.sh`](startup.sh) hard-codes `--workers 1`. If Azure ever runs **2 instances**, you get **two agents placing duplicate real orders**. Every decision below protects the single-instance guarantee: **Always On = yes, scale-out = 1, autoscale = off.**

2. **No Gemini code exists in the repo today.** The strategy is purely rule-based (EMA/RSI/MACD). You asked to account for a Gemini API key — this plan **provisions and secures `GEMINI_API_KEY`** (Key Vault + App Setting) so it's ready the moment AI features are wired in (see FyersAPI.md §7 for the intended use). It is configured but unused until then.

---

## 1. Recommended architecture

```
                         ┌──────────────────────────────────────────┐
                         │              Azure Subscription            │
                         │                                            │
  Browser ──HTTPS──▶  ┌──┴───────────────┐    Key Vault references    │
   (you)              │  App Service      │◀───────────────┐          │
                      │  (Linux, Python)  │                │          │
                      │  Always On = ON   │     ┌──────────┴───────┐  │
                      │  Instances = 1    │     │   Key Vault       │  │
                      │  startup.sh       │     │  FYERS-APP-ID     │  │
                      │  /home/site/data  │     │  FYERS-SECRET-ID  │  │
                      └──┬────────┬───────┘     │  FYERS-ACCESS-TOK │  │
                         │        │             │  GEMINI-API-KEY   │  │
              Outbound   │        │ logs/metrics└──────────────────┘  │
        ┌────────────────┘        ▼                                   │
        ▼                  ┌──────────────┐    ┌──────────────────┐   │
   Fyers API + WS     │ App Insights │    │  Managed Identity │   │
   Gemini API (later)      └──────────────┘    │ (App→KeyVault)   │   │
                                               └──────────────────┘   │
                         └──────────────────────────────────────────┘
```

**Core service: Azure App Service (Linux, Python 3.11).** It is the best fit because the repo already ships a `startup.sh` and `PORT`/`DATA_DIR` handling for exactly this target, it has built-in **Always On** (keeps the background trading loop alive), persistent `/home` storage, Key Vault integration via Managed Identity, and easy GitHub CD. No containerization or rewrite required.

> **Why not the alternatives**
> - *Azure Container Apps / AKS*: built to scale to many replicas — the opposite of what a singleton trading loop needs; more ops overhead for zero benefit here.
> - *Azure Functions*: serverless/stateless with execution time limits; a persistent 6.5-hour trading loop and in-memory broker state don't fit.
> - *A plain VM*: works, but you'd hand-roll TLS, deploys, restarts, patching, and log plumbing that App Service gives you free.

---

## 2. App Service Plan (SKU & sizing)

| Setting | Value | Why |
|---|---|---|
| OS | **Linux** | Matches `startup.sh`; cheaper than Windows |
| Runtime | **Python 3.11** | Matches code/SDK support |
| Plan tier | **Basic B1** (minimum) → **Standard S1** (recommended) | **Always On requires Basic+** (not Free/Shared). S1 adds staging slots, daily backups, better SLA |
| Instance count | **1 (fixed)** | Singleton trading loop — never scale out |
| Autoscale | **Disabled** | Prevents accidental multi-instance duplicate orders |
| Always On | **Enabled** | Keeps `trading_agent_loop` running when no HTTP traffic; without it the worker idles out and the agent stops |

Resource needs are modest (FastAPI + pandas/numpy, ~2 symbols). **B1 (1 core / 1.75 GB)** is enough to run; choose **S1** for staging slots + backups in production.

---

## 3. App configuration (Startup command & settings)

**Startup command** (App Service → Configuration → General settings):
```
startup.sh
```
(`startup.sh` already does `pip install -r requirements.txt` then launches gunicorn with 1 uvicorn worker on `$PORT`. App Service injects `PORT`.)

**General settings**
- Startup command: `startup.sh`
- Always On: **On**
- HTTP version: 2.0; **HTTPS Only: On**; Minimum TLS: 1.2
- ARR affinity: Off (single instance; irrelevant but cleaner)

> Note: `pip install` on every cold start (in `startup.sh`) is fine but slow. For faster, more reproducible starts, enable **Oryx build** (`SCM_DO_BUILD_DURING_DEPLOYMENT=true`) so dependencies are built at deploy time, and you can later simplify `startup.sh` to just the gunicorn line.

---

## 4. Persistent storage for trade data

The app writes JSON to `DATA_DIR`, which [`startup.sh`](startup.sh:12) sets to `/home/site/data`. On App Service Linux, **`/home` is persistent and shared across restarts** (backed by Azure Storage) when `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` (default for code deploys). So `trades.json`, `daily_metrics.json`, `users.json`, etc. survive restarts and deploys. ✅

**App settings to set:**
```
DATA_DIR = /home/site/data
WEBSITES_ENABLE_APP_SERVICE_STORAGE = true
```

**Caveats & recommendation:**
- `/home` I/O is network-backed (slightly slower); fine for this app's buffered JSON writes ([`data/storage.py`](data/storage.py)).
- JSON files are **not** built for concurrency or scale, but with a single instance and single worker that's acceptable for go-live.
- **Future hardening (not required for go-live):** migrate `trades/metrics/users` to **Azure Database for PostgreSQL Flexible Server** (or Azure Table Storage for the simplest lift). This gives durability, backups, and lets you safely add staging slots without data divergence. Track as post-launch.
- Enable **App Service Backups** (Standard+) for `/home` + a backup schedule.

---

## 5. Timezone (critical for trading)

The live trading path uses the host clock and string-compares IST market times (`09:15`/`15:25`) — see FyersAPI.md §8. App Service Linux defaults to **UTC**, which would make the agent trade at the wrong time or never.

**App setting:**
```
TZ = Asia/Kolkata
```
(Belt-and-suspenders: also make the agent timezone-explicit in code per FyersAPI.md §8.1, so correctness doesn't depend solely on the host setting.)

---

## 6. Secrets management (Fyers + Gemini)

**Never** commit secrets or store them in `users.json`. Use **Azure Key Vault + Managed Identity**.

### 6.1 Create Key Vault & secrets
Secrets to store:
| Key Vault secret | Used by | Notes |
|---|---|---|
| `FYERS-APP-ID` | Fyers connect (`client_id`) | from myapi.fyers.in |
| `FYERS-SECRET-ID` | Fyers OAuth token exchange | |
| `FYERS-REDIRECT-URI` | Fyers OAuth | `https://<app>.azurewebsites.net/api/fyers/callback` |
| `FYERS-ACCESS-TOKEN` | Fyers REST/WS | **rotated daily** by the token-refresh job (FyersAPI.md §3.4) |
| `FYERS-REFRESH-TOKEN` | Fyers re-auth | ~15-day validity |
| `GEMINI-API-KEY` | AI features (future, FyersAPI.md §7) | provisioned now, unused until wired |

### 6.2 Wire to the app (Managed Identity, no secrets in config)
1. App Service → Identity → **System-assigned: On**.
2. Key Vault → Access → grant the app's identity **Key Vault Secrets User** (RBAC) or a Get/List access policy.
3. Reference secrets as **App Settings** via Key Vault references, e.g.:
   ```
   FYERS_APP_ID       = @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/FYERS-APP-ID/)
   FYERS_SECRET_ID    = @Microsoft.KeyVault(SecretUri=.../FYERS-SECRET-ID/)
   FYERS_ACCESS_TOKEN = @Microsoft.KeyVault(SecretUri=.../FYERS-ACCESS-TOKEN/)
   GEMINI_API_KEY     = @Microsoft.KeyVault(SecretUri=.../GEMINI-API-KEY/)
   ```
   The app then reads them via `os.environ[...]` — no SDK code needed.

### 6.3 Code change to consume them
Today Fyers creds arrive only via the `/api/save-credentials` UI into in-memory `credentials_cache` ([`main.py`](main.py:49)). For unattended hosting, **also** load from env at startup so the agent can connect without a human clicking the UI after every restart:
```python
credentials_cache["fyers"] = {
    "app_id": os.environ.get("FYERS_APP_ID", ""),
    "secret_id": os.environ.get("FYERS_SECRET_ID", ""),
    "redirect_uri": os.environ.get("FYERS_REDIRECT_URI", ""),
    "access_token": os.environ.get("FYERS_ACCESS_TOKEN", ""),
}
```
Gemini key (when AI is added): `GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")`.

---

## 7. Authentication & app hardening

The app's own login is **weak** for an internet-exposed money app:
- Passwords are **SHA-256 with no salt** ([`main.py`](main.py:180)); sessions are in-memory and lost on restart ([`main.py`](main.py:76)).

For go-live, do at least one of:
- **Front it with Azure App Service "Authentication" (Easy Auth)** using Microsoft Entra ID — only you (your Entra account) can reach the app; the app's own login becomes a second layer. Lowest effort, strongest result.
- And/or restrict access by **IP allow-list / Access Restrictions** (your home/office IP) so the dashboard isn't publicly reachable.
- Harden the in-app auth later: salted hashing (bcrypt/argon2), persistent session store. Note that after every restart, in-memory sessions clear and Fyers must reconnect (handled by §6.3 + FyersAPI.md token gate).

**Always** set **HTTPS Only** and a custom domain + managed certificate if you use one.

---

## 8. Networking (outbound)

The app makes **outbound** calls to Fyers REST + WebSocket (and Gemini later). App Service has outbound internet by default — no inbound ports beyond 443 needed.
- If you later add a VNet/firewall, allow outbound 443 to Fyers (`api-t1.fyers.in`, `api.fyers.in`, socket endpoints) and Gemini (`generativelanguage.googleapis.com`).
- WebSocket support is on by default on App Service (used by Fyers data/order sockets — FyersAPI.md §4.2). Ensure **Web sockets: On** in Configuration.

---

## 9. Daily Fyers token refresh (unattended operation)

Fyers tokens expire daily (FyersAPI.md §3.4). For a hosted agent that must be ready at 09:15 IST:
- **Option A (in-app gate, simplest):** a pre-open check in the agent (~08:45 IST) runs `refresh_token` and updates `credentials_cache`/Key Vault. No extra Azure resource.
- **Option B (separate scheduler):** an **Azure Function (Timer trigger)** or **Logic App** at ~08:45 IST Mon–Fri calls the refresh, writes the new `FYERS-ACCESS-TOKEN` to Key Vault. Cleaner separation, but adds a component.

Either way, after the ~15-day refresh-token window, a human must do a full TOTP login once (visit `/api/fyers/login-url`).

---

## 10. Observability & alerts

- **Application Insights**: enable on the App Service. Captures request telemetry, exceptions, and (with the Python SDK) custom events. Add `WEBSITES_PORT` not needed; App Insights connection string via app setting.
- **Log stream / Diagnostic settings**: ship gunicorn stdout/stderr (already `--access-logfile -`/`--error-logfile -` in [`startup.sh`](startup.sh)) to a **Log Analytics workspace**.
- **Alerts** (route to email/SMS):
  - App **availability/health check** failing (configure a Health Check path, e.g. `/api/user`).
  - **Restart / crash** events (the trading loop dying mid-session is the scariest failure).
  - High exception rate (e.g. repeated Fyers auth/order errors → likely token expiry).
  - Optional custom: emit a heartbeat metric each agent tick; alert if no heartbeat during market hours.
- **Health Check** (App Service feature): set path to a lightweight endpoint; on a single instance this triggers auto-restart on hard failure.

---

## 11. CI/CD (deployment)

A GitHub Actions workflow for App Service already exists (commit `63ee197`). Finalize it:
- **Build**: Python 3.11, `pip install -r requirements.txt` (or rely on Oryx).
- **Deploy**: to the App Service (publish profile or, preferred, **OIDC federated credentials** so no publish-profile secret is stored).
- **Slots (Standard+):** deploy to a **staging slot**, smoke-test, then **swap** to production to avoid downtime. ⚠️ With a singleton trading loop, ensure the staging slot is **not** trading against the live account simultaneously — keep staging in `DRY_RUN`/mock broker, or stop the staging slot's loop. Swap **outside market hours**.
- Pin `requirements.txt` versions (incl. `fyers-apiv3` per FyersAPI.md §2) for reproducible deploys.

---

## 12. Cost estimate (rough, India/South-Central, pay-as-you-go)

| Component | SKU | Approx. monthly |
|---|---|---|
| App Service Plan | Basic B1 | ~$13–15 |
| App Service Plan | Standard S1 (recommended) | ~$70 |
| Key Vault | Standard (per-operation) | < $1 |
| Application Insights / Log Analytics | pay-per-GB ingested | ~$0–5 at this volume |
| Azure Function (optional token refresh) | Consumption | ~$0 (free grant) |
| PostgreSQL (optional, future) | Flexible B1ms | ~$15–25 |

**Go-live minimum:** ~**$15/mo** (B1 + Key Vault + minimal logs). **Recommended:** ~**$75/mo** (S1 for slots/backups). Prices are indicative — confirm in the Azure Pricing Calculator for your region.

---

## 13. Provisioning checklist (go-live)

Infrastructure
- [ ] Resource group created (e.g. `rg-margin-agent`)
- [ ] App Service Plan: **Linux, Basic B1+ (S1 recommended)**, **autoscale disabled**, **1 instance**
- [ ] Web App: Python **3.11**, startup command `startup.sh`, **Always On = On**, **Web sockets = On**, **HTTPS Only = On**
- [ ] `DATA_DIR=/home/site/data`, `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, `TZ=Asia/Kolkata`

Secrets
- [ ] Key Vault created; secrets added (Fyers app/secret/redirect/access/refresh, **GEMINI-API-KEY**)
- [ ] System-assigned Managed Identity on; Key Vault RBAC granted
- [ ] App settings use `@Microsoft.KeyVault(...)` references; verified resolving (no "Key Vault Reference" errors)
- [ ] Code loads Fyers creds (and Gemini key) from env at startup (§6.3)

Security
- [ ] Easy Auth (Entra ID) and/or IP Access Restrictions limiting who can reach the dashboard
- [ ] HTTPS only; TLS 1.2+; custom domain + cert (if used)

Trading-readiness link
- [ ] Daily Fyers token refresh in place (§9)
- [ ] **All FyersAPI.md §9 items green** (dry-run validated, kill switch, reconciliation, etc.)

Operations
- [ ] Application Insights + Log Analytics wired; Health Check path set
- [ ] Alerts: crash/restart, availability, high exception rate, market-hours heartbeat
- [ ] Backups enabled (Standard+); CI/CD deploys via staging slot, swap **outside market hours**
- [ ] Verified: only **one** instance ever runs the trading loop (no autoscale, staging not live-trading)

---

## 14. Recommended order of execution

1. Provision RG + App Service Plan (B1/S1) + Web App; deploy current code, confirm dashboard loads over HTTPS.
2. Set `DATA_DIR`, `WEBSITES_ENABLE_APP_SERVICE_STORAGE`, `TZ`, Always On, Web sockets.
3. Key Vault + Managed Identity + secret references (incl. `GEMINI-API-KEY`); code reads creds from env.
4. Lock down access (Easy Auth / IP restrictions, HTTPS only).
5. App Insights + Health Check + alerts.
6. Implement Fyers items (FyersAPI.md) + daily token refresh; run **DRY_RUN** full-day on Azure.
7. CI/CD with staging slot; swap outside market hours.
8. Small-capital live → full size once both checklists are green.

When §13 here and §9 in FyersAPI.md are both fully green, the app is **hosted and trading-ready end to end**.

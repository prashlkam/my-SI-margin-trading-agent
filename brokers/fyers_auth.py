"""
Fyers API v3 OAuth helpers (FyersAPI.md §3).

Flow:
    build_login_url()      -> user logs in at Fyers (ID + PIN + TOTP)
    Fyers redirects to     -> <redirect_uri>?s=ok&auth_code=XXXX&state=YYYY
    exchange_auth_code()   -> {"access_token": ..., "refresh_token": ...}
    refresh_access_token() -> new access_token from refresh_token + PIN (valid ~15 days)

Access tokens expire daily (~06:30 IST), so the agent re-validates the
session every morning before the market opens (see main.py pre-open gate).
"""
import hashlib
import logging
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"
REFRESH_TOKEN_URL = f"{FYERS_API_BASE}/validate-refresh-token"


def app_id_hash(app_id: str, secret_id: str) -> str:
    """SHA-256 of 'app_id:secret_id', as required by the token endpoints."""
    return hashlib.sha256(f"{app_id}:{secret_id}".encode()).hexdigest()


def _session(app_id: str, secret_id: str, redirect_uri: str, state: str = "None"):
    from fyers_apiv3 import fyersModel

    return fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
        state=state,
    )


def build_login_url(app_id: str, secret_id: str, redirect_uri: str, state: str) -> str:
    """Return the Fyers login URL. `state` is echoed back on the redirect and must be verified."""
    return _session(app_id, secret_id, redirect_uri, state).generate_authcode()


def exchange_auth_code(app_id: str, secret_id: str, redirect_uri: str, auth_code: str) -> Dict[str, Any]:
    """Exchange an auth_code for {access_token, refresh_token}. Raises RuntimeError on failure."""
    session = _session(app_id, secret_id, redirect_uri)
    session.set_token(auth_code)
    resp = session.generate_token()
    if resp.get("s") != "ok" or not resp.get("access_token"):
        raise RuntimeError(f"Fyers auth_code exchange failed: {resp.get('message') or resp}")
    return {"access_token": resp["access_token"], "refresh_token": resp.get("refresh_token", "")}


def refresh_access_token(app_id: str, secret_id: str, refresh_token: str, pin: str) -> str:
    """Mint a new access_token from a refresh_token + PIN. Raises RuntimeError on failure."""
    if not (app_id and secret_id and refresh_token and pin):
        raise RuntimeError("Fyers token refresh needs app_id, secret_id, refresh_token and PIN.")
    payload = {
        "grant_type": "refresh_token",
        "appIdHash": app_id_hash(app_id, secret_id),
        "refresh_token": refresh_token,
        "pin": str(pin),
    }
    try:
        resp = requests.post(REFRESH_TOKEN_URL, json=payload, timeout=15).json()
    except Exception as e:
        raise RuntimeError(f"Fyers token refresh request failed: {e}") from e
    if resp.get("s") != "ok" or not resp.get("access_token"):
        raise RuntimeError(f"Fyers token refresh rejected: {resp.get('message') or resp}")
    return resp["access_token"]

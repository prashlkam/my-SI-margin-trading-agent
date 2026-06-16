# Fyers API Integration Plan — Live Trading Readiness

This document is the complete plan to take the app from "Fyers stub connector" to **real money, live intraday trading** on the Fyers (FY) platform.

It is written against the current code:
- Broker connector: [`brokers/fyers.py`](brokers/fyers.py)
- Connector interface: [`brokers/base.py`](brokers/base.py)
- Orchestration: [`core/agent.py`](core/agent.py)
- App + credential wiring: [`main.py`](main.py)

> ⚠️ **Live trading places real orders with real money.** Do not point the agent at a funded Fyers account until every item in **Section 9 (Go-Live Checklist)** is green, and you have run it through Section 8 (Paper/Dry-Run).

---

## 1. Where we are today vs. what live trading needs

### What already works
- `FyersConnector` implements the full `BaseConnector` interface (`connect`, `get_quote`, `place_order`, `get_positions`, `get_historical_data`, `close_all_positions`).
- Order placement is correctly shaped for Fyers API v3: `productType=INTRADAY`, `side=1/-1`, `type=2` (market) / `1` (limit), `validity=DAY`.
- The broker is wired into the app: `/api/save-credentials` and `/api/select-broker` in [`main.py`](main.py:425) already accept `fyers` and call `fyers_broker.connect(...)`.
- Symbol formatting (`NSE:INFY-EQ`) and position normalization are implemented.

### Gaps that block real trading (addressed in this plan)
| # | Gap | Impact | Section |
|---|-----|--------|---------|
| G1 | `fyers-apiv3` is **not** in [`requirements.txt`](requirements.txt) | App can't connect in a clean deploy | 2 |
| G2 | `connect()` expects a ready-made `access_token`, but Fyers v3 tokens are **OAuth-issued and expire daily (~end of day / next login)** | Agent silently loses connection mid-day or next morning | 3 |
| G3 | `get_quote()` reads `res["read_list"]` — Fyers v3 `quotes()` returns data under key **`d`**, not `read_list` | Quotes always fail → no trading | 4 |
| G4 | Agent ticks every 1s and calls `get_positions()` + `get_quote()` per position (see [`core/agent.py`](core/agent.py:174)) | Fyers **rate limits** (~10 req/s, 200/min) will be hit; orders/quotes throttled | 5 |
| G5 | No funds/margin check before placing orders | Orders rejected for insufficient margin; agent keeps retrying | 6 |
| G6 | Order result assumed `COMPLETE` instantly ([`brokers/fyers.py`](brokers/fyers.py:83)); no fill/reject reconciliation | P&L computed on assumed fills, not actual; rejected orders treated as filled | 6 |
| G7 | Real brokers have no `get_news_and_movers`; agent falls back to **hardcoded fake news** ([`core/agent.py`](core/agent.py:101)) | Stock selection is fictional on a live account | 7 |
| G8 | Live mode uses `datetime.now()` ([`main.py`](main.py:113)); server in UTC → market-hours logic (`09:15`–`15:25`) misfires | Agent never trades, or trades at wrong times | 8 / Azure.md |
| G9 | No kill-switch, no daily loss circuit-breaker enforced against the *broker's* real P&L | Runaway losses | 6 / 9 |

---

## 2. Dependencies & environment

Add the Fyers SDK to [`requirements.txt`](requirements.txt):

```
fyers-apiv3>=3.1.0
```

> Pin the exact version once tested. The v3 SDK bundles both `fyersModel` (REST) and `FyersDataSocket` / `order_ws` (WebSocket).

Confirm Python 3.11 (the SDK supports 3.8–3.12). No system packages required.

---

## 3. Authentication — the daily OAuth token (G2, the biggest change)

Fyers API v3 does **not** hand you a permanent access token. The flow is:

```
App credentials (app_id + secret_id + redirect_uri)
        │
        ▼
  Build auth URL → user logs in (ID + PIN/password + TOTP)
        │
        ▼
  Fyers redirects to redirect_uri?auth_code=XXXX
        │
        ▼
  Exchange auth_code + appIdHash(SHA-256 of "app_id:secret_id")  → access_token
        │
        ▼
  access_token valid until ~end of trading day / next morning
```

### 3.1 One-time Fyers setup (manual, on Fyers side)
1. Create an app at **https://myapi.fyers.in** → note **App ID** (`client_id`), **Secret ID**, and set a **Redirect URI** (must exactly match what we use; for a hosted app use `https://<your-app>.azurewebsites.net/api/fyers/callback`).
2. Enable the API permissions needed (Order placement, Read data).
3. Enroll **TOTP** (required for automated login).

### 3.2 Code changes — add a token manager
Create `brokers/fyers_auth.py` (new) that wraps `fyers_apiv3.fyersModel.SessionModel`:

```python
from fyers_apiv3 import fyersModel

def build_login_url(app_id, secret_id, redirect_uri):
    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    return session.generate_authcode()

def exchange_auth_code(app_id, secret_id, redirect_uri, auth_code):
    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    resp = session.generate_token()      # -> {"access_token": "...", "refresh_token": "..."}
    return resp
```

### 3.3 New endpoints in [`main.py`](main.py)
- `GET /api/fyers/login-url` → returns the login URL for the user to click.
- `GET /api/fyers/callback?auth_code=...` → exchanges code, stores `access_token` (+ `refresh_token`) in `credentials_cache["fyers"]`, connects `fyers_broker`, and persists the token to secure storage (see 3.5).
- Keep existing `POST /api/save-credentials` for `app_id` / `secret_id` / `redirect_uri`.

`credentials_cache["fyers"]` shape becomes:
```python
"fyers": {"app_id": "", "secret_id": "", "redirect_uri": "", "access_token": "", "refresh_token": ""}
```
Update `FyersConnector.connect()` to accept `app_id` as `client_id` and the freshly minted `access_token`.

### 3.4 Daily token expiry handling (critical for an always-on agent)
- Fyers access tokens expire daily. The agent runs continuously, so it **must detect expiry and refuse to trade** rather than firing failing orders.
- Add to `FyersConnector`: every API call checks `res.get("code")`/`res.get("s")`; on auth error (`-16`, `-17`, token invalid), set `self.connected = False` and log a `CRITICAL` event.
- Add a **pre-open re-auth gate**: before 09:15 each day the agent verifies `fyers_broker.connect()` succeeds (calls `get_profile`). If not, it logs `CRITICAL: Fyers token expired — re-authenticate` and **does not enter the trading loop** for the day.
- `refresh_token` can mint a new access token for ~15 days via `validate_refresh_token` + PIN, avoiding a full re-login. Implement `FyersConnector.refresh()` and call it from the pre-open gate. After ~15 days, a full TOTP login is required again.

> Practical pattern for unattended operation: a small scheduled job (or the pre-open gate) runs the TOTP-based auto-login at ~08:45 IST daily and writes the new `access_token` to Key Vault. See Azure.md §9 (scheduled token refresh).

### 3.5 Where to store the token
- **Never** in `users.json` or git. Store in **Azure Key Vault** (see Azure.md §6) and load at connect time, or keep in `credentials_cache` (in-memory) refreshed daily by the gate.

---

## 4. Market data — fix quotes and add a WebSocket feed (G3, G4)

### 4.1 Fix the quote parser (bug)
In [`brokers/fyers.py`](brokers/fyers.py:51), Fyers v3 `quotes()` returns:
```json
{"s":"ok","d":[{"n":"NSE:INFY-EQ","v":{"lp":1520.4, ...}}]}
```
Change the parse from `res["read_list"]` to:
```python
if res.get("s") == "ok" and res.get("d"):
    return float(res["d"][0]["v"]["lp"])
```

### 4.2 Replace per-tick polling with a WebSocket (G4 — strongly recommended)
The agent currently calls `get_quote()` per symbol every second inside [`core/agent.py`](core/agent.py:189). With only ~2 traded symbols this is borderline, but `get_positions()` + quote per tick still risks the rate limit and adds latency.

Plan:
1. Add `brokers/fyers_ws.py` using `FyersDataSocket` (v3). Subscribe to the day's `traded_stocks` symbols (LTP/SymbolUpdate).
2. Maintain an in-memory `last_price: Dict[str, float]` updated by the socket callback.
3. `FyersConnector.get_quote(symbol)` returns the cached `last_price[symbol]` (falling back to a REST `quotes()` call if stale > N seconds).
4. Subscribe at 09:15 stock selection ([`core/agent.py`](core/agent.py:122)); unsubscribe at closeout.

This removes quote calls from the hot path entirely and keeps the agent within rate limits.

### 4.3 Order updates via WebSocket
Use `order_ws` (v3 order socket) to receive fills/rejections in real time and reconcile (see §6).

### 4.4 Historical data
`get_historical_data()` is fine, but Fyers limits history range per request (e.g. ~100 days for intraday resolutions). For 1–5 min candles over a few days this is OK. Add error handling for `res["s"] != "ok"` and respect rate limits (don't call it inside the per-second loop — it's only used at selection time, which is correct).

---

## 5. Rate limiting & request hygiene (G4)

Fyers limits (verify current numbers in Fyers docs at integration time): on the order of **10 requests/second**, **200/minute**, **100k/day**, with separate quotas for data vs orders.

Plan:
- Centralize all REST calls behind a `_rate_limited_call()` helper in `FyersConnector` using a token-bucket (e.g. `pyrate-limiter` or a simple timestamp deque).
- Move live prices to WebSocket (§4.2) so the per-second loop makes **zero** REST calls in steady state.
- Cache `get_positions()` — fetch once per tick max, pass the list to sub-routines instead of re-querying (the agent already mostly does this in [`core/agent.py`](core/agent.py:176)).
- Add exponential backoff on `429`/throttle responses.

---

## 6. Order execution, fills, funds & safety (G5, G6, G9)

### 6.1 Pre-trade funds/margin check (G5)
Before `place_order` in [`core/agent.py`](core/agent.py:276) `enter_position()`:
- Add `FyersConnector.get_funds()` → wraps `fyers.funds()`; read available margin.
- Skip/queue the entry if `available_margin < required_margin(qty, price)`.
- Surface available balance in `/api/state` (currently `balance` is `getattr(active_broker, "balance", 0.0)` which is `0.0` for Fyers — wire it to real funds).

### 6.2 Order reconciliation (G6 — do not trust optimistic fills)
Today `place_order` returns `{"status": "COMPLETE"}` immediately. For real trading:
- After `place_order`, capture the returned `id` (order id).
- Confirm the fill via `order_ws` callback **or** poll `fyers.orderbook()` / `fyers.tradebook()` for status `FILLED` and the **actual traded price + qty**.
- Use the **actual fill price** (not the pre-trade quote) when calling `risk_manager.register_position_open(...)` and when computing realized P&L (currently computed from quotes in [`core/agent.py`](core/agent.py:223)).
- Handle terminal states: `REJECTED`, `CANCELLED`, partial fills. On reject, log and do **not** register a position.
- Make `place_order` return `{order_id, status, filled_qty, avg_fill_price}` and adapt callers.

### 6.3 P&L source of truth
Prefer the broker's reported position P&L (`get_positions()` already maps `pl`) and realized P&L from the tradebook over the agent's internal arithmetic. Keep internal calc only as a sanity cross-check.

### 6.4 Square-off correctness
- `close_all_positions()` ([`brokers/fyers.py`](brokers/fyers.py:142)) iterates positions and places opposing market orders — acceptable. Optionally use Fyers' **`exit_positions`** API for an atomic flatten.
- Ensure the 15:25 closeout ([`core/agent.py`](core/agent.py:309)) verifies positions are actually flat afterward (re-query and retry once), because intraday (MIS) auto-square-off by the broker (~15:15–15:20) may already have closed some.

### 6.5 Kill switch & circuit breaker (G9)
- Add a hard `POST /api/kill` endpoint: sets `is_running=False`, calls `close_all_positions()`, and blocks new entries until manually re-armed.
- Enforce `RiskManager.DAILY_PROFIT_LIMIT` **and** a **daily max-loss** against real realized P&L; on breach → flatten + halt for the day.
- Add a max-consecutive-rejections breaker (e.g. 3 rejects → halt).

---

## 7. Real watchlist / stock selection (G7)

On a live account the pre-market scan falls back to **hardcoded news** ([`core/agent.py`](core/agent.py:101)). Options, in increasing effort:

1. **Static universe (fastest path to live):** Replace the fake news fallback with a fixed, liquid F&O/intraday-eligible universe (e.g. NIFTY 50 large caps) and select top-2 purely by the existing price-action score in [`core/agent.py`](core/agent.py:122) using real Fyers history. This removes fictional inputs.
2. **Real movers:** Add `get_news_and_movers()` to `FyersConnector` by computing % change across the universe from quotes/history (Fyers has no news API; compute movers yourself).
3. **External news/AI (future):** This is the natural home for the **Gemini API key** mentioned in Azure.md — feed headlines (from a news provider) to Gemini to produce sentiment scores that feed `watchlist_news`. Not required for go-live; wire later. (No Gemini code exists today.)

**Recommendation for go-live:** Option 1 — deterministic, no fake data, no external deps.

---

## 8. Timezone & paper/dry-run validation (G8)

### 8.1 Timezone
The live path uses `datetime.now()` ([`main.py`](main.py:113)) and string-compares `"09:15"`/`"15:25"`. This **must** be IST.
- Set `TZ=Asia/Kolkata` on the host (see Azure.md §5). Locally, run in IST or override.
- Better: make the agent timezone-explicit — use `datetime.now(ZoneInfo("Asia/Kolkata"))` in the live branch so it's correct regardless of host TZ. Small change in [`main.py`](main.py:111).
- Add a trading-day guard: skip weekends and NSE holidays (maintain a holiday list or a small config).

### 8.2 Dry-run mode (mandatory before real money)
Add a `DRY_RUN` flag (env var) to `FyersConnector`:
- When `DRY_RUN=true`, `place_order` logs the intended order and returns a synthetic fill **without** hitting Fyers' order endpoint, but `get_quote`/`get_positions`/funds use **real** Fyers data.
- Run a **full live session in DRY_RUN** during market hours and verify: connection holds all day, quotes flow via WebSocket, selection picks real symbols, entries/exits/closeout fire at correct IST times, no rate-limit errors, P&L math matches expected.
- Then flip a **single small-capital** real session (reduce `allocation` in [`core/agent.py`](core/agent.py:279) and `MAX_EXPOSURE`) before full size.

---

## 9. Go-Live checklist

Connection & auth
- [ ] `fyers-apiv3` added to [`requirements.txt`](requirements.txt) and installed (G1)
- [ ] App created at myapi.fyers.in; redirect URI matches deployed callback (3.1)
- [ ] OAuth login + auth_code → access_token flow working (3.2–3.3)
- [ ] Daily expiry handled: pre-open re-auth gate + refresh_token + token in Key Vault (3.4–3.5)

Data & orders
- [ ] Quote parser fixed (`res["d"]`) and verified against live quotes (G3 / 4.1)
- [ ] WebSocket price feed live; per-second loop makes 0 REST calls in steady state (4.2 / 5)
- [ ] Order reconciliation against orderbook/order_ws; actual fill price used for P&L (G6 / 6.2–6.3)
- [ ] Funds/margin check before entries; real balance in `/api/state` (G5 / 6.1)
- [ ] Square-off verified flat after 15:25; handles broker auto-squareoff (6.4)

Safety
- [ ] Kill switch endpoint + daily max-loss circuit breaker + reject breaker (G9 / 6.5)
- [ ] Real (or deterministic) watchlist — no hardcoded fake news in live mode (G7 / 7)
- [ ] Timezone forced to IST; weekend/holiday guard (G8 / 8.1)

Validation
- [ ] Full-day DRY_RUN session clean (8.2)
- [ ] Small-capital live session reviewed trade-by-trade (8.2)
- [ ] Secrets only in Key Vault; nothing in git (3.5 / Azure.md §6)

---

## 10. Suggested implementation order

1. **2 + 4.1** — add SDK, fix quote bug (smallest changes, unblocks everything).
2. **3** — OAuth token flow + endpoints (the real unlock for connecting).
3. **6.1–6.3** — funds check + order reconciliation (correctness with money).
4. **8.1** — timezone (cheap, prevents "never trades" surprise).
5. **5 + 4.2** — rate limiting / WebSocket (stability for all-day runs).
6. **6.5 + 7** — kill switch/breakers + real watchlist.
7. **3.4** — daily token refresh automation.
8. **8.2** — DRY_RUN validation → small-capital live → full size.

Once §9 is fully green, the Fyers side is **trading-ready**. Hosting is covered in Azure.md.

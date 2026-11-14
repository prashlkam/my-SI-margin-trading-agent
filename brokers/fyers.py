"""
Fyers API v3 connector — live trading (see FyersAPI.md).

Key behaviours:
  * Every REST call goes through _call(): token-bucket rate limiting,
    exponential backoff on throttling, and auth-error detection (an expired
    token flips `connected` to False so the agent stops trading).
  * Quotes come from a WebSocket LTP cache, falling back to REST when stale.
  * place_order() waits for the orderbook to report a terminal state and
    returns the ACTUAL fill: {order_id, status, filled_qty, avg_fill_price}.
  * DRY_RUN=true: real market data, but orders are filled on an in-memory
    paper book at the live price and never sent to Fyers.
"""
import collections
import datetime
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from brokers.base import BaseConnector
from brokers.fyers_ws import FyersPriceFeed, from_fyers_symbol, to_fyers_symbol

logger = logging.getLogger(__name__)

# Fyers error codes that mean the token is missing, invalid or expired.
AUTH_ERROR_CODES = {-8, -15, -16, -17, -300, -352, 401}
THROTTLE_CODES = {429, -429}

# Fyers orderbook status codes.
ORDER_STATUS = {1: "CANCELLED", 2: "FILLED", 4: "TRANSIT", 5: "REJECTED", 6: "PENDING", 7: "EXPIRED"}
TERMINAL_STATUSES = {"CANCELLED", "FILLED", "REJECTED", "EXPIRED"}

# Liquid NSE large caps scanned for movers when going live (FyersAPI.md §7).
# Max 50 symbols so the scan is a single quotes() call. Override with LIVE_UNIVERSE.
DEFAULT_UNIVERSE = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "KOTAKBANK",
    "LT", "ITC", "BHARTIARTL", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA", "TITAN",
    "ULTRACEMCO", "ASIANPAINT", "HCLTECH", "WIPRO", "NTPC", "POWERGRID", "TATASTEEL", "M&M",
    "ADANIENT", "ADANIPORTS", "JSWSTEEL", "ONGC", "COALINDIA", "BAJAJFINSV", "TECHM",
    "NESTLEIND", "HINDALCO", "GRASIM", "CIPLA", "DRREDDY", "EICHERMOT", "HEROMOTOCO",
    "BRITANNIA", "APOLLOHOSP", "INDUSINDBK", "SBILIFE", "HDFCLIFE", "TATACONSUM", "BEL",
    "TRENT", "SHRIRAMFIN", "BAJAJ-AUTO",
]


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class FyersAuthError(ConnectionError):
    pass


class RateLimiter:
    """Sliding-window limiter enforcing both a per-second and per-minute cap."""

    def __init__(self, per_second: int, per_minute: int):
        self.per_second = per_second
        self.per_minute = per_minute
        self._calls = collections.deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 60.0:
                    self._calls.popleft()
                last_second = sum(1 for t in self._calls if now - t < 1.0)
                if last_second < self.per_second and len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    return
                if len(self._calls) >= self.per_minute:
                    wait = 60.0 - (now - self._calls[0])
                else:
                    wait = 1.0 - (now - self._calls[-last_second])
            time.sleep(max(wait, 0.05))


class FyersConnector(BaseConnector):
    def __init__(self):
        self.fyers = None
        self.connected = False
        self.client_id = ""
        self.access_token = ""
        self.last_auth_error = ""
        self.dry_run = _env_flag("DRY_RUN")

        # Stay under Fyers' published 10 req/s and 200 req/min.
        self._limiter = RateLimiter(
            per_second=int(os.environ.get("FYERS_MAX_REQ_PER_SEC", 8)),
            per_minute=int(os.environ.get("FYERS_MAX_REQ_PER_MIN", 180)),
        )
        self.feed = FyersPriceFeed()
        self.quote_max_age = float(os.environ.get("FYERS_QUOTE_MAX_AGE_SEC", 5))
        self.order_fill_timeout = float(os.environ.get("FYERS_ORDER_FILL_TIMEOUT_SEC", 10))

        self._positions_cache: Optional[List[Dict[str, Any]]] = None
        self._positions_ts = 0.0
        self.positions_ttl = float(os.environ.get("FYERS_POSITIONS_TTL_SEC", 3))
        self._funds_cache: Optional[Dict[str, float]] = None
        self._funds_ts = 0.0

        # DRY_RUN paper book: symbol -> {"qty", "avg_price", "realized"}
        self._paper: Dict[str, Dict[str, float]] = {}

        universe_env = os.environ.get("LIVE_UNIVERSE", "")
        self.universe = [s.strip().upper() for s in universe_env.split(",") if s.strip()] or DEFAULT_UNIVERSE

    # ------------------------------------------------------------------ auth
    def connect(self, credentials: Dict[str, Any]) -> bool:
        """
        Connect with an already-issued token.
        Expected: {'app_id' (or 'client_id'): ..., 'access_token': ...}
        Obtain the token via the OAuth flow in brokers/fyers_auth.py.
        """
        try:
            from fyers_apiv3 import fyersModel
        except ImportError:
            logger.error("fyers-apiv3 is not installed. Run 'pip install -r requirements.txt'.")
            return False

        client_id = credentials.get("app_id") or credentials.get("client_id")
        access_token = credentials.get("access_token")
        if not client_id or not access_token:
            logger.error("Fyers connect requires 'app_id' and an 'access_token' (complete the Fyers login first).")
            return False

        try:
            self.fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
            self.client_id, self.access_token = client_id, access_token
            self.connected = True  # provisional, so _call() will run the profile check
            self._call(self.fyers.get_profile)
        except Exception as e:
            self.connected = False
            logger.error(f"Fyers profile check failed: {e}")
            return False

        self.last_auth_error = ""
        self._positions_cache = None
        self._funds_cache = None
        self.feed.start(client_id, access_token)
        logger.info(f"Authenticated with Fyers API v3{' (DRY_RUN: orders are simulated)' if self.dry_run else ''}.")
        return True

    def check_session(self) -> bool:
        """Verify the token is still valid (used by the pre-open gate)."""
        if not self.fyers or not self.access_token:
            self.connected = False
            return False
        try:
            self.connected = True
            self._call(self.fyers.get_profile)
            return True
        except Exception as e:
            logger.error(f"Fyers session check failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        self.feed.stop()
        self.connected = False

    def _mark_auth_failure(self, res: Any):
        self.connected = False
        self.last_auth_error = str(res)
        self.feed.stop()
        logger.critical(f"Fyers token rejected/expired — trading disabled until re-authentication. Response: {res}")

    def _call(self, fn: Callable, *args, **kwargs) -> Dict[str, Any]:
        """Rate-limited REST call with throttle backoff and auth-error detection."""
        if not self.connected or not self.fyers:
            raise ConnectionError("Fyers API not connected.")
        delay = 0.5
        for attempt in range(4):
            self._limiter.acquire()
            res = fn(*args, **kwargs)
            if not isinstance(res, dict):
                raise RuntimeError(f"Unexpected Fyers response: {res!r}")
            code = res.get("code")
            message = str(res.get("message", "")).lower()
            failed = res.get("s") != "ok"
            if code in THROTTLE_CODES or (failed and "request limit" in message):
                logger.warning(f"Fyers throttled request (attempt {attempt + 1}); backing off {delay:.1f}s.")
                time.sleep(delay)
                delay *= 2
                continue
            if code in AUTH_ERROR_CODES or (failed and "token" in message and ("invalid" in message or "expired" in message)):
                self._mark_auth_failure(res)
                raise FyersAuthError(f"Fyers authentication error: {res.get('message') or res}")
            if failed:
                raise RuntimeError(f"Fyers API error: {res.get('message') or res}")
            return res
        raise RuntimeError("Fyers API still throttling after retries.")

    # ------------------------------------------------------------ market data
    def get_quote(self, symbol: str) -> float:
        price = self.feed.get(symbol, self.quote_max_age)
        if price is not None:
            return price
        res = self._call(self.fyers.quotes, data={"symbols": to_fyers_symbol(symbol)})
        if res.get("d"):
            lp = res["d"][0].get("v", {}).get("lp")
            if lp:
                return float(lp)
        raise ValueError(f"Could not fetch quote for {symbol} from Fyers: {res}")

    def subscribe_symbols(self, symbols: List[str]):
        self.feed.subscribe(symbols)

    def unsubscribe_symbols(self, symbols: List[str]):
        self.feed.unsubscribe(symbols)

    def get_news_and_movers(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fyers has no news API. Movers are computed from one batched quotes() call over
        the configured universe. News stays empty — no fabricated headlines on a live account.
        """
        movers = []
        for i in range(0, len(self.universe), 50):
            chunk = self.universe[i:i + 50]
            try:
                res = self._call(self.fyers.quotes, data={"symbols": ",".join(to_fyers_symbol(s) for s in chunk)})
            except FyersAuthError:
                raise
            except Exception as e:
                logger.error(f"Fyers movers scan failed: {e}")
                continue
            for item in res.get("d", []):
                v = item.get("v", {})
                if item.get("s") == "error" or not v.get("lp"):
                    continue
                movers.append({
                    "symbol": from_fyers_symbol(item.get("n", v.get("symbol", ""))),
                    "price": float(v["lp"]),
                    "change_percent": round(float(v.get("chp", 0.0)), 2),
                })
        movers.sort(key=lambda m: abs(m["change_percent"]), reverse=True)
        return {"news": [], "movers": movers[: int(os.environ.get("LIVE_MOVERS_COUNT", 10))]}

    def get_historical_data(self, symbol: str, interval: str = "5minute", duration_days: int = 5) -> List[Dict[str, Any]]:
        resolution = {"1minute": "1", "5minute": "5", "15minute": "15", "day": "D"}.get(interval, "5")
        from core.market_time import IST, now_ist

        now = now_ist()
        data = {
            "symbol": to_fyers_symbol(symbol),
            "resolution": resolution,
            "date_format": "1",
            "range_from": (now - datetime.timedelta(days=duration_days)).strftime("%Y-%m-%d"),
            "range_to": now.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        res = self._call(self.fyers.history, data=data)
        formatted = []
        for c in res.get("candles", []):
            # [epoch, open, high, low, close, volume]
            formatted.append({
                "date": datetime.datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None).isoformat(),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": int(c[5]),
            })
        return formatted

    # ---------------------------------------------------------------- account
    def get_funds(self, max_age_seconds: float = 15.0) -> Dict[str, float]:
        """Return {'available': ..., 'total': ..., 'utilized': ...} from fyers.funds()."""
        if self._funds_cache is not None and time.monotonic() - self._funds_ts < max_age_seconds:
            return self._funds_cache
        res = self._call(self.fyers.funds)
        funds = {"available": 0.0, "total": 0.0, "utilized": 0.0}
        for row in res.get("fund_limit", []):
            title = str(row.get("title", "")).lower()
            amount = float(row.get("equityAmount", 0.0))
            if row.get("id") == 10 or title == "available balance":
                funds["available"] = amount
            elif row.get("id") == 1 or title == "total balance":
                funds["total"] = amount
            elif row.get("id") == 2 or title == "utilized amount":
                funds["utilized"] = amount
        self._funds_cache, self._funds_ts = funds, time.monotonic()
        return funds

    @property
    def balance(self) -> float:
        try:
            return self.get_funds()["available"] if self.connected else 0.0
        except Exception:
            return self._funds_cache["available"] if self._funds_cache else 0.0

    def get_positions(self) -> List[Dict[str, Any]]:
        if self.dry_run:
            return self._paper_positions()
        if self._positions_cache is not None and time.monotonic() - self._positions_ts < self.positions_ttl:
            return [dict(p) for p in self._positions_cache]

        res = self._call(self.fyers.positions)
        formatted = []
        for pos in res.get("netPositions", []):
            qty = int(pos.get("netQty", 0))
            if qty == 0:
                continue
            symbol = from_fyers_symbol(pos.get("symbol", ""))
            avg = pos.get("netAvg") or (pos.get("buyAvg") if qty > 0 else pos.get("sellAvg")) or 0.0
            ltp = self.feed.get(symbol, self.quote_max_age) or float(pos.get("ltp", 0.0))
            formatted.append({
                "symbol": symbol,
                "qty": qty,
                "avg_price": float(avg),
                "pnl": float(pos.get("unrealized_profit", pos.get("pl", 0.0))),
                "current_price": float(ltp),
            })
        self._positions_cache, self._positions_ts = formatted, time.monotonic()
        self._day_pnl = res.get("overall", {})
        return [dict(p) for p in formatted]

    def get_day_pnl(self) -> Optional[Dict[str, float]]:
        """Broker-reported day P&L (FyersAPI.md §6.3). None if unavailable."""
        if self.dry_run:
            realized = sum(p["realized"] for p in self._paper.values())
            unrealized = sum(p["pnl"] for p in self._paper_positions())
            return {"realized": realized, "unrealized": unrealized, "total": realized + unrealized}
        self.get_positions()
        overall = getattr(self, "_day_pnl", None) or {}
        if "pl_total" not in overall:
            return None
        return {
            "realized": float(overall.get("pl_realized", 0.0)),
            "unrealized": float(overall.get("pl_unrealized", 0.0)),
            "total": float(overall.get("pl_total", 0.0)),
        }

    def _invalidate(self):
        self._positions_cache = None
        self._funds_cache = None

    # ----------------------------------------------------------------- orders
    def place_order(self, symbol: str, transaction_type: str, qty: int, order_type: str = "MARKET") -> Dict[str, Any]:
        """
        Place an INTRADAY order and wait for its terminal state.
        Returns {order_id, status, filled_qty, avg_fill_price, symbol, qty, message}
        where status is one of FILLED, PARTIAL, REJECTED, CANCELLED, EXPIRED, PENDING.
        """
        if self.dry_run:
            return self._paper_fill(symbol, transaction_type, qty)

        side = 1 if transaction_type.upper() == "BUY" else -1
        data = {
            "symbol": to_fyers_symbol(symbol),
            "qty": int(qty),
            "type": 2 if order_type.upper() == "MARKET" else 1,  # 2 = market, 1 = limit
            "side": side,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            res = self._call(self.fyers.place_order, data=data)
        except FyersAuthError:
            raise
        except Exception as e:
            self._invalidate()
            return {"order_id": None, "status": "REJECTED", "filled_qty": 0, "avg_fill_price": 0.0,
                    "symbol": symbol, "qty": qty, "message": str(e)}
        order_id = res.get("id")
        logger.info(f"Fyers order sent: {transaction_type} {qty} {symbol} (id {order_id}). Awaiting fill...")
        result = self._await_fill(order_id, symbol, qty)
        self._invalidate()
        return result

    def _await_fill(self, order_id: str, symbol: str, qty: int) -> Dict[str, Any]:
        deadline = time.monotonic() + self.order_fill_timeout
        order: Dict[str, Any] = {}
        status = "PENDING"
        while True:
            try:
                res = self._call(self.fyers.orderbook, data={"id": order_id})
                book = res.get("orderBook", [])
                order = next((o for o in book if str(o.get("id")) == str(order_id)), order)
                status = ORDER_STATUS.get(order.get("status"), "PENDING")
            except FyersAuthError:
                raise
            except Exception as e:
                logger.warning(f"Orderbook poll for {order_id} failed: {e}")
            if status in TERMINAL_STATUSES or time.monotonic() >= deadline:
                break
            time.sleep(0.5)

        filled = int(order.get("filledQty", 0) or 0)
        if status == "FILLED" and filled == 0:
            filled = qty
        if status not in TERMINAL_STATUSES:
            # Never leave a stray working order behind: cancel whatever didn't fill in time.
            logger.warning(f"Order {order_id} not complete after {self.order_fill_timeout}s; cancelling remainder.")
            self.cancel_order(order_id)
            status = "PARTIAL" if filled > 0 else "CANCELLED"
        result = {
            "order_id": order_id,
            "status": status,
            "filled_qty": filled,
            "avg_fill_price": float(order.get("tradedPrice", 0.0) or 0.0),
            "symbol": symbol,
            "qty": qty,
            "message": order.get("message", ""),
        }
        log = logger.info if status == "FILLED" else logger.warning
        log(f"Fyers order {order_id} {status}: {filled}/{qty} {symbol} @ {result['avg_fill_price']} {result['message']}")
        return result

    def cancel_order(self, order_id: str):
        if self.dry_run or not order_id:
            return
        try:
            self._call(self.fyers.cancel_order, data={"id": order_id})
        except Exception as e:
            logger.error(f"Cancel of order {order_id} failed: {e}")

    def close_all_positions(self) -> List[Dict[str, Any]]:
        """Flatten everything. Uses Fyers' atomic exit_positions in live mode."""
        if self.dry_run:
            closed = []
            for symbol, p in list(self._paper.items()):
                if p["qty"]:
                    side = "SELL" if p["qty"] > 0 else "BUY"
                    closed.append(self._paper_fill(symbol, side, abs(int(p["qty"]))))
            return closed
        positions = self.get_positions()
        if not positions:
            return []
        try:
            res = self._call(self.fyers.exit_positions, data={})
            logger.info(f"Fyers exit_positions: {res.get('message', 'ok')}")
        except FyersAuthError:
            raise
        except Exception as e:
            logger.error(f"exit_positions failed ({e}); falling back to per-symbol market orders.")
            results = []
            for pos in positions:
                side = "SELL" if pos["qty"] > 0 else "BUY"
                results.append(self.place_order(pos["symbol"], side, abs(pos["qty"]), "MARKET"))
            return results
        finally:
            self._invalidate()
        return [{"symbol": p["symbol"], "qty": p["qty"], "status": "EXIT_REQUESTED"} for p in positions]

    # ------------------------------------------------------- DRY_RUN paper book
    def _paper_fill(self, symbol: str, transaction_type: str, qty: int) -> Dict[str, Any]:
        symbol = symbol.upper()
        price = self.get_quote(symbol)
        signed = qty if transaction_type.upper() == "BUY" else -qty
        p = self._paper.setdefault(symbol, {"qty": 0, "avg_price": 0.0, "realized": 0.0})
        old_qty = p["qty"]
        if old_qty == 0 or (old_qty > 0) == (signed > 0):
            new_qty = old_qty + signed
            p["avg_price"] = (p["avg_price"] * abs(old_qty) + price * qty) / abs(new_qty)
            p["qty"] = new_qty
        else:
            closing = min(abs(old_qty), qty)
            direction = 1 if old_qty > 0 else -1
            p["realized"] += (price - p["avg_price"]) * closing * direction
            p["qty"] = old_qty + signed
            if p["qty"] == 0:
                p["avg_price"] = 0.0
            elif (p["qty"] > 0) != (old_qty > 0):
                p["avg_price"] = price  # flipped through zero
        order_id = f"DRYRUN-{int(time.time() * 1000)}"
        logger.info(f"[DRY_RUN] {transaction_type} {qty} {symbol} @ {price} (not sent to Fyers, id {order_id})")
        return {"order_id": order_id, "status": "FILLED", "filled_qty": qty, "avg_fill_price": price,
                "symbol": symbol, "qty": qty, "message": "dry run"}

    def _paper_positions(self) -> List[Dict[str, Any]]:
        out = []
        for symbol, p in self._paper.items():
            if not p["qty"]:
                continue
            try:
                ltp = self.get_quote(symbol)
            except Exception:
                ltp = p["avg_price"]
            out.append({
                "symbol": symbol,
                "qty": int(p["qty"]),
                "avg_price": round(p["avg_price"], 2),
                "pnl": round((ltp - p["avg_price"]) * p["qty"], 2),
                "current_price": ltp,
            })
        return out

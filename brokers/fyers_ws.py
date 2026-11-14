"""
Live LTP cache fed by the Fyers v3 data WebSocket (FyersAPI.md §4.2).

The trading loop ticks every second; reading prices from this cache instead
of calling REST `quotes()` keeps the agent well inside Fyers' rate limits.
FyersConnector.get_quote() falls back to REST when a price is missing or stale.
"""
import logging
import threading
import time
from typing import Dict, Iterable, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def to_fyers_symbol(symbol: str) -> str:
    return symbol if ":" in symbol else f"NSE:{symbol}-EQ"


def from_fyers_symbol(instrument: str) -> str:
    sym = instrument.split(":")[-1]
    return sym[:-3] if sym.endswith("-EQ") else sym


class FyersPriceFeed:
    def __init__(self):
        self._socket = None
        self._lock = threading.Lock()
        self._prices: Dict[str, Tuple[float, float]] = {}  # symbol -> (ltp, monotonic ts)
        self._symbols: Set[str] = set()
        self.connected = False

    # ---- lifecycle -------------------------------------------------------
    def start(self, client_id: str, access_token: str):
        """Open the socket. Safe to call again after a token refresh."""
        self.stop()
        try:
            from fyers_apiv3.FyersWebsocket import data_ws

            # FyersDataSocket is a process-wide singleton; drop any old instance so a
            # fresh token takes effect.
            data_ws.FyersDataSocket._instance = None
            self._socket = data_ws.FyersDataSocket(
                access_token=f"{client_id}:{access_token}",
                log_path="",
                litemode=True,
                write_to_file=False,
                reconnect=True,
                on_connect=self._on_connect,
                on_close=self._on_close,
                on_error=self._on_error,
                on_message=self._on_message,
            )
            # connect() spawns the socket thread and returns.
            threading.Thread(target=self._socket.connect, name="fyers-data-ws", daemon=True).start()
        except Exception as e:
            logger.error(f"Fyers data WebSocket failed to start: {e}")
            self._socket = None

    def stop(self):
        if self._socket is not None:
            try:
                self._socket.close_connection()
            except Exception as e:
                logger.debug(f"Error closing Fyers data socket: {e}")
        self._socket = None
        self.connected = False

    # ---- subscriptions ---------------------------------------------------
    def subscribe(self, symbols: Iterable[str]):
        new = {s.upper() for s in symbols} - self._symbols
        self._symbols |= new
        if new and self.connected and self._socket is not None:
            self._socket.subscribe(symbols=[to_fyers_symbol(s) for s in new], data_type="SymbolUpdate")

    def unsubscribe(self, symbols: Iterable[str]):
        gone = {s.upper() for s in symbols} & self._symbols
        self._symbols -= gone
        with self._lock:
            for s in gone:
                self._prices.pop(s, None)
        if gone and self.connected and self._socket is not None:
            try:
                self._socket.unsubscribe(symbols=[to_fyers_symbol(s) for s in gone], data_type="SymbolUpdate")
            except Exception as e:
                logger.debug(f"Fyers unsubscribe error: {e}")

    def get(self, symbol: str, max_age_seconds: float) -> Optional[float]:
        with self._lock:
            entry = self._prices.get(symbol.upper())
        if entry is None or time.monotonic() - entry[1] > max_age_seconds:
            return None
        return entry[0]

    # ---- socket callbacks (run on the SDK's thread) ------------------------
    def _on_connect(self):
        self.connected = True
        logger.info("Fyers data WebSocket connected.")
        if self._symbols and self._socket is not None:
            self._socket.subscribe(symbols=[to_fyers_symbol(s) for s in self._symbols], data_type="SymbolUpdate")

    def _on_close(self, message):
        self.connected = False
        logger.warning(f"Fyers data WebSocket closed: {message}")

    def _on_error(self, message):
        logger.error(f"Fyers data WebSocket error: {message}")

    def _on_message(self, message):
        if not isinstance(message, dict):
            return
        instrument, ltp = message.get("symbol"), message.get("ltp")
        if not instrument or ltp is None:
            return
        with self._lock:
            self._prices[from_fyers_symbol(instrument).upper()] = (float(ltp), time.monotonic())

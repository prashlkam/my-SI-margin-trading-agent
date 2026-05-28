import logging
from typing import Dict, List, Any
from brokers.base import BaseConnector

logger = logging.getLogger(__name__)

class ZerodhaConnector(BaseConnector):
    def __init__(self):
        self.kite = None
        self.connected = False

    def connect(self, credentials: Dict[str, Any]) -> bool:
        """
        Connect to Zerodha Kite API.
        Expected credentials: { 'api_key': '...', 'access_token': '...' }
        """
        try:
            from kiteconnect import KiteConnect
            api_key = credentials.get("api_key")
            access_token = credentials.get("access_token")
            
            if not api_key or not access_token:
                logger.error("Zerodha API requires both 'api_key' and 'access_token'.")
                return False
                
            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)
            self.connected = True
            logger.info("Successfully authenticated with Zerodha Kite Connect.")
            return True
        except ImportError:
            logger.error("kiteconnect library is not installed. Please install it using 'pip install kiteconnect'.")
            return False
        except Exception as e:
            logger.error(f"Error connecting to Zerodha: {e}")
            return False

    def get_quote(self, symbol: str) -> float:
        if not self.connected or not self.kite:
            raise ConnectionError("Zerodha Kite not connected.")
        # Zerodha instrument format usually is e.g. NSE:INFY
        instrument = f"NSE:{symbol}" if ":" not in symbol else symbol
        quote = self.kite.ltp([instrument])
        return float(quote[instrument]["last_price"])

    def place_order(self, symbol: str, transaction_type: str, qty: int, order_type: str = "MARKET") -> Dict[str, Any]:
        if not self.connected or not self.kite:
            raise ConnectionError("Zerodha Kite not connected.")
        
        exchange = "NSE"
        trading_symbol = symbol
        if ":" in symbol:
            exchange, trading_symbol = symbol.split(":", 1)

        t_type = self.kite.TRANSACTION_TYPE_BUY if transaction_type.upper() == "BUY" else self.kite.TRANSACTION_TYPE_SELL
        o_type = self.kite.ORDER_TYPE_MARKET if order_type.upper() == "MARKET" else self.kite.ORDER_TYPE_LIMIT

        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=trading_symbol,
            transaction_type=t_type,
            quantity=qty,
            product=self.kite.PRODUCT_MIS,  # Margin Intraday Squareoff
            order_type=o_type
        )
        logger.info(f"Placed order on Zerodha: {transaction_type} {qty} {symbol}, Order ID: {order_id}")
        return {"order_id": order_id, "status": "COMPLETE", "symbol": symbol, "qty": qty}

    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.connected or not self.kite:
            raise ConnectionError("Zerodha Kite not connected.")
        
        positions = self.kite.positions()
        # Filter net positions
        net_positions = positions.get("net", [])
        formatted_positions = []
        for pos in net_positions:
            qty = pos.get("quantity", 0)
            if qty != 0:
                formatted_positions.append({
                    "symbol": pos.get("tradingsymbol"),
                    "qty": qty,
                    "avg_price": float(pos.get("average_price", 0)),
                    "pnl": float(pos.get("pnl", 0)),
                    "current_price": float(pos.get("last_price", 0))
                })
        return formatted_positions

    def get_historical_data(self, symbol: str, interval: str = "5minute", duration_days: int = 5) -> List[Dict[str, Any]]:
        if not self.connected or not self.kite:
            raise ConnectionError("Zerodha Kite not connected.")
        
        # In a real environment, we look up the instrument token
        # For simplicity, let's assume we map or query instrument token
        # Here we mock the shape returned by the API
        import datetime
        to_date = datetime.datetime.now()
        from_date = to_date - datetime.timedelta(days=duration_days)
        
        # Dummy lookup: in real usage, you use self.kite.instruments()
        # and match tradingsymbol to find the instrument token.
        # We assume 123456 as placeholder token or let it fail if not set up
        token = 123456 # Placeholder
        try:
            records = self.kite.historical_data(instrument_token=token, from_date=from_date, to_date=to_date, interval=interval)
            return [
                {
                    "date": r.get("date"),
                    "open": float(r.get("open")),
                    "high": float(r.get("high")),
                    "low": float(r.get("low")),
                    "close": float(r.get("close")),
                    "volume": int(r.get("volume", 0))
                }
                for r in records
            ]
        except Exception as e:
            logger.error(f"Error fetching historical data from Zerodha: {e}")
            return []

    def close_all_positions(self) -> List[Dict[str, Any]]:
        positions = self.get_positions()
        closed_orders = []
        for pos in positions:
            symbol = pos["symbol"]
            qty = pos["qty"]
            if qty > 0:
                # Sell to close long
                order = self.place_order(symbol, "SELL", qty, "MARKET")
                closed_orders.append(order)
            elif qty < 0:
                # Buy to close short
                order = self.place_order(symbol, "BUY", abs(qty), "MARKET")
                closed_orders.append(order)
        return closed_orders

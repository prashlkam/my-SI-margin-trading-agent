import logging
from typing import Dict, List, Any
from brokers.base import BaseConnector

logger = logging.getLogger(__name__)

class ICICIDirectConnector(BaseConnector):
    def __init__(self):
        self.breeze = None
        self.connected = False

    def connect(self, credentials: Dict[str, Any]) -> bool:
        """
        Connect to ICICI Direct Breeze API.
        Expected credentials: { 'api_key': '...', 'secret_key': '...', 'session_token': '...' }
        """
        try:
            from breeze_connect import BreezeConnect
            api_key = credentials.get("api_key")
            secret_key = credentials.get("secret_key")
            session_token = credentials.get("session_token")
            
            if not all([api_key, secret_key, session_token]):
                logger.error("ICICIDirect requires 'api_key', 'secret_key', and 'session_token'.")
                return False
                
            self.breeze = BreezeConnect(api_key=api_key)
            self.breeze.generate_session(api_secret=secret_key, session_token=session_token)
            self.connected = True
            logger.info("Successfully authenticated with ICICI Direct Breeze.")
            return True
        except ImportError:
            logger.error("breeze-connect library is not installed. Please install it using 'pip install breeze-connect'.")
            return False
        except Exception as e:
            logger.error(f"Error connecting to ICICI Direct: {e}")
            return False

    def get_quote(self, symbol: str) -> float:
        if not self.connected or not self.breeze:
            raise ConnectionError("ICICI Direct Breeze not connected.")
        # ICICI Breeze API uses stock_code, exchange_code, e.g. exchange_code="NSE", stock_code="INFY"
        res = self.breeze.get_names(exchange_code="NSE", stock_code=symbol)
        quote = self.breeze.get_quotes(stock_code=symbol, exchange_code="NSE")
        if quote.get("Status") == 200 and quote.get("Success"):
            data = quote.get("Success", [])
            if data:
                return float(data[0].get("ltp", 0.0))
        raise ValueError(f"Could not fetch quote for {symbol} from ICICI Direct.")

    def place_order(self, symbol: str, transaction_type: str, qty: int, order_type: str = "MARKET") -> Dict[str, Any]:
        if not self.connected or not self.breeze:
            raise ConnectionError("ICICI Direct Breeze not connected.")
        
        t_type = "buy" if transaction_type.upper() == "BUY" else "sell"
        o_type = "market" if order_type.upper() == "MARKET" else "limit"

        # MIS is Margin Intraday Squareoff in ICICI Direct (often represented as 'margin' product)
        order = self.breeze.place_order(
            stock_code=symbol,
            exchange_code="NSE",
            action=t_type,
            order_type=o_type,
            quantity=str(qty),
            price="0" if o_type == "market" else "limit_price_here",
            validity="day",
            product="margin"
        )
        if order.get("Status") == 200 and order.get("Success"):
            order_id = order["Success"]["order_id"]
            logger.info(f"Placed order on ICICIDirect: {transaction_type} {qty} {symbol}, Order ID: {order_id}")
            return {"order_id": order_id, "status": "COMPLETE", "symbol": symbol, "qty": qty}
        else:
            raise RuntimeError(f"Order placement failed on ICICIDirect: {order.get('Error')}")

    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.connected or not self.breeze:
            raise ConnectionError("ICICI Direct Breeze not connected.")
        
        res = self.breeze.get_portfolio_positions()
        formatted_positions = []
        if res.get("Status") == 200 and res.get("Success"):
            positions = res["Success"]
            for pos in positions:
                qty = int(pos.get("quantity", 0))
                # Breeze uses buy_sell indicator to determine sign
                action = pos.get("action", "").upper()
                signed_qty = qty if action == "BUY" else -qty
                if signed_qty != 0:
                    formatted_positions.append({
                        "symbol": pos.get("stock_code"),
                        "qty": signed_qty,
                        "avg_price": float(pos.get("average_price", 0)),
                        "pnl": float(pos.get("pnl", 0)),
                        "current_price": float(pos.get("ltp", 0))
                    })
        return formatted_positions

    def get_historical_data(self, symbol: str, interval: str = "5minute", duration_days: int = 5) -> List[Dict[str, Any]]:
        if not self.connected or not self.breeze:
            raise ConnectionError("ICICI Direct Breeze not connected.")
        
        import datetime
        to_date = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z')
        from_date = (datetime.datetime.now() - datetime.timedelta(days=duration_days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        
        # Interval mapping (ICICI Direct uses '1minute', '5minute', '30minute', '1day')
        res = self.breeze.get_historical_data_v2(
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            stock_code=symbol,
            exchange_code="NSE",
            product_type="margin"
        )
        
        formatted = []
        if res.get("Status") == 200 and res.get("Success"):
            for r in res["Success"]:
                formatted.append({
                    "date": r.get("datetime"),
                    "open": float(r.get("open")),
                    "high": float(r.get("high")),
                    "low": float(r.get("low")),
                    "close": float(r.get("close")),
                    "volume": int(r.get("volume", 0))
                })
        return formatted

    def close_all_positions(self) -> List[Dict[str, Any]]:
        positions = self.get_positions()
        closed_orders = []
        for pos in positions:
            symbol = pos["symbol"]
            qty = pos["qty"]
            if qty > 0:
                order = self.place_order(symbol, "SELL", qty, "MARKET")
                closed_orders.append(order)
            elif qty < 0:
                order = self.place_order(symbol, "BUY", abs(qty), "MARKET")
                closed_orders.append(order)
        return closed_orders

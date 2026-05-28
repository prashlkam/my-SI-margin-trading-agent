import logging
from typing import Dict, List, Any
from brokers.base import BaseConnector

logger = logging.getLogger(__name__)

class FyersConnector(BaseConnector):
    def __init__(self):
        self.fyers = None
        self.connected = False

    def connect(self, credentials: Dict[str, Any]) -> bool:
        """
        Connect to Fyers API.
        Expected credentials: { 'client_id': '...', 'access_token': '...' }
        """
        try:
            from fyers_apiv3 import fyersModel
            client_id = credentials.get("client_id")
            access_token = credentials.get("access_token")
            
            if not client_id or not access_token:
                logger.error("Fyers API requires both 'client_id' and 'access_token'.")
                return False
                
            self.fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
            # Check connection by fetching profile
            profile = self.fyers.get_profile()
            if profile.get("s") == "ok":
                self.connected = True
                logger.info("Successfully authenticated with Fyers API v3.")
                return True
            else:
                logger.error(f"Fyers profile check failed: {profile}")
                return False
        except ImportError:
            logger.error("fyers-apiv3 library is not installed. Please install it using 'pip install fyers-apiv3'.")
            return False
        except Exception as e:
            logger.error(f"Error connecting to Fyers: {e}")
            return False

    def get_quote(self, symbol: str) -> float:
        if not self.connected or not self.fyers:
            raise ConnectionError("Fyers API not connected.")
        
        # Fyers uses exchange:symbol format, e.g. NSE:INFY-EQ
        instrument = f"NSE:{symbol}-EQ" if ":" not in symbol else symbol
        data = {"symbols": instrument}
        res = self.fyers.quotes(data=data)
        if res.get("s") == "ok" and res.get("read_list"):
            quote_data = res["read_list"][0]
            return float(quote_data.get("v", {}).get("lp", 0.0)) # lp is Last Traded Price
        raise ValueError(f"Could not fetch quote for {symbol} from Fyers: {res}")

    def place_order(self, symbol: str, transaction_type: str, qty: int, order_type: str = "MARKET") -> Dict[str, Any]:
        if not self.connected or not self.fyers:
            raise ConnectionError("Fyers API not connected.")
        
        instrument = f"NSE:{symbol}-EQ" if ":" not in symbol else symbol
        side = 1 if transaction_type.upper() == "BUY" else -1
        
        # Fyers Order Type: 1 = Limit, 2 = Market
        o_type = 2 if order_type.upper() == "MARKET" else 1

        data = {
            "symbol": instrument,
            "qty": qty,
            "type": o_type,
            "side": side,
            "productType": "INTRADAY", # Intraday margin trading
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False
        }
        
        res = self.fyers.place_order(data=data)
        if res.get("s") == "ok":
            order_id = res.get("id")
            logger.info(f"Placed order on Fyers: {transaction_type} {qty} {symbol}, Order ID: {order_id}")
            return {"order_id": order_id, "status": "COMPLETE", "symbol": symbol, "qty": qty}
        else:
            raise RuntimeError(f"Order placement failed on Fyers: {res}")

    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.connected or not self.fyers:
            raise ConnectionError("Fyers API not connected.")
        
        res = self.fyers.positions()
        formatted_positions = []
        if res.get("s") == "ok" and "netPositions" in res:
            for pos in res["netPositions"]:
                qty = int(pos.get("netQty", 0))
                if qty != 0:
                    formatted_positions.append({
                        "symbol": pos.get("symbol").split(":")[-1].replace("-EQ", ""),
                        "qty": qty,
                        "avg_price": float(pos.get("buyAvg", 0.0) if qty > 0 else pos.get("sellAvg", 0.0)),
                        "pnl": float(pos.get("pl", 0.0)),
                        "current_price": float(pos.get("ltp", 0.0))
                    })
        return formatted_positions

    def get_historical_data(self, symbol: str, interval: str = "5minute", duration_days: int = 5) -> List[Dict[str, Any]]:
        if not self.connected or not self.fyers:
            raise ConnectionError("Fyers API not connected.")
        
        # Fyers uses resolution parameter: '1', '5', '15', '30', 'D' etc.
        resolution = "5" if interval == "5minute" else "1"
        
        import datetime
        to_date = datetime.datetime.now().strftime('%Y-%m-%d')
        from_date = (datetime.datetime.now() - datetime.timedelta(days=duration_days)).strftime('%Y-%m-%d')
        
        instrument = f"NSE:{symbol}-EQ" if ":" not in symbol else symbol
        data = {
            "symbol": instrument,
            "resolution": resolution,
            "date_format": "1",
            "range_from": from_date,
            "range_to": to_date,
            "cont_flag": "1"
        }
        
        res = self.fyers.history(data=data)
        formatted = []
        if res.get("s") == "ok" and "candles" in res:
            for c in res["candles"]:
                # candle format: [timestamp, open, high, low, close, volume]
                formatted.append({
                    "date": datetime.datetime.fromtimestamp(c[0]).isoformat(),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": int(c[5])
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

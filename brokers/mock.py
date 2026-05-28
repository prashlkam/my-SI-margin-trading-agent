import logging
import random
from typing import Dict, List, Any
import datetime
from brokers.base import BaseConnector

logger = logging.getLogger(__name__)

# List of mock stocks
MOCK_STOCKS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "TATAMOTORS", "ITC", "BHARTIARTL", "L&T"]

class MockBrokerConnector(BaseConnector):
    def __init__(self):
        self.connected = False
        self.balance = 100000.0  # Virtual capital
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.current_prices: Dict[str, float] = {
            "RELIANCE": 2450.0,
            "TCS": 3200.0,
            "INFY": 1420.0,
            "HDFCBANK": 1550.0,
            "ICICIBANK": 920.0,
            "SBIN": 580.0,
            "TATAMOTORS": 610.0,
            "ITC": 430.0,
            "BHARTIARTL": 880.0,
            "L&T": 2350.0
        }
        # Base prices for mean reversion — prevents prices from drifting to infinity
        self._base_prices: Dict[str, float] = dict(self.current_prices)
        self.stock_trends: Dict[str, float] = {}  # Drift factor for random walk
        self.virtual_time = datetime.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        self.speed_multiplier = 1.0 # default normal speed
        self._initialize_trends()

    def _initialize_trends(self):
        # Set a random trend drift for each stock (positive or negative)
        for stock in MOCK_STOCKS:
            self.stock_trends[stock] = random.choice([-0.0005, -0.0002, 0.0001, 0.0003, 0.0006])

    def connect(self, credentials: Dict[str, Any]) -> bool:
        self.connected = True
        logger.info("Successfully connected to Mock Broker Simulator.")
        return True

    def set_speed(self, multiplier: float):
        self.speed_multiplier = multiplier

    def update_prices(self, seconds_elapsed: float):
        # Tick the virtual time
        scaled_seconds = seconds_elapsed * self.speed_multiplier
        self.virtual_time += datetime.timedelta(seconds=scaled_seconds)

        # Update stock prices using a random walk with trend drift AND mean reversion
        # Mean reversion pulls prices back toward their initial base price,
        # preventing astronomical inflation over multi-month simulations.
        mean_reversion_strength = 0.0005  # strength of pull toward base price per tick
        for stock in MOCK_STOCKS:
            drift = self.stock_trends[stock]
            volatility = 0.0015  # standard volatility
            change = drift + random.normalvariate(0, volatility)
            
            # Mean reversion: pull price toward base price
            base = self._base_prices[stock]
            current = self.current_prices[stock]
            reversion = (base - current) / base * mean_reversion_strength
            
            self.current_prices[stock] = max(1.0, round(current * (1 + change + reversion), 2))

            # Update position pnl
            if stock in self.positions:
                pos = self.positions[stock]
                qty = pos["qty"]
                avg_price = pos["avg_price"]
                current_price = self.current_prices[stock]
                pos["current_price"] = current_price
                if qty > 0:
                    pos["pnl"] = round((current_price - avg_price) * qty, 2)
                else:
                    pos["pnl"] = round((avg_price - current_price) * abs(qty), 2)

    def get_quote(self, symbol: str) -> float:
        # Tick 1 second automatically on quote check
        self.update_prices(1.0)
        symbol_upper = symbol.upper()
        if symbol_upper in self.current_prices:
            return self.current_prices[symbol_upper]
        return 100.0

    def get_news_and_movers(self) -> Dict[str, List[Dict[str, Any]]]:
        """Simulate pre-market news and movers at 9:05 AM."""
        news = [
            {"symbol": "INFY", "headline": "Infosys wins massive $1.5B cloud transformation deal", "sentiment": 0.8},
            {"symbol": "RELIANCE", "headline": "Reliance Retail expands partnership with international brands", "sentiment": 0.5},
            {"symbol": "TATAMOTORS", "headline": "Tata Motors EV division reports 40% YoY growth in sales", "sentiment": 0.7},
            {"symbol": "TCS", "headline": "TCS earnings report slightly misses street margins expectation", "sentiment": -0.3},
            {"symbol": "HDFCBANK", "headline": "RBI approves new branches expansion strategy for HDFC", "sentiment": 0.4},
            {"symbol": "ICICIBANK", "headline": "ICICI Bank launches premium business suite for SMEs", "sentiment": 0.3},
            {"symbol": "SBIN", "headline": "State Bank of India NPA levels decrease to record lows", "sentiment": 0.6},
            {"symbol": "ITC", "headline": "ITC hotel business demerger scheme approved by shareholders", "sentiment": 0.5}
        ]
        
        # Select 5 random news items
        selected_news = random.sample(news, 5)
        
        # Create 5 top movers with initial % changes
        movers = []
        stocks = list(self.current_prices.keys())
        random.shuffle(stocks)
        for s in stocks[:5]:
            change = round(random.uniform(-4.0, 4.0), 2)
            movers.append({
                "symbol": s,
                "price": self.current_prices[s],
                "change_percent": change
            })
            
        # Add high drift to news/movers to make them volatile for simulation
        for item in selected_news:
            s = item["symbol"]
            self.stock_trends[s] = item["sentiment"] * 0.0012  # boost trend based on news sentiment
            
        for item in movers:
            s = item["symbol"]
            if s not in [n["symbol"] for n in selected_news]:
                self.stock_trends[s] += (item["change_percent"] / 100.0) * 0.005
                
        return {"news": selected_news, "movers": sorted(movers, key=lambda x: abs(x["change_percent"]), reverse=True)}

    def place_order(self, symbol: str, transaction_type: str, qty: int, order_type: str = "MARKET") -> Dict[str, Any]:
        self.update_prices(0.5) # Simulating slight execution latency
        symbol_upper = symbol.upper()
        price = self.current_prices.get(symbol_upper, 100.0)
        
        # Check exposure limit or capital availability
        cost = price * qty
        
        if transaction_type.upper() == "BUY":
            if symbol_upper in self.positions:
                pos = self.positions[symbol_upper]
                # If existing is SHORT (negative qty), reduce it
                if pos["qty"] < 0:
                    pos["qty"] += qty
                    # update avg price if closing/partially closing
                    if pos["qty"] == 0:
                        del self.positions[symbol_upper]
                else:
                    # accumulate long
                    pos["qty"] += qty
                    pos["avg_price"] = round(((pos["avg_price"] * (pos["qty"] - qty)) + cost) / pos["qty"], 2)
            else:
                self.positions[symbol_upper] = {
                    "symbol": symbol_upper,
                    "qty": qty,
                    "avg_price": price,
                    "current_price": price,
                    "pnl": 0.0
                }
        else: # SELL order (entering short or closing long)
            if symbol_upper in self.positions:
                pos = self.positions[symbol_upper]
                if pos["qty"] > 0:
                    pos["qty"] -= qty
                    if pos["qty"] == 0:
                        del self.positions[symbol_upper]
                else:
                    # accumulate short
                    pos["qty"] -= qty
                    pos["avg_price"] = round(((pos["avg_price"] * (abs(pos["qty"]) - qty)) + cost) / abs(pos["qty"]), 2)
            else:
                self.positions[symbol_upper] = {
                    "symbol": symbol_upper,
                    "qty": -qty,
                    "avg_price": price,
                    "current_price": price,
                    "pnl": 0.0
                }

        order_id = f"MOCK-ORD-{random.randint(100000, 999999)}"
        logger.info(f"Mock Order {order_id} filled: {transaction_type} {qty} shares of {symbol} at Rs. {price}")
        return {
            "order_id": order_id,
            "status": "COMPLETE",
            "symbol": symbol_upper,
            "qty": qty,
            "price": price,
            "timestamp": self.virtual_time.isoformat()
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        self.update_prices(0.1)
        return list(self.positions.values())

    def get_historical_data(self, symbol: str, interval: str = "5minute", duration_days: int = 5) -> List[Dict[str, Any]]:
        # Return a simulated list of historical candles with some volatility
        symbol_upper = symbol.upper()
        base_price = self.current_prices.get(symbol_upper, 100.0)
        
        import datetime
        now = self.virtual_time
        candles = []
        
        # Make a realistic-looking back history of 50 intervals
        for i in range(50, 0, -1):
            time_offset = datetime.timedelta(minutes=5 * i)
            candle_time = now - time_offset
            
            # Simple walk backwards
            noise = random.uniform(-10.0, 10.0)
            c_open = base_price + noise
            c_close = c_open + random.uniform(-5.0, 5.0)
            c_high = max(c_open, c_close) + random.uniform(0.0, 3.0)
            c_low = min(c_open, c_close) - random.uniform(0.0, 3.0)
            
            candles.append({
                "date": candle_time.isoformat(),
                "open": round(c_open, 2),
                "high": round(c_high, 2),
                "low": round(c_low, 2),
                "close": round(c_close, 2),
                "volume": random.randint(1000, 50000)
            })
        return candles

    def close_all_positions(self) -> List[Dict[str, Any]]:
        closed_orders = []
        active_symbols = list(self.positions.keys())
        for symbol in active_symbols:
            pos = self.positions[symbol]
            qty = pos["qty"]
            if qty > 0:
                order = self.place_order(symbol, "SELL", qty, "MARKET")
                closed_orders.append(order)
            elif qty < 0:
                order = self.place_order(symbol, "BUY", abs(qty), "MARKET")
                closed_orders.append(order)
        self.positions.clear()
        return closed_orders

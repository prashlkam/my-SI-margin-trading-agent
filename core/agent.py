import logging
import datetime
from typing import Dict, List, Any, Optional
from core.strategy import TradingStrategy
from core.risk_manager import RiskManager
from core.optimizer import StrategyOptimizer
from data.storage import DataStorage
from brokers.base import BaseConnector

logger = logging.getLogger(__name__)

class MarginTradingAgent:
    def __init__(self, broker: BaseConnector, storage: DataStorage, config_dir: str):
        self.broker = broker
        self.storage = storage
        self.config_dir = config_dir
        self.last_date = None
        
        # Risk & Strategy setup
        self.optimizer = StrategyOptimizer(config_dir)
        self.strategy_params = self.optimizer.load_optimized_params()
        self.strategy = TradingStrategy(self.strategy_params)
        self.risk_manager = RiskManager()
        
        # Watchlist and status variables
        self.pre_market_scanned = False
        self.market_opened = False
        self.market_closed = False
        
        self.watchlist_news: List[Dict[str, Any]] = []
        self.watchlist_movers: List[Dict[str, Any]] = []
        self.traded_stocks: List[str] = [] # The 2 stocks selected at 9:15 AM
        
        # Cache for historical data of traded stocks to run optimization at 3:25 PM
        self.daily_price_data: Dict[str, List[Dict[str, Any]]] = {}

        # Log system status
        self.log("Margin Trading Agent Initialised in Standby Mode.")

    def log(self, message: str, level: str = "INFO", timestamp: Optional[datetime.datetime] = None):
        logger.info(message)
        if timestamp is None:
            if hasattr(self.broker, "virtual_time"):
                timestamp = self.broker.virtual_time
            else:
                timestamp = datetime.datetime.now()
        self.storage.save_log_entry(message, level, timestamp)

    def set_broker(self, broker: BaseConnector):
        self.broker = broker
        self.log(f"Switched broker connector to {broker.__class__.__name__}.")

    def reload_strategy_params(self):
        self.strategy_params = self.optimizer.load_optimized_params()
        self.strategy.update_params(self.strategy_params)
        self.log("Strategy parameters reloaded.")

    def tick(self, current_time: datetime.datetime):
        """
        Main execution tick called periodically by the backend runner.
        Controls pre-market scans, stock selection, live trading, and closeout.
        """
        # Check if day has changed to support multi-day backtesting/simulation
        current_date = current_time.date()
        if self.last_date is not None and current_date != self.last_date:
            self.log(f"New trading day detected: {current_date.strftime('%Y-%m-%d')}. Resetting daily states.", "INFO", current_time)
            self.pre_market_scanned = False
            self.market_opened = False
            self.market_closed = False
            self.traded_stocks = []
            self.daily_price_data = {}
            self.risk_manager.reset_daily()
        self.last_date = current_date

        # Formatted string for logs
        time_str = current_time.strftime("%H:%M")
        
        # 1. Pre-Market Scan: 10 minutes before market open (9:05 AM)
        if time_str >= "09:05" and not self.pre_market_scanned:
            self.run_pre_market_scan()
            
        # 2. Market Open selection: 9:15 AM
        if time_str >= "09:15" and not self.market_opened:
            self.run_market_open_selection()

        # 3. Live Intraday Trading Loop: 9:15 AM - 3:25 PM
        if self.market_opened and not self.market_closed:
            if time_str < "15:25":
                self.run_live_trading(current_time)
            else:
                self.run_daily_closeout(current_time)

    def run_pre_market_scan(self):
        self.log("Running Pre-Market scan (9:05 AM)...")
        # For mock broker, we get simulated news and movers.
        # For real brokers, we query sector performance or APIs.
        if hasattr(self.broker, "get_news_and_movers"):
            res = self.broker.get_news_and_movers()
            self.watchlist_news = res.get("news", [])
            self.watchlist_movers = res.get("movers", [])
        else:
            # Fallback for live brokers (mock news and movers if real API doesn't support them)
            self.watchlist_news = [
                {"symbol": "INFY", "headline": "Infosys wins massive cloud deal", "sentiment": 0.7},
                {"symbol": "RELIANCE", "headline": "Reliance green energy trial starts", "sentiment": 0.4},
                {"symbol": "TCS", "headline": "TCS sets up AI center of excellence", "sentiment": 0.5},
                {"symbol": "TATAMOTORS", "headline": "Tata Motors launches new commercial EV line", "sentiment": 0.6},
                {"symbol": "HDFCBANK", "headline": "HDFC Bank Q4 profits spike 15%", "sentiment": 0.6}
            ]
            self.watchlist_movers = [
                {"symbol": "INFY", "price": 1420.0, "change_percent": 2.4},
                {"symbol": "TATAMOTORS", "price": 610.0, "change_percent": 1.9},
                {"symbol": "RELIANCE", "price": 2450.0, "change_percent": 1.2},
                {"symbol": "SBIN", "price": 580.0, "change_percent": -1.1},
                {"symbol": "TCS", "price": 3200.0, "change_percent": -0.8}
            ]
        
        self.log(f"Pre-Market Watchlist created. News stocks: {[x['symbol'] for x in self.watchlist_news]}")
        self.log(f"Top Movers: {[x['symbol'] + ' (' + str(x['change_percent']) + '%)' for x in self.watchlist_movers]}")
        self.pre_market_scanned = True

    def run_market_open_selection(self):
        self.log("Market Open! Selecting top 2 stocks for day trading based on price action (9:15 AM)...")
        
        # Candidate pool: Union of top news and top movers symbols
        candidates = list(set([x["symbol"] for x in self.watchlist_news] + [x["symbol"] for x in self.watchlist_movers]))
        if not candidates:
            # Fallback
            candidates = ["INFY", "RELIANCE", "TATAMOTORS", "TCS", "HDFCBANK"]
            
        scores = []
        for symbol in candidates:
            try:
                # Retrieve initial quotes or brief historical trend
                candles = self.broker.get_historical_data(symbol, interval="5minute", duration_days=2)
                if len(candles) >= 5:
                    # Score price action: volatility + direction (absolute price change slope)
                    closes = [float(c["close"]) for c in candles[-5:]]
                    volatility = max(closes) - min(closes)
                    base_price = closes[0] if closes[0] != 0 else 1.0  # prevent division by zero
                    trend = (closes[-1] - closes[0]) / base_price
                    # Score is volatility scaled by positive or negative strength
                    score = abs(trend) * volatility
                    scores.append((symbol, score, candles))
            except Exception as e:
                logger.error(f"Failed scoring candidate {symbol}: {e}")
                
        # Sort candidates by price action score descending
        scores.sort(key=lambda x: x[1], reverse=True)
        
        # Pick top 2
        selected = [item[0] for item in scores[:2]]
        if len(selected) < 2:
            # Hardcoded fallbacks if we couldn't score enough
            fallback_options = ["RELIANCE", "INFY", "TATAMOTORS"]
            for f in fallback_options:
                if f not in selected:
                    selected.append(f)
                if len(selected) == 2:
                    break
                    
        self.traded_stocks = selected
        
        # Cache historical data for optimization
        for item in scores:
            if item[0] in self.traded_stocks:
                self.daily_price_data[item[0]] = item[2]
                
        self.log(f"Selected Top 2 Stocks for the day: {self.traded_stocks[0]} and {self.traded_stocks[1]}")
        self.risk_manager.reset_daily()
        self.market_opened = True
        self.market_closed = False

    def run_live_trading(self, current_time: datetime.datetime):
        # 1. First, check daily profit cap limit (Rs. 12,000)
        positions = self.broker.get_positions()
        unrealized_pnl = sum([pos["pnl"] for pos in positions])
        
        if self.risk_manager.check_daily_profit_limit(unrealized_pnl):
            self.log(f"Daily profit target exceeded Rs. {self.risk_manager.DAILY_PROFIT_LIMIT:.2f}! Closing all positions.", "WARNING")
            self.run_daily_closeout(current_time)
            return

        # 2. Monitor existing positions for SL, TP, or Trend Reversal
        for pos in positions:
            symbol = pos["symbol"]
            qty = pos["qty"]
            avg_price = pos["avg_price"]
            current_price = self.broker.get_quote(symbol) # also ticks prices in mock broker
            
            # Update peak/valley in risk manager and check SL/TP triggers
            trigger = self.risk_manager.update_and_check_signals(symbol, current_price)
            
            # Check strategy for manual trend reversal exit
            trend_reversal = False
            if symbol in self.daily_price_data:
                # Add current price candle
                self.daily_price_data[symbol].append({
                    "date": current_time.isoformat(),
                    "open": current_price,
                    "high": current_price,
                    "low": current_price,
                    "close": current_price,
                    "volume": 0
                })
                # Check reversal
                direction = self.risk_manager.position_directions.get(symbol, "LONG")
                trend_reversal = self.strategy.check_trend_reversal(direction, self.daily_price_data[symbol])

            # Process exit if stop loss, take profit or trend reversal triggered
            if trigger in ["SL", "TP"] or trend_reversal:
                exit_reason = "Trailing Stop Loss" if trigger == "SL" else "Take Profit"
                if trend_reversal:
                    exit_reason = "Trend Reversal Signal"
                
                self.log(f"EXIT TRIGGER: {exit_reason} for {symbol} at Rs. {current_price}", "WARNING")
                
                # Execute broker close
                order_type = "SELL" if qty > 0 else "BUY"
                self.broker.place_order(symbol, order_type, abs(qty), "MARKET")
                
                # Calculate final trade profit
                realized = (current_price - avg_price) * qty if qty > 0 else (avg_price - current_price) * abs(qty)
                self.risk_manager.register_position_close(symbol, realized)
                
                # Save to database log
                self.storage.save_trade({
                    "symbol": symbol,
                    "direction": "LONG" if qty > 0 else "SHORT",
                    "entry_price": avg_price,
                    "exit_price": current_price,
                    "qty": abs(qty),
                    "pnl": realized,
                    "exit_reason": exit_reason,
                    "timestamp": current_time.isoformat()
                })
                
                # Update dashboard metrics
                self.update_summary_metrics()

                # 3. CONTRA POSITION TRIGGER:
                # "in case of loss or trend reversal - close the active position and take a contra position to get back into profits"
                # If we closed because of SL or Trend Reversal, take the opposite trade immediately.
                if (trigger == "SL" or trend_reversal) and not self.risk_manager.is_trade_limit_reached():
                    contra_direction = "SHORT" if qty > 0 else "LONG"
                    self.log(f"Initiating CONTRA position ({contra_direction}) on {symbol} to recover losses.", "IMPORTANT")
                    self.enter_position(symbol, contra_direction, current_price, current_time)

        # 4. Check for new entries on watchlist if trade limit not reached
        if not self.risk_manager.is_trade_limit_reached():
            active_symbols = [pos["symbol"] for pos in positions]
            
            for symbol in self.traded_stocks:
                if symbol not in active_symbols:
                    # Retrieve latest price and run analysis
                    current_price = self.broker.get_quote(symbol)
                    
                    # Accumulate today's candles
                    if symbol in self.daily_price_data:
                        candles = self.daily_price_data[symbol]
                    else:
                        candles = self.broker.get_historical_data(symbol, interval="5minute", duration_days=1)
                        self.daily_price_data[symbol] = candles
                        
                    analysis = self.strategy.analyze(candles)
                    score = analysis["signal"]
                    threshold = self.strategy_params["entry_threshold"]
                    
                    if score >= threshold:
                        # Bullish entry -> LONG
                        self.enter_position(symbol, "LONG", current_price, current_time)
                    elif score <= -threshold:
                        # Bearish entry -> SHORT
                        self.enter_position(symbol, "SHORT", current_price, current_time)

    def enter_position(self, symbol: str, direction: str, price: float, current_time: datetime.datetime):
        # Calculate quantity based on exposure limit (max Rs. 15,000 per stock to keep total exposure under Rs. 30,000)
        # Position sizing: quantity = allocation / price
        allocation = 14500.0  # Rs. 14,500 exposure per trade
        qty = int(allocation / price)
        if qty <= 0:
            qty = 1
            
        # Verify exposure limit
        positions = self.broker.get_positions()
        if not self.risk_manager.check_exposure_limit(symbol, price, qty, positions):
            self.log(f"Order skipped: Exposure would exceed Rs. 30,000 threshold.", "WARNING")
            return
            
        # Place order
        order_type = "BUY" if direction == "LONG" else "SELL"
        try:
            self.broker.place_order(symbol, order_type, qty, "MARKET")
            # Calculate recent volatility from daily price data for dynamic TP scaling
            vol = 0.0
            if symbol in self.daily_price_data and len(self.daily_price_data[symbol]) >= 5:
                closes = [c["close"] for c in self.daily_price_data[symbol][-5:]]
                if isinstance(closes[0], str):
                    closes = [float(c) for c in closes]
                avg = sum(closes) / len(closes)
                mean_price = avg if avg != 0 else 1.0  # prevent division by zero
                variances = [abs(c - mean_price) / mean_price for c in closes]
                vol = sum(variances) / len(variances) * 100.0  # as percentage
            self.risk_manager.register_position_open(symbol, direction, price, volatility_pct=vol)
            self.log(f"Opened {direction} position: {qty} shares of {symbol} at Rs. {price}", "IMPORTANT")
        except Exception as e:
            self.log(f"Failed to place entry order for {symbol}: {e}", "ERROR")

    def run_daily_closeout(self, current_time: datetime.datetime):
        self.log(f"Closing open positions for daily square-off (3:25 PM / Target profit cap hit)...")
        
        # Get active positions to log and close
        positions = self.broker.get_positions()
        for pos in positions:
            symbol = pos["symbol"]
            qty = pos["qty"]
            avg_price = pos["avg_price"]
            current_price = self.broker.get_quote(symbol)
            
            # Place order to close
            order_type = "SELL" if qty > 0 else "BUY"
            try:
                self.broker.place_order(symbol, order_type, abs(qty), "MARKET")
                pnl = (current_price - avg_price) * qty if qty > 0 else (avg_price - current_price) * abs(qty)
                self.risk_manager.register_position_close(symbol, pnl)
                
                # Save trade log
                self.storage.save_trade({
                    "symbol": symbol,
                    "direction": "LONG" if qty > 0 else "SHORT",
                    "entry_price": avg_price,
                    "exit_price": current_price,
                    "qty": abs(qty),
                    "pnl": pnl,
                    "exit_reason": "End-of-day Squareoff" if current_time.strftime("%H:%M") >= "15:25" else "Daily Profit Cap Hit",
                    "timestamp": current_time.isoformat()
                })
            except Exception as e:
                self.log(f"Error squaring off {symbol}: {e}", "ERROR")

        # Clear active flags
        self.broker.close_all_positions() # safety back-up
        self.risk_manager.peak_prices.clear()
        self.risk_manager.position_directions.clear()
        
        self.market_closed = True
        self.market_opened = False
        self.update_summary_metrics()
        self.log("Daily trade operations completed. Positions squared off.")
        
        # 5. Nightly Self-Improvement optimization
        self.log("Running self-improvement engine: Optimising parameters for next session...")
        trades = self.storage.get_trades()
        optimized = self.optimizer.run_optimization(trades, self.daily_price_data)
        self.strategy_params = optimized
        self.strategy.update_params(optimized)
        self.log(f"Self-improvement complete. Best params for next run: EMA fast={optimized['ema_fast']}, slow={optimized['ema_slow']}, entry_threshold={optimized['entry_threshold']}")

    def update_summary_metrics(self):
        trades = self.storage.get_trades()
        if not trades:
            return
            
        winning_trades = [t for t in trades if t["pnl"] > 0]
        win_rate = (len(winning_trades) / len(trades)) * 100.0 if trades else 0.0
        total_pnl = sum([t["pnl"] for t in trades])
        
        # Calculate Drawdown
        pnl_series = [t["pnl"] for t in trades]
        cumulative = []
        running_total = 0.0
        for p in pnl_series:
            running_total += p
            cumulative.append(running_total)
            
        max_dd = 0.0
        peak = 0.0
        for val in cumulative:
            if val > peak:
                peak = val
            drawdown = peak - val
            if drawdown > max_dd:
                max_dd = drawdown
                
        metrics = {
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "total_trades": len(trades),
            "sharpe_ratio": round(self.calculate_sharpe_ratio(pnl_series), 2),
            "max_drawdown": round(max_dd, 2)
        }
        self.storage.update_metrics(metrics)

    def calculate_sharpe_ratio(self, pnl_list: List[float]) -> float:
        if len(pnl_list) < 3:
            return 1.0 # default
        import numpy as np
        pnl_arr = np.array(pnl_list)
        mean_p = np.mean(pnl_arr)
        std_p = np.std(pnl_arr)
        if std_p == 0:
            return 0.0
        return (mean_p / std_p) * np.sqrt(252) # annualized factor

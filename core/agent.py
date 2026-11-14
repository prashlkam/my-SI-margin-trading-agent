import json
import logging
import datetime
import os
import time
from typing import Dict, List, Any, Optional
from core.market_time import now_ist
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

        # Safety state (FyersAPI.md §6.5)
        self.kill_switch = False      # manual kill: blocks trading until re-armed
        self.halted_today = False     # circuit breaker: blocks trading until next day
        self.halt_reason = ""

        # Optional CPU-quota guard (core.cpu_budget.CpuBudget), set by main.py on Azure F1
        self.cpu_guard = None
        # Live-mode daily risk counters persisted across restarts
        self._risk_state_path = os.path.join(config_dir, "risk_state.json")
        self._risk_state_date = None
        self._risk_state_saved = None
        # symbol -> monotonic time of last exit fill; guards against re-adopting a position
        # the broker's position book hasn't caught up on yet
        self._recent_exits: Dict[str, float] = {}

        # Log system status
        self.log("Margin Trading Agent Initialised in Standby Mode.")

    @property
    def is_simulated(self) -> bool:
        return hasattr(self.broker, "virtual_time")

    def log(self, message: str, level: str = "INFO", timestamp: Optional[datetime.datetime] = None):
        log_fn = {"CRITICAL": logger.critical, "ERROR": logger.error, "WARNING": logger.warning}.get(level, logger.info)
        log_fn(message)
        if timestamp is None:
            if hasattr(self.broker, "virtual_time"):
                timestamp = self.broker.virtual_time
            else:
                timestamp = now_ist()
        self.storage.save_log_entry(message, level, timestamp)

    def set_broker(self, broker: BaseConnector):
        self.broker = broker
        self.log(f"Switched broker connector to {broker.__class__.__name__}.")

    # ---------------------------------------------------------------- safety
    def engage_kill_switch(self, reason: str = "Manual kill switch"):
        """Flatten everything and block all trading until rearm() is called."""
        self.kill_switch = True
        self.halt_reason = reason
        self.log(f"KILL SWITCH ENGAGED: {reason}. Flattening all positions.", "CRITICAL")
        self._flatten_and_verify()
        self.risk_manager.peak_prices.clear()
        self.risk_manager.position_directions.clear()
        self.risk_manager.tp_targets.clear()

    def rearm(self):
        self.kill_switch = False
        self.halt_reason = ""
        self.log("Kill switch re-armed by user. Trading may resume.", "WARNING")

    def halt_for_day(self, reason: str, current_time: datetime.datetime):
        """Circuit breaker: flatten and stop trading until the next trading day."""
        if self.halted_today:
            return
        self.halted_today = True
        self.halt_reason = reason
        self.log(f"CIRCUIT BREAKER: {reason}. Flattening and halting for the day.", "CRITICAL", current_time)
        self.run_daily_closeout(current_time, reason=reason)

    def _flatten_and_verify(self) -> bool:
        """close_all_positions(), then re-query; retry once. Returns True if flat."""
        for attempt in range(2):
            try:
                self.broker.close_all_positions()
            except Exception as e:
                self.log(f"close_all_positions failed (attempt {attempt + 1}): {e}", "ERROR")
            if not self.is_simulated:
                time.sleep(1.0)  # let exit orders reach the broker's position book
            try:
                remaining = self.broker.get_positions()
            except Exception as e:
                self.log(f"Could not verify positions are flat: {e}", "CRITICAL")
                return False
            if not remaining:
                return True
            self.log(f"Positions still open after square-off attempt {attempt + 1}: "
                     f"{[(p['symbol'], p['qty']) for p in remaining]}", "WARNING")
        self.log("SQUARE-OFF INCOMPLETE — positions remain open. Check the broker terminal immediately!", "CRITICAL")
        return False

    def _execute(self, symbol: str, side: str, qty: int, reference_price: float) -> Optional[Dict[str, Any]]:
        """
        Place a market order and normalise the broker result.
        Returns {"qty": filled_qty, "price": actual fill price} or None if nothing filled.
        Works with both the mock broker (status COMPLETE) and Fyers (status FILLED/PARTIAL/...).
        """
        try:
            result = self.broker.place_order(symbol, side, qty, "MARKET") or {}
        except ConnectionError:
            raise
        except Exception as e:
            result = {"status": "REJECTED", "message": str(e)}

        status = str(result.get("status", "")).upper()
        filled_qty = int(result.get("filled_qty", qty if status in ("COMPLETE", "FILLED") else 0) or 0)
        if status in ("COMPLETE", "FILLED", "PARTIAL") and filled_qty > 0:
            self.risk_manager.register_order_success()
            price = float(result.get("avg_fill_price") or result.get("price") or reference_price)
            if status == "PARTIAL":
                self.log(f"Partial fill on {side} {symbol}: {filled_qty}/{qty} @ Rs. {price}", "WARNING")
            return {"qty": filled_qty, "price": price}

        self.log(f"Order {side} {qty} {symbol} not filled ({status or 'ERROR'}): {result.get('message', '')}", "ERROR")
        if self.risk_manager.register_order_reject() and not self.halted_today:
            # Set the halt directly (not via halt_for_day) — we may already be inside a closeout.
            self.halted_today = True
            self.halt_reason = f"{self.risk_manager.consecutive_rejects} consecutive order rejections"
            self.log(f"CIRCUIT BREAKER: {self.halt_reason}. Flattening and halting for the day.", "CRITICAL")
            self._flatten_and_verify()
        return None

    def _day_pnl(self, positions: List[Dict[str, Any]]) -> float:
        """Total day P&L: broker-reported when available (source of truth), else internal."""
        if hasattr(self.broker, "get_day_pnl"):
            try:
                broker_pnl = self.broker.get_day_pnl()
                if broker_pnl is not None:
                    return broker_pnl["total"]
            except Exception as e:
                logger.warning(f"Broker day P&L unavailable, using internal: {e}")
        return self.risk_manager.realized_pnl + sum(pos["pnl"] for pos in positions)

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
            self.halted_today = False
            if not self.kill_switch:
                self.halt_reason = ""
        self.last_date = current_date

        # Kill switch / circuit breaker: no scanning, selection or trading.
        if self.kill_switch or self.halted_today:
            return

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

        if not self.is_simulated:
            self._sync_risk_state(current_date)

    def _sync_risk_state(self, current_date: datetime.date):
        """
        Persist trade count / realized P&L for live trading, and restore them after an
        unplanned restart so daily limits still hold (Azure.md §2a).
        """
        day = current_date.isoformat()
        rm = self.risk_manager
        if self._risk_state_date != day:
            self._risk_state_date = day
            try:
                with open(self._risk_state_path) as f:
                    saved = json.load(f)
                if saved.get("date") == day:
                    rm.trade_count = max(rm.trade_count, int(saved.get("trade_count", 0)))
                    if not rm.realized_pnl:
                        rm.realized_pnl = float(saved.get("realized_pnl", 0.0))
                    self.log(f"Restored today's risk state after restart: {rm.trade_count} trades, "
                             f"realized P&L Rs. {rm.realized_pnl:.2f}.", "WARNING")
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.error(f"Could not read risk state: {e}")
        state = {"date": day, "trade_count": rm.trade_count, "realized_pnl": round(rm.realized_pnl, 2)}
        if state != self._risk_state_saved:
            try:
                with open(self._risk_state_path, "w") as f:
                    json.dump(state, f)
                self._risk_state_saved = state
            except Exception as e:
                logger.error(f"Could not persist risk state: {e}")

    def run_pre_market_scan(self):
        self.log("Running Pre-Market scan (9:05 AM)...")
        # For mock broker, we get simulated news and movers.
        # For real brokers, we query sector performance or APIs.
        if hasattr(self.broker, "get_news_and_movers"):
            try:
                res = self.broker.get_news_and_movers()
            except Exception as e:
                self.log(f"Pre-market movers scan failed: {e}", "ERROR")
                res = {}
            self.watchlist_news = res.get("news", [])
            self.watchlist_movers = res.get("movers", [])
        elif not self.is_simulated:
            # Live broker without a movers API: no fabricated inputs on a real account.
            # Selection falls back to scoring the broker's (or default) static universe.
            self.watchlist_news = []
            self.watchlist_movers = []
        else:
            # Simulation-only placeholder news and movers
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
            # Fallback: the broker's configured universe (live) or a default basket
            candidates = list(getattr(self.broker, "universe", None) or ["INFY", "RELIANCE", "TATAMOTORS", "TCS", "HDFCBANK"])
            
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
            except ConnectionError:
                raise
            except Exception as e:
                logger.error(f"Failed scoring candidate {symbol}: {e}")

        # Sort candidates by price action score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        # Pick top 2
        selected = [item[0] for item in scores[:2]]
        if len(selected) < 2 and self.is_simulated:
            # Hardcoded fallbacks if we couldn't score enough (simulation only —
            # live trading only trades symbols that were actually scored on real data)
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
                
        if self.traded_stocks:
            self.log(f"Selected stocks for the day: {', '.join(self.traded_stocks)}")
        else:
            self.log("No candidates could be scored on real market data — no stocks selected today.", "WARNING")
        if hasattr(self.broker, "subscribe_symbols") and self.traded_stocks:
            self.broker.subscribe_symbols(self.traded_stocks)
        self.risk_manager.reset_daily()
        self.market_opened = True
        self.market_closed = False

    def run_live_trading(self, current_time: datetime.datetime):
        # 0. Azure free-tier CPU quota: flatten while we still can, before Azure stops the app
        if self.cpu_guard is not None and self.cpu_guard.should_halt_trading():
            self.halt_for_day("Azure free-tier CPU quota nearly used up", current_time)
            return

        # 1. Daily P&L breakers: profit cap (Rs. 12,000) and max-loss circuit breaker
        positions = self.broker.get_positions()

        # Positions the risk manager doesn't know about (e.g. after an app restart) would
        # otherwise have no trailing stop-loss. Adopt them.
        for pos in positions:
            if time.monotonic() - self._recent_exits.get(pos["symbol"], -1e9) < 15:
                continue
            if pos["symbol"] not in self.risk_manager.position_directions:
                direction = "LONG" if pos["qty"] > 0 else "SHORT"
                self.risk_manager.adopt_position(pos["symbol"], direction, pos["avg_price"],
                                                 pos.get("current_price") or pos["avg_price"])
                self.log(f"Adopted untracked {direction} position in {pos['symbol']} ({pos['qty']} @ "
                         f"Rs. {pos['avg_price']}) — trailing stop-loss now active.", "WARNING", current_time)
                if hasattr(self.broker, "subscribe_symbols"):
                    self.broker.subscribe_symbols([pos["symbol"]])
        day_pnl = self._day_pnl(positions)
        unrealized_pnl = day_pnl - self.risk_manager.realized_pnl

        if self.risk_manager.check_daily_profit_limit(unrealized_pnl):
            self.log(f"Daily profit target exceeded Rs. {self.risk_manager.DAILY_PROFIT_LIMIT:.2f}! Closing all positions.", "WARNING")
            self.run_daily_closeout(current_time, reason="Daily Profit Cap Hit")
            return

        if self.risk_manager.check_daily_loss_limit(day_pnl):
            self.halt_for_day(f"Daily max loss Rs. {self.risk_manager.DAILY_MAX_LOSS:.2f} breached (P&L Rs. {day_pnl:.2f})", current_time)
            return

        # 2. Monitor existing positions for SL, TP, or Trend Reversal
        for pos in positions:
            if self.halted_today or self.kill_switch:
                return
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
                
                # Execute broker close and use the ACTUAL fill price for P&L
                order_type = "SELL" if qty > 0 else "BUY"
                fill = self._execute(symbol, order_type, abs(qty), current_price)
                if fill is None:
                    self.log(f"Exit order for {symbol} did not fill — position still open, will retry next tick.", "CRITICAL")
                    continue
                exit_price = fill["price"]
                closed_qty = fill["qty"] if qty > 0 else -fill["qty"]
                self._recent_exits[symbol] = time.monotonic()

                realized = (exit_price - avg_price) * closed_qty
                if abs(closed_qty) == abs(qty):
                    self.risk_manager.register_position_close(symbol, realized)
                else:
                    self.risk_manager.realized_pnl += realized
                    self.log(f"Exit of {symbol} only partially filled ({abs(closed_qty)}/{abs(qty)}); remainder stays tracked.", "WARNING")
                
                # Save to database log
                self.storage.save_trade({
                    "symbol": symbol,
                    "direction": "LONG" if qty > 0 else "SHORT",
                    "entry_price": avg_price,
                    "exit_price": exit_price,
                    "qty": abs(closed_qty),
                    "pnl": realized,
                    "exit_reason": exit_reason,
                    "timestamp": current_time.isoformat()
                })
                
                # Update dashboard metrics
                self.update_summary_metrics()

                # 3. CONTRA POSITION TRIGGER:
                # "in case of loss or trend reversal - close the active position and take a contra position to get back into profits"
                # If we closed because of SL or Trend Reversal, take the opposite trade immediately.
                if (trigger == "SL" or trend_reversal) and abs(closed_qty) == abs(qty) \
                        and not self.risk_manager.is_trade_limit_reached():
                    contra_direction = "SHORT" if qty > 0 else "LONG"
                    self.log(f"Initiating CONTRA position ({contra_direction}) on {symbol} to recover losses.", "IMPORTANT")
                    self.enter_position(symbol, contra_direction, exit_price, current_time)

        # 4. Check for new entries on watchlist if trade limit not reached
        if not self.risk_manager.is_trade_limit_reached():
            active_symbols = [pos["symbol"] for pos in positions]
            
            for symbol in self.traded_stocks:
                if self.halted_today or self.kill_switch:
                    return
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
        if self.kill_switch or self.halted_today:
            return
        # Calculate quantity based on exposure limit (max Rs. 15,000 per stock to keep total exposure under Rs. 30,000)
        # Position sizing: quantity = allocation / price
        allocation = self.risk_manager.ALLOCATION_PER_TRADE
        qty = int(allocation / price)
        if qty <= 0:
            qty = 1
            
        # Verify exposure limit
        positions = self.broker.get_positions()
        if not self.risk_manager.check_exposure_limit(symbol, price, qty, positions):
            self.log(f"Order skipped: Exposure would exceed Rs. {self.risk_manager.MAX_EXPOSURE:,.0f} threshold.", "WARNING")
            return

        # Pre-trade funds/margin check (FyersAPI.md §6.1)
        if hasattr(self.broker, "get_funds"):
            try:
                available = self.broker.get_funds()["available"]
            except ConnectionError:
                raise
            except Exception as e:
                self.log(f"Order skipped: could not fetch available funds ({e}).", "WARNING")
                return
            required = self.risk_manager.required_margin(qty, price)
            if available < required:
                self.log(f"Order skipped: insufficient margin for {qty} {symbol} "
                         f"(need ~Rs. {required:,.2f}, available Rs. {available:,.2f}).", "WARNING")
                return
            
        # Place order and register the position at the ACTUAL fill price
        order_type = "BUY" if direction == "LONG" else "SELL"
        fill = self._execute(symbol, order_type, qty, price)
        if fill is None:
            return
        fill_price = fill["price"]

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
        self.risk_manager.register_position_open(symbol, direction, fill_price, volatility_pct=vol)
        self.log(f"Opened {direction} position: {fill['qty']} shares of {symbol} at Rs. {fill_price}", "IMPORTANT")

    def run_daily_closeout(self, current_time: datetime.datetime, reason: Optional[str] = None):
        self.log(f"Closing open positions for daily square-off ({reason or '3:25 PM'})...")
        if reason is None:
            reason = "End-of-day Squareoff" if current_time.strftime("%H:%M") >= "15:25" else "Daily Profit Cap Hit"
        
        # Get active positions to log and close
        try:
            positions = self.broker.get_positions()
        except Exception as e:
            self.log(f"Could not fetch positions for square-off: {e}", "CRITICAL")
            positions = []
        for pos in positions:
            symbol = pos["symbol"]
            qty = pos["qty"]
            avg_price = pos["avg_price"]
            try:
                current_price = self.broker.get_quote(symbol)
            except Exception:
                current_price = pos.get("current_price", avg_price)
            
            # Place order to close, P&L from the actual fill
            order_type = "SELL" if qty > 0 else "BUY"
            try:
                fill = self._execute(symbol, order_type, abs(qty), current_price)
                if fill is None:
                    continue  # the flatten-and-verify below retries
                closed_qty = fill["qty"] if qty > 0 else -fill["qty"]
                pnl = (fill["price"] - avg_price) * closed_qty
                self.risk_manager.register_position_close(symbol, pnl)
                
                # Save trade log
                self.storage.save_trade({
                    "symbol": symbol,
                    "direction": "LONG" if qty > 0 else "SHORT",
                    "entry_price": avg_price,
                    "exit_price": fill["price"],
                    "qty": abs(closed_qty),
                    "pnl": pnl,
                    "exit_reason": reason,
                    "timestamp": current_time.isoformat()
                })
            except Exception as e:
                self.log(f"Error squaring off {symbol}: {e}", "ERROR")

        # Safety back-up: flatten anything left (incl. broker auto-square-off races) and verify flat
        self._flatten_and_verify()
        self.risk_manager.peak_prices.clear()
        self.risk_manager.position_directions.clear()
        if hasattr(self.broker, "unsubscribe_symbols") and self.traded_stocks:
            self.broker.unsubscribe_symbols(self.traded_stocks)
        
        self.market_closed = True
        self.market_opened = False
        self.update_summary_metrics()
        self.log("Daily trade operations completed. Positions squared off.")
        
        # 5. Nightly Self-Improvement optimization (optional; skipped if the CPU quota is low)
        min_cpu = float(os.environ.get("OPTIMIZER_MIN_CPU_SEC", 900))
        if self.cpu_guard is not None and self.cpu_guard.remaining_seconds() < min_cpu:
            self.log("Skipping parameter optimization: not enough Azure CPU quota left today.", "WARNING")
            return
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

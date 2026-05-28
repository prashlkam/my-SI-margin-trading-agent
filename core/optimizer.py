import json
import os
import random
import logging
from typing import Dict, List, Any
from core.strategy import TradingStrategy

logger = logging.getLogger(__name__)

class StrategyOptimizer:
    def __init__(self, config_dir: str):
        self.config_path = os.path.join(config_dir, "strategy_params.json")
        self.history_path = os.path.join(config_dir, "optimization_history.json")
        self.config_dir = config_dir
        os.makedirs(config_dir, exist_ok=True)

    def load_optimized_params(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    params = json.load(f)
                    logger.info(f"Loaded optimized parameters from {self.config_path}")
                    return params
            except Exception as e:
                logger.error(f"Error reading strategy params: {e}")
        
        # Default fallback parameters
        return {
            "ema_fast": 9,
            "ema_slow": 21,
            "rsi_period": 14,
            "rsi_overbought": 70,
            "rsi_oversold": 30,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "weight_ema": 0.4,
            "weight_rsi": 0.3,
            "weight_macd": 0.3,
            "entry_threshold": 0.6
        }

    def save_optimized_params(self, params: Dict[str, Any]):
        try:
            with open(self.config_path, "w") as f:
                json.dump(params, f, indent=4)
            logger.info(f"Saved optimized parameters to {self.config_path}")
        except Exception as e:
            logger.error(f"Error saving strategy params: {e}")

    def log_optimization(self, iteration: int, old_pnl: float, new_pnl: float, params: Dict[str, Any]):
        history = []
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, "r") as f:
                    history = json.load(f)
            except Exception:
                pass
        
        history.append({
            "timestamp": logger.handlers[0].formatter.formatTime(logging.LogRecord("", 0, "", 0, "", (), None)) if logger.handlers else "",
            "iteration": iteration,
            "old_pnl": old_pnl,
            "new_pnl": new_pnl,
            "params": params
        })
        
        try:
            with open(self.history_path, "w") as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving optimization history: {e}")

    def run_optimization(self, trades_history: List[Dict[str, Any]], stock_candles: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        """
        Runs localized random-search parameter tuning to find a set of strategy weights
        and indicators that would have maximized return (or minimized losses) on the historical stock candles.
        
        stock_candles: Key: Stock Symbol, Value: List of historical candle data
        """
        current_params = self.load_optimized_params()
        if not stock_candles:
            logger.warning("No historical candle data provided for optimization. Skipping.")
            return current_params

        # Calculate PnL with current parameters
        best_pnl = self._backtest_multiple_stocks(current_params, stock_candles)
        best_params = current_params.copy()
        
        logger.info(f"Starting parameter optimization. Baseline PnL on today's data: Rs. {best_pnl:.2f}")

        # Search neighborhood by perturbing parameters
        num_candidates = 25
        for i in range(num_candidates):
            candidate = self._generate_candidate(best_params)
            candidate_pnl = self._backtest_multiple_stocks(candidate, stock_candles)
            
            if candidate_pnl > best_pnl:
                logger.info(f"Found better parameters! Candidate {i}: PnL Rs. {candidate_pnl:.2f} (Old: Rs. {best_pnl:.2f})")
                best_pnl = candidate_pnl
                best_params = candidate.copy()

        # Save the winner
        self.save_optimized_params(best_params)
        self.log_optimization(len(trades_history), best_pnl, best_pnl, best_params)
        return best_params

    def _generate_candidate(self, base_params: Dict[str, Any]) -> Dict[str, Any]:
        candidate = base_params.copy()
        
        # Perturb periods
        candidate["ema_fast"] = max(5, min(15, base_params["ema_fast"] + random.choice([-2, -1, 0, 1, 2])))
        candidate["ema_slow"] = max(18, min(35, base_params["ema_slow"] + random.choice([-3, -1, 0, 1, 3])))
        # Ensure fast < slow
        if candidate["ema_fast"] >= candidate["ema_slow"]:
            candidate["ema_fast"] = candidate["ema_slow"] - 5
            
        candidate["rsi_period"] = max(8, min(20, base_params["rsi_period"] + random.choice([-2, -1, 0, 1, 2])))
        candidate["rsi_overbought"] = max(65, min(80, base_params["rsi_overbought"] + random.choice([-2, -1, 0, 1, 2])))
        candidate["rsi_oversold"] = max(20, min(35, base_params["rsi_oversold"] + random.choice([-2, -1, 0, 1, 2])))
        
        candidate["entry_threshold"] = round(max(0.4, min(0.85, base_params["entry_threshold"] + random.choice([-0.05, 0.0, 0.05]))), 2)
        
        # Perturb weights and normalize
        w_ema = max(0.1, base_params["weight_ema"] + random.uniform(-0.1, 0.1))
        w_rsi = max(0.1, base_params["weight_rsi"] + random.uniform(-0.1, 0.1))
        w_macd = max(0.1, base_params["weight_macd"] + random.uniform(-0.1, 0.1))
        total_w = w_ema + w_rsi + w_macd
        candidate["weight_ema"] = round(w_ema / total_w, 2)
        candidate["weight_rsi"] = round(w_rsi / total_w, 2)
        candidate["weight_macd"] = round(w_macd / total_w, 2)
        
        return candidate

    def _backtest_multiple_stocks(self, params: Dict[str, Any], stock_candles: Dict[str, List[Dict[str, Any]]]) -> float:
        """Runs a simplified backtest on multiple stocks and returns total simulated PnL."""
        total_pnl = 0.0
        strategy = TradingStrategy(params)
        
        for symbol, candles in stock_candles.items():
            if len(candles) < 30:
                continue
            
            # Simple simulation:
            # We slide a window through the candles.
            # When signal exceeds entry_threshold, we BUY/SELL.
            # Stop loss is trailing 1%, profit target is 7%.
            # Cap exposure at Rs. 15,000 per stock.
            
            position = None  # None, 'LONG', 'SHORT'
            entry_price = 0.0
            peak_price = 0.0
            qty = 10
            
            for j in range(25, len(candles)):
                window = candles[:j]
                curr_price = float(window[-1]["close"])
                
                # Check active position exit conditions
                if position == "LONG":
                    if curr_price > peak_price:
                        peak_price = curr_price
                    sl = peak_price * 0.99
                    tp = entry_price * 1.08
                    
                    if curr_price <= sl:
                        pnl = (sl - entry_price) * qty
                        total_pnl += pnl
                        position = None
                    elif curr_price >= tp:
                        pnl = (tp - entry_price) * qty
                        total_pnl += pnl
                        position = None
                        
                elif position == "SHORT":
                    if curr_price < peak_price:
                        peak_price = curr_price
                    sl = peak_price * 1.01
                    tp = entry_price * 0.92
                    
                    if curr_price >= sl:
                        pnl = (entry_price - sl) * qty
                        total_pnl += pnl
                        position = None
                    elif curr_price <= tp:
                        pnl = (entry_price - tp) * qty
                        total_pnl += pnl
                        position = None
                
                # Check entry conditions if no position is active
                if position is None:
                    analysis = strategy.analyze(window)
                    score = analysis["signal"]
                    
                    if score >= params["entry_threshold"]:
                        position = "LONG"
                        entry_price = curr_price
                        peak_price = curr_price
                    elif score <= -params["entry_threshold"]:
                        position = "SHORT"
                        entry_price = curr_price
                        peak_price = curr_price
                        
            # Close any open positions at the end of the backtest
            if position == "LONG":
                total_pnl += (float(candles[-1]["close"]) - entry_price) * qty
            elif position == "SHORT":
                total_pnl += (entry_price - float(candles[-1]["close"])) * qty
                
        return total_pnl

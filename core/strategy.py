import pandas as pd
import numpy as np
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class TradingStrategy:
    def __init__(self, params: Dict[str, Any] = None):
        # Default strategy parameters (will be tuned by core/optimizer.py)
        self.params = {
            "ema_fast": 9,
            "ema_slow": 21,
            "rsi_period": 14,
            "rsi_overbought": 70,
            "rsi_oversold": 30,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            # Weights for different indicators in final signal scoring (sum to 1.0)
            "weight_ema": 0.4,
            "weight_rsi": 0.3,
            "weight_macd": 0.3,
            # Minimum signal strength threshold to take a position (0.0 to 1.0)
            "entry_threshold": 0.6
        }
        if params:
            self.params.update(params)
        logger.info(f"Initialized Strategy with parameters: {self.params}")

    def update_params(self, params: Dict[str, Any]):
        self.params.update(params)
        logger.info(f"Updated Strategy parameters: {self.params}")

    def calculate_ema(self, df: pd.DataFrame, period: int) -> pd.Series:
        return df["close"].ewm(span=period, adjust=False).mean()

    def calculate_rsi(self, df: pd.DataFrame, period: int) -> pd.Series:
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def calculate_macd(self, df: pd.DataFrame, fast: int, slow: int, signal_period: int) -> tuple:
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
        macd_hist = macd_line - signal_line
        return macd_line, signal_line, macd_hist

    def analyze(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyze price data and return trading indicators and a trading signal score (-1.0 to +1.0).
        Positive values indicate BUY pressure, negative values indicate SELL pressure.
        """
        if len(candles) < max(self.params["ema_slow"], self.params["macd_slow"] + self.params["macd_signal"]):
            # Not enough data
            return {"signal": 0.0, "ema_fast": 0.0, "ema_slow": 0.0, "rsi": 50.0, "macd": 0.0, "macd_signal": 0.0}

        df = pd.DataFrame(candles)
        df["close"] = df["close"].astype(float)
        
        # Calculate indicators
        ema_f = self.calculate_ema(df, self.params["ema_fast"])
        ema_s = self.calculate_ema(df, self.params["ema_slow"])
        rsi = self.calculate_rsi(df, self.params["rsi_period"])
        macd_l, macd_s, macd_h = self.calculate_macd(df, self.params["macd_fast"], self.params["macd_slow"], self.params["macd_signal"])
        
        latest_idx = len(df) - 1
        
        # Latest values
        curr_price = df.loc[latest_idx, "close"]
        curr_ema_f = ema_f.loc[latest_idx]
        curr_ema_s = ema_s.loc[latest_idx]
        curr_rsi = rsi.loc[latest_idx]
        curr_macd = macd_l.loc[latest_idx]
        curr_macd_s = macd_s.loc[latest_idx]
        curr_macd_h = macd_h.loc[latest_idx]

        # 1. EMA Signal (Trend following)
        # Bullish if fast EMA > slow EMA, bearish if fast < slow
        ema_signal = 1.0 if curr_ema_f > curr_ema_s else -1.0
        
        # 2. RSI Signal (Mean reversion / Momentum)
        # Overbought -> Bearish signal, Oversold -> Bullish signal
        # Alternatively, RSI crossing 50 can indicate trend momentum. Let's combine:
        rsi_signal = 0.0
        if curr_rsi < self.params["rsi_oversold"]:
            # oversold, bullish signal strength scales with how deep it is oversold
            oversold = max(self.params["rsi_oversold"], 1.0)  # prevent division by zero
            rsi_signal = (oversold - curr_rsi) / oversold
            rsi_signal = min(1.0, rsi_signal * 2) # boost
        elif curr_rsi > self.params["rsi_overbought"]:
            # overbought, bearish signal
            overbought_range = max(100.0 - self.params["rsi_overbought"], 1.0)  # prevent division by zero
            rsi_signal = -(curr_rsi - self.params["rsi_overbought"]) / overbought_range
            rsi_signal = max(-1.0, rsi_signal * 2)
        else:
            # RSI momentum: bullish if rsi > 50, bearish if rsi < 50
            rsi_signal = (curr_rsi - 50.0) / 50.0 # ranges from -1.0 to 1.0

        # 3. MACD Signal (Momentum Crossover)
        # Bullish if MACD line is above signal line, bearish if below
        macd_signal = 1.0 if curr_macd > curr_macd_s else -1.0
        
        # 4. Price Action (Recent trend)
        # Check slope of last 5 candles
        last_5_closes = df["close"].tail(5).tolist()
        base_price = last_5_closes[0] if last_5_closes[0] != 0 else 1.0  # prevent division by zero
        price_action_slope = (last_5_closes[-1] - last_5_closes[0]) / base_price
        price_action_signal = np.clip(price_action_slope * 100, -1.0, 1.0) # scaled

        # Combine signals using weights
        w_ema = self.params["weight_ema"]
        w_rsi = self.params["weight_rsi"]
        w_macd = self.params["weight_macd"]
        
        combined_signal = (
            w_ema * ema_signal +
            w_rsi * rsi_signal +
            w_macd * macd_signal
        )
        
        # Incorporate price action slope
        final_score = 0.8 * combined_signal + 0.2 * price_action_signal
        final_score = float(np.clip(final_score, -1.0, 1.0))

        return {
            "signal": final_score,
            "ema_fast": float(curr_ema_f),
            "ema_slow": float(curr_ema_s),
            "rsi": float(curr_rsi) if not np.isnan(curr_rsi) else 50.0,
            "macd": float(curr_macd),
            "macd_signal": float(curr_macd_s),
            "macd_hist": float(curr_macd_h),
            "price_action_signal": float(price_action_signal)
        }
        
    def check_trend_reversal(self, current_direction: str, candles: List[Dict[str, Any]]) -> bool:
        """
        Check if indicators suggest a strong trend reversal against the current position.
        """
        analysis = self.analyze(candles)
        signal = analysis["signal"]
        
        if current_direction == "LONG" and signal < -0.4:
            logger.warning(f"Trend reversal detected for LONG position! Signal: {signal:.2f}")
            return True
        elif current_direction == "SHORT" and signal > 0.4:
            logger.warning(f"Trend reversal detected for SHORT position! Signal: {signal:.2f}")
            return True
            
        return False

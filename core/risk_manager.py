import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

class RiskManager:
    def __init__(self):
        # Configuration
        self.MAX_EXPOSURE = 30000.0       # Rs. 30,000 limit
        self.TRAILING_SL_PCT = 0.01      # 1% strict stop loss
        self.TP_MIN = 0.08               # Minimum take-profit threshold (1:8 RR with 1% SL)
        self.TP_MAX = 0.20               # Maximum take-profit threshold (more the better)
        self.MAX_TRADES_DAILY = 12       # Less than 12 trades
        self.DAILY_PROFIT_LIMIT = 12000.0 # Rs. 12,000 cap
        
        # State tracking
        self.trade_count = 0
        self.realized_pnl = 0.0
        # Tracks peak prices for trailing stop loss
        # Key: symbol, Value: float (highest price reached for LONG, lowest for SHORT)
        self.peak_prices: Dict[str, float] = {}
        # Tracks the direction of active positions
        # Key: symbol, Value: str ('LONG' or 'SHORT')
        self.position_directions: Dict[str, str] = {}
        # Per-symbol take-profit targets (range 8% to 20%)
        # Key: symbol, Value: float (TP percentage as decimal)
        self.tp_targets: Dict[str, float] = {}

    def reset_daily(self):
        self.trade_count = 0
        self.realized_pnl = 0.0
        self.peak_prices.clear()
        self.position_directions.clear()
        self.tp_targets.clear()
        logger.info("Risk Manager state reset for new trading day.")

    def check_exposure_limit(self, symbol: str, price: float, qty: int, active_positions: List[Dict[str, Any]]) -> bool:
        """
        Check if taking this position violates the Rs. 30,000 total exposure limit.
        Exposure is calculated as the sum of absolute values of all position exposures.
        """
        current_exposure = 0.0
        # Calculate current active exposure
        for pos in active_positions:
            pos_symbol = pos["symbol"]
            # Exclude symbol if we are modifying its position (we calculate its new exposure instead)
            if pos_symbol != symbol:
                current_exposure += abs(pos["qty"] * pos["current_price"])

        # Proposed exposure for this stock
        proposed_exposure = abs(qty * price)
        total_proposed_exposure = current_exposure + proposed_exposure
        
        if total_proposed_exposure > self.MAX_EXPOSURE:
            logger.warning(f"Exposure limit breach! Proposed: Rs. {total_proposed_exposure:.2f}, Limit: Rs. {self.MAX_EXPOSURE}")
            return False
        return True

    def register_position_open(self, symbol: str, direction: str, entry_price: float, volatility_pct: float = 0.0):
        """
        Register the opening of a new position to track its trailing stop loss.
        Sets a per-symbol take-profit target in the range 8%-20% based on volatility.
        Higher volatility → higher TP target (more the better).
        """
        self.position_directions[symbol] = direction.upper()
        self.peak_prices[symbol] = entry_price
        
        # Dynamic TP target: scale between TP_MIN and TP_MAX based on volatility.
        # Volatility drives the TP higher since "more the better".
        # Clamp volatility influence to [0, 1] range.
        vol_factor = min(1.0, volatility_pct / 5.0)  # 5% volatility → full TP_MAX
        tp_target = self.TP_MIN + vol_factor * (self.TP_MAX - self.TP_MIN)
        self.tp_targets[symbol] = round(tp_target, 3)
        
        self.trade_count += 1
        logger.info(f"Registered {direction} position for {symbol} at Rs. {entry_price}. TP target: {self.tp_targets[symbol]*100:.1f}%. Daily trade count: {self.trade_count}")

    def register_position_close(self, symbol: str, exit_pnl: float):
        """Register position closure and record realized PnL."""
        if symbol in self.peak_prices:
            del self.peak_prices[symbol]
        if symbol in self.position_directions:
            del self.position_directions[symbol]
        if symbol in self.tp_targets:
            del self.tp_targets[symbol]
        self.realized_pnl += exit_pnl
        logger.info(f"Closed position for {symbol}. Closed PnL: Rs. {exit_pnl:.2f}. Total daily realized PnL: Rs. {self.realized_pnl:.2f}")

    def update_and_check_signals(self, symbol: str, current_price: float) -> str:
        """
        Updates the trailing stop-loss peaks and evaluates if a Stop-Loss or Take-Profit has been triggered.
        Take-profit target is per-symbol (range 8%-20%), dynamically set when the position was opened.
        Returns:
            'SL' if trailing stop-loss triggered.
            'TP' if take-profit target triggered.
            'HOLD' if no risk trigger has been breached.
        """
        if symbol not in self.peak_prices or symbol not in self.position_directions:
            return "HOLD"

        direction = self.position_directions[symbol]
        peak = self.peak_prices[symbol]
        tp_target = self.tp_targets.get(symbol, self.TP_MIN)
        
        if direction == "LONG":
            # Trailing SL: 1% below peak price
            # If price moves higher, update the peak
            if current_price > peak:
                self.peak_prices[symbol] = current_price
                peak = current_price
                logger.debug(f"Updated LONG peak for {symbol} to Rs. {peak:.2f}")
            
            sl_price = peak * (1.0 - self.TRAILING_SL_PCT)
            
            if current_price <= sl_price:
                logger.info(f"Stop loss triggered for LONG {symbol}! Current: {current_price}, SL: {sl_price:.2f} (Peak: {peak:.2f})")
                return "SL"
            
            # Active take-profit at the per-symbol target (between 8% and 20%)
            entry_price = self.peak_prices[symbol]
            tp_price = entry_price * (1.0 + tp_target)
            if current_price >= tp_price:
                logger.info(f"Take profit triggered for LONG {symbol}! Gain: {((current_price - entry_price) / entry_price)*100:.2f}% (Target: {tp_target*100:.1f}%, Price: {current_price})")
                return "TP"

        elif direction == "SHORT":
            # Trailing SL for Short: 1% above valley price
            # If price moves lower, update the valley (peak)
            if current_price < peak:
                self.peak_prices[symbol] = current_price
                peak = current_price
                logger.debug(f"Updated SHORT valley for {symbol} to Rs. {peak:.2f}")

            sl_price = peak * (1.0 + self.TRAILING_SL_PCT)
            
            if current_price >= sl_price:
                logger.info(f"Stop loss triggered for SHORT {symbol}! Current: {current_price}, SL: {sl_price:.2f} (Valley: {peak:.2f})")
                return "SL"

            # Active take-profit at the per-symbol target (between 8% and 20%)
            entry_price = peak
            tp_price = entry_price * (1.0 - tp_target)
            if current_price <= tp_price:
                profit_pct = (entry_price - current_price) / entry_price
                logger.info(f"Take profit triggered for SHORT {symbol}! Gain: {profit_pct*100:.2f}% (Target: {tp_target*100:.1f}%, Price: {current_price})")
                return "TP"

        return "HOLD"

    def is_trade_limit_reached(self) -> bool:
        return self.trade_count >= self.MAX_TRADES_DAILY

    def check_daily_profit_limit(self, current_pnl: float) -> bool:
        """
        Check if total daily profit (realized + unrealized) exceeds Rs. 12,000.
        If so, return True indicating we must close all positions and stop trading for the day.
        """
        total_pnl = self.realized_pnl + current_pnl
        if total_pnl >= self.DAILY_PROFIT_LIMIT:
            logger.warning(f"Daily profit target reached! PnL: Rs. {total_pnl:.2f}, Target: Rs. {self.DAILY_PROFIT_LIMIT:.2f}. Halting operations.")
            return True
        return False

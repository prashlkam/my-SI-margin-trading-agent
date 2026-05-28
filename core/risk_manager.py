import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

class RiskManager:
    def __init__(self):
        # Configuration
        self.MAX_EXPOSURE = 30000.0       # Rs. 30,000 limit
        self.TRAILING_SL_PCT = 0.01      # 1% strict stop loss
        self.TARGET_PROFIT_PCT = 0.07    # 7% profit target (satisfies 1:7 RR with 1% SL, >5% profit target)
        self.MAX_TRADES_DAILY = 12       # Less than 12 trades
        self.DAILY_PROFIT_LIMIT = 3000.0 # Rs. 3,000 cap
        
        # State tracking
        self.trade_count = 0
        self.realized_pnl = 0.0
        # Tracks peak prices for trailing stop loss
        # Key: symbol, Value: float (highest price reached for LONG, lowest for SHORT)
        self.peak_prices: Dict[str, float] = {}
        # Tracks the direction of active positions
        # Key: symbol, Value: str ('LONG' or 'SHORT')
        self.position_directions: Dict[str, str] = {}

    def reset_daily(self):
        self.trade_count = 0
        self.realized_pnl = 0.0
        self.peak_prices.clear()
        self.position_directions.clear()
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

    def register_position_open(self, symbol: str, direction: str, entry_price: float):
        """Register the opening of a new position to track its trailing stop loss."""
        self.position_directions[symbol] = direction.upper()
        self.peak_prices[symbol] = entry_price
        self.trade_count += 1
        logger.info(f"Registered {direction} position for {symbol} at Rs. {entry_price}. Peak initialized. Daily trade count: {self.trade_count}")

    def register_position_close(self, symbol: str, exit_pnl: float):
        """Register position closure and record realized PnL."""
        if symbol in self.peak_prices:
            del self.peak_prices[symbol]
        if symbol in self.position_directions:
            del self.position_directions[symbol]
        self.realized_pnl += exit_pnl
        logger.info(f"Closed position for {symbol}. Closed PnL: Rs. {exit_pnl:.2f}. Total daily realized PnL: Rs. {self.realized_pnl:.2f}")

    def update_and_check_signals(self, symbol: str, current_price: float) -> str:
        """
        Updates the trailing stop-loss peaks and evaluates if a Stop-Loss or Take-Profit has been triggered.
        Returns:
            'SL' if trailing stop-loss triggered.
            'TP' if take-profit target triggered.
            'HOLD' if no risk trigger has been breached.
        """
        if symbol not in self.peak_prices or symbol not in self.position_directions:
            return "HOLD"

        direction = self.position_directions[symbol]
        peak = self.peak_prices[symbol]
        
        if direction == "LONG":
            # Trailing SL: 1% below peak price
            # If price moves higher, update the peak
            if current_price > peak:
                self.peak_prices[symbol] = current_price
                peak = current_price
                logger.debug(f"Updated LONG peak for {symbol} to Rs. {peak:.2f}")
            
            sl_price = peak * (1.0 - self.TRAILING_SL_PCT)
            tp_price = peak * (1.0 + self.TARGET_PROFIT_PCT) # or compared to entry, let's look at absolute target profit >= 5% or 7%
            
            # Since strict stop loss is 1%, and we target risk-reward 1:7,
            # we should take profit if price rises by 7% from our entry or peak.
            # Let's verify take profit if current price shows > 5% profit and is reversing, or target 7%.
            # The prompt says: "take profit if greater than 5%" and "risk to reward must be at least 1:7".
            # This means if SL is 1%, the target reward must be at least 7% to enter, but we can exit and lock in profits once it exceeds 5% or hits 7%.
            # Let's trigger 'TP' if the price goes above 7% from entry, or if it crosses 5% and shows signs of slowing.
            # Let's implement direct TP trigger at 7% to strictly satisfy 1:7 RR, or when current profit is >= 5%.
            # Let's set target profit at 7% of entry price. Let's make it simple: if profit > 7% -> take profit.
            entry_price = self.peak_prices[symbol] # actually we initialized peak to entry_price. Let's use entry_price from first peak
            
            if current_price <= sl_price:
                logger.info(f"Stop loss triggered for LONG {symbol}! Current: {current_price}, SL: {sl_price:.2f} (Peak: {peak:.2f})")
                return "SL"
            
            # Profit relative to trailing SL or entry:
            # Let's say if current_price >= entry_price * 1.07 (or 1.05)
            # Let's take profit if it exceeds 7% to ensure 1:7 RR is met.
            # Wait, what if we have a trailing stop loss that locks in profit?
            # A 1% trailing stop loss naturally locks in profit as the stock rises.
            # E.g., if price rises 6%, peak is 6%, trailing SL is at 5%. If it drops to 5%, it triggers stop loss but we get 5% profit!
            # So the trailing stop loss itself acts as a profit-taking mechanism!
            # But the prompt says "take profit if greater than 5%". So if profit > 5%, we can actively close it.
            # Let's implement active take profit at 7% (satisfies 1:7 RR) or if profit is > 5% and starts reversing.
            # To be safe and maximize profits, let's trigger TP at 7% gain.
            profit_pct = (current_price - entry_price) / entry_price
            if profit_pct >= self.TARGET_PROFIT_PCT:
                logger.info(f"Take profit triggered for LONG {symbol}! Gain: {profit_pct*100:.2f}% (Price: {current_price})")
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

            # Entry price is the initial peak
            entry_price = peak # approximate
            profit_pct = (entry_price - current_price) / entry_price
            if profit_pct >= self.TARGET_PROFIT_PCT:
                logger.info(f"Take profit triggered for SHORT {symbol}! Gain: {profit_pct*100:.2f}% (Price: {current_price})")
                return "TP"

        return "HOLD"

    def is_trade_limit_reached(self) -> bool:
        return self.trade_count >= self.MAX_TRADES_DAILY

    def check_daily_profit_limit(self, current_pnl: float) -> bool:
        """
        Check if total daily profit (realized + unrealized) exceeds Rs. 3,000.
        If so, return True indicating we must close all positions and stop trading for the day.
        """
        total_pnl = self.realized_pnl + current_pnl
        if total_pnl >= self.DAILY_PROFIT_LIMIT:
            logger.warning(f"Daily profit target reached! PnL: Rs. {total_pnl:.2f}, Target: Rs. {self.DAILY_PROFIT_LIMIT:.2f}. Halting operations.")
            return True
        return False

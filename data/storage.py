import json
import os
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

class DataStorage:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.trades_file = os.path.join(base_dir, "trades.json")
        self.logs_file = os.path.join(base_dir, "app_logs.json")
        self.metrics_file = os.path.join(base_dir, "daily_metrics.json")
        os.makedirs(base_dir, exist_ok=True)
        
        # Buffering parameters for fast backtesting
        self.buffered_mode = False
        self.buffered_trades = []
        self.buffered_logs = []
        self.buffered_metrics = None
        
        # Initialise files if they don't exist
        for f in [self.trades_file, self.logs_file, self.metrics_file]:
            if not os.path.exists(f):
                with open(f, "w") as file_handle:
                    json.dump([], file_handle)

    def save_trade(self, trade: Dict[str, Any]):
        if self.buffered_mode:
            self.buffered_trades.append(trade)
            return
        trades = self.get_trades()
        trades.append(trade)
        try:
            with open(self.trades_file, "w") as f:
                json.dump(trades, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving trade log: {e}")

    def get_trades(self) -> List[Dict[str, Any]]:
        if self.buffered_mode:
            return self.buffered_trades
        if os.path.exists(self.trades_file):
            try:
                with open(self.trades_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def save_log_entry(self, message: str, level: str = "INFO", timestamp=None):
        import datetime
        if timestamp is None:
            timestamp = datetime.datetime.now()
        time_str = timestamp.strftime("%Y-%m-%d %H:%M:%S") if isinstance(timestamp, datetime.datetime) else str(timestamp)
        
        if self.buffered_mode:
            self.buffered_logs.append({
                "timestamp": time_str,
                "level": level,
                "message": message
            })
            self.buffered_logs = self.buffered_logs[-200:]
            return
            
        entries = self.get_log_entries()
        entries.append({
            "timestamp": time_str,
            "level": level,
            "message": message
        })
        # Limit to last 200 entries to prevent files growing too large
        entries = entries[-200:]
        try:
            with open(self.logs_file, "w") as f:
                json.dump(entries, f, indent=4)
        except Exception:
            pass

    def get_log_entries(self) -> List[Dict[str, Any]]:
        if self.buffered_mode:
            return self.buffered_logs
        if os.path.exists(self.logs_file):
            try:
                with open(self.logs_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def get_metrics(self) -> Dict[str, Any]:
        if self.buffered_mode and self.buffered_metrics is not None:
            return self.buffered_metrics
        if os.path.exists(self.metrics_file):
            try:
                with open(self.metrics_file, "r") as f:
                    data = json.load(f)
                    if data:
                        return data
            except Exception:
                pass
        return {
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "total_trades": 0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0
        }

    def update_metrics(self, new_metrics: Dict[str, Any]):
        if self.buffered_mode:
            self.buffered_metrics = new_metrics
            return
        try:
            with open(self.metrics_file, "w") as f:
                json.dump(new_metrics, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving daily metrics: {e}")
            
    def clear_all(self):
        """Reset dashboard simulation data."""
        if self.buffered_mode:
            self.buffered_trades = []
            self.buffered_logs = []
            self.buffered_metrics = {
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "total_trades": 0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0
            }
            return
            
        with open(self.trades_file, "w") as f:
            json.dump([], f)
        with open(self.logs_file, "w") as f:
            json.dump([], f)
        with open(self.metrics_file, "w") as f:
            json.dump({
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "total_trades": 0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0
            }, f)
        logger.info("Cleared all trade logs and metrics from storage.")

    def flush_buffer(self):
        """Write all in-memory buffered data to disk files."""
        # Ensure we write out everything
        try:
            with open(self.trades_file, "w") as f:
                json.dump(self.buffered_trades, f, indent=4)
        except Exception as e:
            logger.error(f"Error flushing trades: {e}")
            
        try:
            with open(self.logs_file, "w") as f:
                json.dump(self.buffered_logs, f, indent=4)
        except Exception as e:
            logger.error(f"Error flushing logs: {e}")
            
        if self.buffered_metrics is not None:
            try:
                with open(self.metrics_file, "w") as f:
                    json.dump(self.buffered_metrics, f, indent=4)
            except Exception as e:
                logger.error(f"Error flushing metrics: {e}")
        else:
            try:
                with open(self.metrics_file, "w") as f:
                    json.dump({
                        "win_rate": 0.0,
                        "total_pnl": 0.0,
                        "total_trades": 0,
                        "sharpe_ratio": 0.0,
                        "max_drawdown": 0.0
                    }, f, indent=4)
            except Exception as e:
                logger.error(f"Error flushing default metrics: {e}")


from abc import ABC, abstractmethod
from typing import Dict, List, Any

class BaseConnector(ABC):
    @abstractmethod
    def connect(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate with the broker using provided API keys or credentials."""
        pass

    @abstractmethod
    def get_quote(self, symbol: str) -> float:
        """Fetch the current last traded price (LTP) for a symbol."""
        pass

    @abstractmethod
    def place_order(self, symbol: str, transaction_type: str, qty: int, order_type: str = "MARKET") -> Dict[str, Any]:
        """
        Place an order.
        transaction_type: 'BUY' or 'SELL'
        order_type: 'MARKET' or 'LIMIT'
        """
        pass

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """Retrieve all active open positions."""
        pass

    @abstractmethod
    def get_historical_data(self, symbol: str, interval: str = "5minute", duration_days: int = 5) -> List[Dict[str, Any]]:
        """Fetch historical data for a symbol (list of dictionaries with open, high, low, close, volume)."""
        pass

    @abstractmethod
    def close_all_positions(self) -> List[Dict[str, Any]]:
        """Square off and close all open positions."""
        pass

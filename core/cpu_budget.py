"""
CPU-quota guard for Azure App Service Free (F1) — see Azure.md §2a.

F1 apps get a fixed CPU (Day) quota that resets at 00:00 UTC. If the app
exceeds it, Azure STOPS the app until the reset — in the middle of a trading
session that would leave open positions with no stop-loss management.

This tracker measures the process's own CPU time, persists it per UTC day
(so restarts don't reset the count), and lets the agent halt and flatten
while it still can, and skip the optional nightly optimizer when low.
Disabled unless CPU_DAILY_BUDGET_SEC (or AZURE_FREE_TIER=true) is set.
"""
import datetime
import json
import logging
import os
import resource
import threading
import time

logger = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class CpuBudget:
    def __init__(self, data_dir: str):
        free_tier = _env_flag("AZURE_FREE_TIER")
        budget = os.environ.get("CPU_DAILY_BUDGET_SEC", "3600" if free_tier else "")
        self.enabled = bool(budget)
        self.budget = float(budget or 0)
        # Halt trading once this fraction of the quota is used (leaves headroom for the
        # square-off itself, startup cost after a restart, and measurement error).
        self.halt_fraction = float(os.environ.get("CPU_HALT_FRACTION", 0.75))
        self.path = os.path.join(data_dir, "cpu_usage.json")
        self._lock = threading.Lock()
        self._day = self._utc_day()
        self._offset = self._load()          # CPU used today by earlier processes
        self._base = self._process_cpu()     # this process's CPU at start of tracking
        self._last_save = 0.0

    @staticmethod
    def _utc_day() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _process_cpu() -> float:
        self_usage = resource.getrusage(resource.RUSAGE_SELF)
        child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return (self_usage.ru_utime + self_usage.ru_stime + child_usage.ru_utime + child_usage.ru_stime)

    def _load(self) -> float:
        try:
            with open(self.path) as f:
                data = json.load(f)
            return float(data.get("used_sec", 0.0)) if data.get("day") == self._day else 0.0
        except Exception:
            return 0.0

    def used_seconds(self) -> float:
        if not self.enabled:
            return 0.0
        with self._lock:
            today = self._utc_day()
            if today != self._day:  # quota reset at 00:00 UTC
                self._day, self._offset, self._base = today, 0.0, self._process_cpu()
            used = self._offset + (self._process_cpu() - self._base)
            if time.monotonic() - self._last_save > 60:
                self._last_save = time.monotonic()
                try:
                    with open(self.path, "w") as f:
                        json.dump({"day": self._day, "used_sec": round(used, 1)}, f)
                except Exception as e:
                    logger.debug(f"Could not persist CPU usage: {e}")
            return used

    def remaining_seconds(self) -> float:
        return float("inf") if not self.enabled else max(self.budget - self.used_seconds(), 0.0)

    def should_halt_trading(self) -> bool:
        return self.enabled and self.used_seconds() >= self.budget * self.halt_fraction

    def status(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        used = self.used_seconds()
        return {"enabled": True, "used_sec": round(used, 1), "budget_sec": self.budget,
                "halt_at_sec": round(self.budget * self.halt_fraction, 1)}

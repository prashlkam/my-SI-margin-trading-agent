"""
IST clock and NSE trading-calendar helpers (FyersAPI.md §8.1).

The agent compares wall-clock strings such as "09:15" and "15:25", so live
trading must always run on India Standard Time regardless of the host TZ.
"""
import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# NSE equity-segment weekday holidays. Update every December when NSE
# publishes the next year's circular. Extra dates can be supplied without a
# code change via the NSE_HOLIDAYS env var (comma-separated YYYY-MM-DD) or a
# JSON list in <DATA_DIR>/nse_holidays.json.
NSE_HOLIDAYS = {
    # 2026
    "2026-01-15",  # Municipal Corporation Election - Maharashtra
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Id
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali Balipratipada
    "2026-11-24",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}


def now_ist() -> datetime.datetime:
    """Current IST time as a naive datetime (the agent works with naive datetimes)."""
    return datetime.datetime.now(IST).replace(tzinfo=None)


def load_extra_holidays(data_dir: str = "") -> set:
    extra = set()
    env = os.environ.get("NSE_HOLIDAYS", "")
    extra.update(d.strip() for d in env.split(",") if d.strip())
    if data_dir:
        path = os.path.join(data_dir, "nse_holidays.json")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    extra.update(str(d) for d in json.load(f))
            except Exception as e:
                logger.error(f"Could not read {path}: {e}")
    return extra


def is_trading_day(day: datetime.date, extra_holidays: set = frozenset()) -> bool:
    if day.weekday() >= 5:
        return False
    key = day.strftime("%Y-%m-%d")
    return key not in NSE_HOLIDAYS and key not in extra_holidays

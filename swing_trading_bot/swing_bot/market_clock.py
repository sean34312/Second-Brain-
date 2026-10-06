"""US equity market-hours helpers (regular session, America/New_York)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)


def now_et() -> datetime:
    return datetime.now(ET)


def today_et() -> date:
    return now_et().date()


def is_regular_session(now: datetime | None = None) -> bool:
    """Weekday 9:30-16:00 ET. Does not know about exchange holidays -
    the live broker asks Robinhood for the real calendar instead."""
    now = (now or now_et()).astimezone(ET)
    return now.weekday() < 5 and OPEN <= now.time() < CLOSE

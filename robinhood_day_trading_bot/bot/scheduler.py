"""Market-hours helpers, all in US/Eastern regardless of the host machine's
local timezone.

Note: this uses a fixed clock schedule and does NOT check the actual NYSE
holiday/early-close calendar. Don't run it on a half day or market holiday
without checking manually -- it will happily believe the market is open.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

from zoneinfo import ZoneInfo

from bot.config import BotConfig


def _parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


class MarketClock:
    def __init__(self, config: BotConfig):
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.market_open = _parse_hhmm(config.market_open)
        self.no_new_entries_after = _parse_hhmm(config.no_new_entries_after)
        self.force_close_at = _parse_hhmm(config.force_close_at)
        self.market_close = _parse_hhmm(config.market_close)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def is_weekday(self) -> bool:
        return self.now().weekday() < 5

    def is_market_open(self) -> bool:
        t = self.now().time()
        return self.is_weekday() and self.market_open <= t < self.market_close

    def entries_allowed(self) -> bool:
        t = self.now().time()
        return self.is_weekday() and self.market_open <= t < self.no_new_entries_after

    def should_force_close(self) -> bool:
        t = self.now().time()
        return self.is_weekday() and t >= self.force_close_at

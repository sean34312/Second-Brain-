"""Persistent bot state (JSON) - survives restarts.

Stores the metadata the broker doesn't know about: entry signal, stop price,
resting stop-order id, entry date, plus the daily-loss bookkeeping.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ManagedPosition:
    symbol: str
    quantity: float
    entry_price: float
    stop_price: float
    entry_time: str  # ISO timestamp
    entry_date: str  # YYYY-MM-DD (ET)
    entry_reasons: str = ""
    stop_order_id: str | None = None


@dataclass
class BotState:
    day: str = ""  # YYYY-MM-DD (ET) the daily counters belong to
    day_start_equity: float = 0.0
    realized_pnl_today: float = 0.0
    halted_today: bool = False
    positions: dict[str, ManagedPosition] = field(default_factory=dict)
    last_exit_dates: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "BotState":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            backup = path.with_suffix(".corrupt")
            path.replace(backup)
            log.error("State file unreadable (%s); moved to %s and starting fresh", e, backup)
            return cls()
        positions = {s: ManagedPosition(**p) for s, p in raw.pop("positions", {}).items()}
        return cls(positions=positions, **raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)  # atomic on POSIX - a crash never leaves a half-written file

    def roll_day(self, today: str, equity: float) -> bool:
        """Reset daily counters if the date changed. Returns True on a new day."""
        if self.day == today:
            return False
        self.day = today
        self.day_start_equity = equity
        self.realized_pnl_today = 0.0
        self.halted_today = False
        return True

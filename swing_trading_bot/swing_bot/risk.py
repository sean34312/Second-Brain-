"""Risk management: position sizing, stop-loss, position limits, daily loss limit."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import RiskConfig


@dataclass
class PositionSize:
    quantity: float
    entry_price: float
    stop_price: float
    risk_amount: float  # $ lost if the stop is hit (excluding slippage/gaps)
    notional: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.quantity > 0


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def stop_price(self, entry_price: float) -> float:
        return round(entry_price * (1 - self.cfg.stop_loss_pct / 100), 2)

    def position_size(self, equity: float, entry_price: float, buying_power: float) -> PositionSize:
        """Size a position so that hitting the stop loses at most ``risk_per_trade_pct`` of equity.

        quantity = (equity * risk%) / (entry - stop), then capped by
        ``max_position_pct`` of equity and by available buying power.
        """
        c = self.cfg
        stop = self.stop_price(entry_price)
        per_share_risk = entry_price - stop
        if equity <= 0 or entry_price <= 0 or per_share_risk <= 0:
            return PositionSize(0, entry_price, stop, 0, 0, "invalid equity/price")

        risk_budget = equity * c.risk_per_trade_pct / 100
        qty = risk_budget / per_share_risk
        limits = {"risk budget": qty,
                  "max position size": equity * c.max_position_pct / 100 / entry_price,
                  "buying power": max(buying_power, 0) / entry_price}
        binding = min(limits, key=limits.get)
        qty = limits[binding]
        qty = math.floor(qty * 1e4) / 1e4 if c.allow_fractional else math.floor(qty)

        if qty <= 0:
            return PositionSize(0, entry_price, stop, 0, 0,
                                f"size rounds to zero (limited by {binding})")
        return PositionSize(
            quantity=qty, entry_price=entry_price, stop_price=stop,
            risk_amount=round(qty * per_share_risk, 2), notional=round(qty * entry_price, 2),
            reason=f"limited by {binding}",
        )

    def can_open_position(self, open_positions: int, halted: bool) -> tuple[bool, str]:
        if halted:
            return False, "daily loss limit reached - trading halted for today"
        if open_positions >= self.cfg.max_positions:
            return False, f"max concurrent positions ({self.cfg.max_positions}) reached"
        return True, ""

    def daily_loss_pct(self, day_start_equity: float, current_equity: float) -> float:
        if day_start_equity <= 0:
            return 0.0
        return (day_start_equity - current_equity) / day_start_equity * 100

    def daily_loss_breached(self, day_start_equity: float, current_equity: float) -> bool:
        return self.daily_loss_pct(day_start_equity, current_equity) >= self.cfg.daily_max_loss_pct

    def stop_hit(self, price: float, stop_price: float) -> bool:
        return price <= stop_price

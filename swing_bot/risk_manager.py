"""Position sizing, exposure limits, and daily loss circuit-breaker."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass
class SizeResult:
    shares: int
    risk_per_share: float
    dollar_risk: float
    reason: str = ""


class RiskManager:
    def __init__(self, config):
        self.config = config
        self._trading_day: date = date.today()
        self._realized_pnl_today: float = 0.0

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if today != self._trading_day:
            self._trading_day = today
            self._realized_pnl_today = 0.0

    def record_realized_pnl(self, pnl: float) -> None:
        self._roll_day_if_needed()
        self._realized_pnl_today += pnl

    def daily_pnl(self) -> float:
        self._roll_day_if_needed()
        return self._realized_pnl_today

    def daily_loss_limit_hit(self, account_equity: float) -> bool:
        self._roll_day_if_needed()
        if account_equity <= 0:
            return True
        loss_limit = -abs(self.config.daily_loss_limit_pct) * account_equity
        return self._realized_pnl_today <= loss_limit

    def can_open_new_position(self, open_position_count: int, account_equity: float) -> tuple[bool, str]:
        if self.daily_loss_limit_hit(account_equity):
            return False, "daily loss limit reached; new entries blocked"
        if open_position_count >= self.config.max_concurrent_positions:
            return False, f"max concurrent positions reached ({self.config.max_concurrent_positions})"
        return True, ""

    def position_size(self, account_equity: float, entry_price: float, stop_loss_price: float) -> SizeResult:
        """Size the position so that a stop-out risks at most risk_per_trade_pct
        of account equity, i.e. shares * (entry - stop) <= equity * risk_pct."""
        if entry_price <= 0 or stop_loss_price <= 0:
            return SizeResult(0, 0.0, 0.0, "invalid price inputs")
        risk_per_share = entry_price - stop_loss_price
        if risk_per_share <= 0:
            return SizeResult(0, 0.0, 0.0, "stop loss must be below entry price for a long position")

        dollar_risk = account_equity * self.config.risk_per_trade_pct
        shares = math.floor(dollar_risk / risk_per_share)
        if shares <= 0:
            return SizeResult(0, risk_per_share, dollar_risk, "computed position size is zero")
        return SizeResult(shares, risk_per_share, dollar_risk, "ok")

    @staticmethod
    def validate_buying_power(buying_power: float, shares: int, price: float) -> tuple[bool, str]:
        cost = shares * price
        if cost > buying_power:
            return False, f"insufficient buying power: need ${cost:,.2f}, have ${buying_power:,.2f}"
        return True, ""

    def stop_loss_price(self, entry_price: float) -> float:
        return round(entry_price * (1 - self.config.stop_loss_pct), 2)

    def take_profit_price(self, entry_price: float) -> float:
        return round(entry_price * (1 + self.config.take_profit_pct), 2)

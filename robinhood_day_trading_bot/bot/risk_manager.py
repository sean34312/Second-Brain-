"""Position sizing and the safety circuit breakers that sit in front of every
order: max concurrent positions, the pattern-day-trader rule, and an
intraday drawdown halt.

These circuit breakers are ON by default (see bot/config.py). Turning off
``enforce_pdt_rule`` does not change what Robinhood enforces on its end --
it only removes this bot's own pre-check, so you would find out about a PDT
restriction from Robinhood rejecting the order (or flagging the account)
instead of from this bot refusing to place it.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

import robin_stocks.robinhood as rh

from bot.config import BotConfig
from bot.logger_setup import setup_logging

logger = setup_logging(__name__)


@dataclass
class AccountSnapshot:
    equity: float
    buying_power: float
    margin_enabled: bool


class RiskManager:
    def __init__(self, config: BotConfig, paper_starting_equity: float = 25000.0):
        self.config = config
        self.paper_equity = paper_starting_equity
        self.day_start_equity: Optional[float] = None
        self.trading_halted_for_day = False
        os.makedirs(config.state_dir, exist_ok=True)
        self._day_trades_path = os.path.join(config.state_dir, "day_trades.json")
        self._day_trades: List[str] = self._load_day_trades()

    # -- account state ----------------------------------------------------
    def get_account_snapshot(self) -> AccountSnapshot:
        if self.config.paper_trading:
            return AccountSnapshot(
                equity=self.paper_equity, buying_power=self.paper_equity, margin_enabled=False
            )
        profile = rh.profiles.load_portfolio_profile()
        account = rh.profiles.load_account_profile()
        equity = float(profile.get("equity") or 0.0)
        buying_power = float(account.get("buying_power") or 0.0)
        margin_enabled = account.get("margin_balances") is not None
        return AccountSnapshot(equity=equity, buying_power=buying_power, margin_enabled=margin_enabled)

    def mark_new_trading_day(self) -> None:
        snap = self.get_account_snapshot()
        self.day_start_equity = snap.equity
        self.trading_halted_for_day = False
        logger.info("New trading day. Starting equity: $%.2f", snap.equity)

    def apply_paper_fill_pnl(self, pnl: float) -> None:
        """Only used in paper-trading mode to keep the simulated equity curve
        moving as fake trades close."""
        self.paper_equity += pnl

    # -- drawdown circuit breaker ------------------------------------------
    def check_drawdown_breach(self) -> bool:
        if self.day_start_equity is None:
            return False
        snap = self.get_account_snapshot()
        if self.day_start_equity <= 0:
            return False
        drawdown = (self.day_start_equity - snap.equity) / self.day_start_equity
        if drawdown >= self.config.max_daily_drawdown_pct:
            if not self.trading_halted_for_day:
                logger.warning(
                    "DRAWDOWN CIRCUIT BREAKER: equity down %.2f%% today (limit %.2f%%). "
                    "Halting new entries.",
                    drawdown * 100,
                    self.config.max_daily_drawdown_pct * 100,
                )
            self.trading_halted_for_day = True
        return self.trading_halted_for_day

    # -- PDT rule -----------------------------------------------------------
    def _load_day_trades(self) -> List[str]:
        if os.path.exists(self._day_trades_path):
            try:
                with open(self._day_trades_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_day_trades(self) -> None:
        with open(self._day_trades_path, "w") as f:
            json.dump(self._day_trades, f)

    def record_day_trade(self) -> None:
        self._day_trades.append(date.today().isoformat())
        self._save_day_trades()

    def count_recent_day_trades(self, lookback_days: int = 5) -> int:
        cutoff = date.today() - timedelta(days=lookback_days)
        recent = [d for d in self._day_trades if date.fromisoformat(d) >= cutoff]
        return len(recent)

    def pdt_blocked(self) -> bool:
        if not self.config.enforce_pdt_rule:
            return False
        snap = self.get_account_snapshot()
        if snap.equity >= self.config.pdt_equity_threshold:
            return False
        return self.count_recent_day_trades() >= self.config.max_day_trades_per_5_days

    # -- gating ---------------------------------------------------------------
    def can_open_new_position(self, open_position_count: int) -> bool:
        if open_position_count >= self.config.max_concurrent_positions:
            return False
        if self.check_drawdown_breach():
            return False
        if self.pdt_blocked():
            logger.info("PDT rule: day-trade limit reached for accounts under $%.0f equity.",
                        self.config.pdt_equity_threshold)
            return False
        return True

    # -- sizing -----------------------------------------------------------------
    def position_size_shares(self, price: float) -> int:
        if price <= 0:
            return 0
        snap = self.get_account_snapshot()
        leverage = self.config.leverage if self.config.use_margin else 1.0
        risk_capital = snap.equity * self.config.position_size_pct * leverage
        risk_capital = min(risk_capital, snap.buying_power)
        shares = math.floor(risk_capital / price)
        return max(shares, 0)

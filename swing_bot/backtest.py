"""Backtests the swing strategy against historical data without touching
the live API, then reports win rate, profit factor, and max drawdown."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List

import pandas as pd

from .indicators import add_indicators
from .risk_manager import RiskManager
from .strategy import Signal, SwingStrategy

logger = logging.getLogger("swing_bot")


@dataclass
class BacktestTrade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    exit_reason: str


@dataclass
class BacktestResult:
    starting_equity: float
    ending_equity: float
    trades: List[BacktestTrade] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return (self.ending_equity - self.starting_equity) / self.starting_equity * 100

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades) * 100

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown_pct(self) -> float:
        if not self.trades:
            return 0.0
        equity = self.starting_equity
        peak = equity
        max_dd = 0.0
        for t in self.trades:
            equity += t.pnl
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
        return max_dd * 100

    def summary(self) -> str:
        lines = [
            "=== Backtest Results ===",
            f"Trades executed:   {len(self.trades)}",
            f"Starting equity:   ${self.starting_equity:,.2f}",
            f"Ending equity:     ${self.ending_equity:,.2f}",
            f"Total return:      {self.total_return_pct:.2f}%",
            f"Win rate:          {self.win_rate:.1f}%",
            f"Profit factor:     {self.profit_factor:.2f}",
            f"Max drawdown:      {self.max_drawdown_pct:.2f}%",
        ]
        return "\n".join(lines)


class Backtester:
    """Runs the same strategy/risk logic as live trading over historical
    OHLC bars, one symbol at a time, using next-bar-close fills."""

    def __init__(self, config, starting_equity: float = 10_000.0):
        self.config = config
        self.strategy = SwingStrategy(config)
        self.risk = RiskManager(config)
        self.starting_equity = starting_equity

    def run(self, price_data: Dict[str, pd.DataFrame]) -> BacktestResult:
        equity = self.starting_equity
        trades: List[BacktestTrade] = []
        open_positions: Dict[str, dict] = {}

        prepared = {sym: add_indicators(df, self.config).dropna(
            subset=["rsi", "macd", "macd_signal", "sma_short", "sma_long"]
        ) for sym, df in price_data.items()}

        all_timestamps = sorted(set().union(*[set(df.index) for df in prepared.values()]))

        for ts in all_timestamps:
            for symbol, df in prepared.items():
                if ts not in df.index:
                    continue
                idx = df.index.get_loc(ts)
                if idx < 1:
                    continue
                window = df.iloc[: idx + 1]
                price = float(window["close"].iloc[-1])

                if symbol in open_positions:
                    pos = open_positions[symbol]
                    result = self.strategy.evaluate(window)
                    exit_reason = None
                    if price <= pos["stop_loss"]:
                        exit_reason = "stop-loss"
                    elif price >= pos["take_profit"]:
                        exit_reason = "take-profit"
                    elif result.signal is Signal.SELL:
                        exit_reason = result.reason

                    if exit_reason:
                        pnl = (price - pos["entry_price"]) * pos["quantity"]
                        equity += pnl
                        self.risk.record_realized_pnl(pnl)
                        trades.append(
                            BacktestTrade(
                                symbol=symbol,
                                entry_time=pos["entry_time"],
                                exit_time=ts,
                                entry_price=pos["entry_price"],
                                exit_price=price,
                                quantity=pos["quantity"],
                                pnl=pnl,
                                exit_reason=exit_reason,
                            )
                        )
                        del open_positions[symbol]
                    continue

                allowed, _ = self.risk.can_open_new_position(len(open_positions), equity)
                if not allowed:
                    continue

                result = self.strategy.evaluate(window)
                if result.signal is not Signal.BUY:
                    continue

                stop_price = self.risk.stop_loss_price(price)
                size = self.risk.position_size(equity, price, stop_price)
                if size.shares <= 0:
                    continue

                open_positions[symbol] = {
                    "entry_price": price,
                    "quantity": size.shares,
                    "stop_loss": stop_price,
                    "take_profit": self.risk.take_profit_price(price),
                    "entry_time": ts,
                }

        # Mark remaining open positions to the last available close.
        for symbol, pos in open_positions.items():
            last_price = float(prepared[symbol]["close"].iloc[-1])
            pnl = (last_price - pos["entry_price"]) * pos["quantity"]
            equity += pnl
            trades.append(
                BacktestTrade(
                    symbol=symbol,
                    entry_time=pos["entry_time"],
                    exit_time=prepared[symbol].index[-1],
                    entry_price=pos["entry_price"],
                    exit_price=last_price,
                    quantity=pos["quantity"],
                    pnl=pnl,
                    exit_reason="end-of-backtest mark-to-market",
                )
            )

        return BacktestResult(starting_equity=self.starting_equity, ending_equity=equity, trades=trades)

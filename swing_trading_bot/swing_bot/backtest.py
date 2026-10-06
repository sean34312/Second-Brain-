"""Portfolio-level backtester using the exact same strategy and risk rules as the bot.

Execution model (no look-ahead):
  * Signals are computed on the CLOSE of day t.
  * Entries and signal-based exits execute at the OPEN of day t+1 (+/- slippage).
  * The stop-loss is checked intraday against the LOW (gap-downs fill at the open).
  * Take-profit is checked intraday against the HIGH. If both could trigger on
    the same bar the stop is assumed to hit first (conservative).
  * The daily loss limit blocks the next day's entries if equity fell by more
    than ``daily_max_loss_pct`` on the day.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .risk import RiskManager
from .strategy import Signal, SwingStrategy

log = logging.getLogger(__name__)


@dataclass
class _Pos:
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    entry_date: pd.Timestamp
    entry_idx: int  # index into the symbol's own frame
    reasons: str


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.Series
    metrics: dict[str, float]
    benchmark_return_pct: float | None = None
    skipped: dict[str, int] = field(default_factory=dict)

    def report(self) -> str:
        m = self.metrics
        pf = m["profit_factor"]
        rows = [
            ("Period", f"{self.equity_curve.index[0]:%Y-%m-%d} -> {self.equity_curve.index[-1]:%Y-%m-%d}"),
            ("Starting capital", f"${m['initial_capital']:,.2f}"),
            ("Ending equity", f"${m['final_equity']:,.2f}"),
            ("Total return", f"{m['total_return_pct']:+.2f}%"),
            ("CAGR", f"{m['cagr_pct']:+.2f}%"),
            ("Max drawdown", f"{m['max_drawdown_pct']:.2f}%"),
            ("Sharpe (daily, rf=0)", f"{m['sharpe']:.2f}"),
            ("Trades", f"{int(m['num_trades'])}"),
            ("Win rate", f"{m['win_rate_pct']:.1f}%"),
            ("Profit factor", "inf" if math.isinf(pf) else f"{pf:.2f}"),
            ("Avg win / avg loss", f"${m['avg_win']:,.2f} / ${m['avg_loss']:,.2f}"),
            ("Expectancy / trade", f"${m['expectancy']:,.2f}"),
            ("Avg holding (days)", f"{m['avg_holding_days']:.1f}"),
            ("Time in market", f"{m['exposure_pct']:.1f}%"),
        ]
        if self.benchmark_return_pct is not None:
            rows.append(("Buy & hold watchlist", f"{self.benchmark_return_pct:+.2f}% (equal weight)"))
        if not self.trades.empty:
            by_reason = self.trades["exit_reason"].str.split(";").str[0].str.replace(r" \(.*", "", regex=True)
            rows.append(("Exit reasons", ", ".join(f"{k}: {v}" for k, v in by_reason.value_counts().items())))
        width = max(len(k) for k, _ in rows)
        body = "\n".join(f"  {k:<{width}}  {v}" for k, v in rows)
        bar = "=" * 64
        return f"{bar}\n BACKTEST RESULTS\n{bar}\n{body}\n{bar}"

    def save(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self.trades.to_csv(out_dir / "trades.csv", index=False)
        self.equity_curve.rename("equity").to_csv(out_dir / "equity_curve.csv", index_label="date")
        pd.Series(self.metrics).to_csv(out_dir / "metrics.csv", header=["value"], index_label="metric")


def compute_metrics(trades: pd.DataFrame, equity: pd.Series, initial_capital: float,
                    exposure_days: int = 0) -> dict[str, float]:
    final = float(equity.iloc[-1]) if len(equity) else initial_capital
    total_ret = (final / initial_capital - 1) * 100
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9) if len(equity) > 1 else 0
    cagr = ((final / initial_capital) ** (1 / years) - 1) * 100 if years and final > 0 else 0.0
    running_max = equity.cummax()
    drawdown = (equity / running_max - 1) * 100
    daily = equity.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0

    pnl = trades["pnl"] if not trades.empty else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0
    return {
        "initial_capital": initial_capital,
        "final_equity": round(final, 2),
        "total_return_pct": round(total_ret, 3),
        "cagr_pct": round(cagr, 3),
        "max_drawdown_pct": round(float(-drawdown.min()) if len(drawdown) else 0.0, 3),
        "sharpe": round(sharpe, 3),
        "num_trades": float(len(pnl)),
        "win_rate_pct": round(len(wins) / len(pnl) * 100, 2) if len(pnl) else 0.0,
        "profit_factor": round(profit_factor, 3) if not math.isinf(profit_factor) else profit_factor,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "expectancy": round(float(pnl.mean()), 2) if len(pnl) else 0.0,
        "avg_holding_days": round(float(trades["holding_days"].mean()), 2) if len(pnl) else 0.0,
        "exposure_pct": round(exposure_days / len(equity) * 100, 2) if len(equity) else 0.0,
    }


class Backtester:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.strategy = SwingStrategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk)
        self.slip = cfg.backtest.slippage_pct / 100
        self.commission = cfg.backtest.commission_per_trade

    def run(self, data: dict[str, pd.DataFrame], start: str | None = None) -> BacktestResult:
        """Run over ``data`` ({symbol: OHLCV frame}). Bars before ``start`` are used
        only as indicator warm-up."""
        bt, rcfg, scfg = self.cfg.backtest, self.cfg.risk, self.cfg.strategy
        frames = {s: self.strategy.prepare(df) for s, df in data.items() if len(df)}
        if not frames:
            raise ValueError("no price data to backtest")
        start_ts = pd.Timestamp(start or bt.start)
        calendar = sorted(set().union(*(f.index for f in frames.values())))
        calendar = [d for d in calendar if d >= start_ts]
        if not calendar:
            raise ValueError(f"no bars on/after {start_ts.date()}")
        loc = {s: {d: i for i, d in enumerate(f.index)} for s, f in frames.items()}
        order = [s for s in self.cfg.bot.watchlist if s in frames] + sorted(set(frames) - set(self.cfg.bot.watchlist))

        cash = bt.initial_capital
        positions: dict[str, _Pos] = {}
        last_close: dict[str, float] = {}
        pending_entries: list[str] = []
        pending_exits: dict[str, str] = {}
        last_exit_day: dict[str, int] = {}
        halted_next_day = False
        trades: list[dict] = []
        equity_points: list[float] = []
        exposure_days = 0
        skipped = {"max_positions": 0, "size_zero": 0, "daily_loss_halt": 0, "cooldown": 0}
        prev_equity = bt.initial_capital
        pending_reasons: dict[str, str] = {}

        def close_pos(p: _Pos, price: float, day: pd.Timestamp, reason: str, idx: int) -> None:
            nonlocal cash
            cash += p.qty * price - self.commission
            pnl = (price - p.entry_price) * p.qty - 2 * self.commission
            trades.append({
                "symbol": p.symbol, "entry_date": p.entry_date.date(), "exit_date": day.date(),
                "entry_price": round(p.entry_price, 4), "exit_price": round(price, 4), "quantity": p.qty,
                "stop_price": p.stop_price, "pnl": round(pnl, 2),
                "pnl_pct": round((price / p.entry_price - 1) * 100, 3),
                "holding_days": idx - p.entry_idx, "entry_reasons": p.reasons, "exit_reason": reason,
            })
            del positions[p.symbol]
            last_exit_day[p.symbol] = day_no

        for day_no, day in enumerate(calendar):
            # ---- 1. exits decided at yesterday's close -> today's open
            for sym, reason in list(pending_exits.items()):
                i = loc[sym].get(day)
                if i is None or sym not in positions:
                    continue
                bar = frames[sym].iloc[i]
                close_pos(positions[sym], bar.open * (1 - self.slip), day, reason, i)
                pending_exits.pop(sym)

            # ---- 2. entries decided at yesterday's close -> today's open
            mark_equity = cash + sum(p.qty * last_close.get(s, p.entry_price) for s, p in positions.items())
            for sym in pending_entries:
                i = loc[sym].get(day)
                if i is None or sym in positions:
                    continue
                ok, _ = self.risk.can_open_position(len(positions), halted_next_day)
                if not ok:
                    skipped["daily_loss_halt" if halted_next_day else "max_positions"] += 1
                    continue
                bar = frames[sym].iloc[i]
                prev_close = frames[sym].close.iloc[i - 1]
                if bar.open > prev_close * (1 + self.cfg.orders.max_entry_gap_pct / 100):
                    continue  # same gap guard as the live bot
                price = bar.open * (1 + self.slip)
                size = self.risk.position_size(mark_equity, price, cash - self.commission)
                if not size.ok:
                    skipped["size_zero"] += 1
                    continue
                cash -= size.quantity * price + self.commission
                positions[sym] = _Pos(sym, size.quantity, price, self.risk.stop_price(price), day, i,
                                      pending_reasons.get(sym, ""))
            pending_entries = []

            # ---- 3. intraday stop-loss / take-profit
            for sym, p in list(positions.items()):
                i = loc[sym].get(day)
                if i is None:
                    continue
                bar = frames[sym].iloc[i]
                tp = p.entry_price * (1 + scfg.take_profit_pct / 100) if scfg.take_profit_pct > 0 else None
                if bar.low <= p.stop_price:
                    fill = min(bar.open, p.stop_price) if i > p.entry_idx else p.stop_price
                    close_pos(p, fill * (1 - self.slip), day, f"stop-loss ({rcfg.stop_loss_pct:g}%)", i)
                elif tp is not None and bar.high >= tp:
                    fill = max(bar.open, tp) if i > p.entry_idx else tp
                    close_pos(p, fill * (1 - self.slip), day, f"take profit (+{scfg.take_profit_pct:g}%)", i)

            # ---- 4. mark to market at the close
            for sym, f in frames.items():
                i = loc[sym].get(day)
                if i is not None:
                    last_close[sym] = float(f.close.iloc[i])
            equity = cash + sum(p.qty * last_close[s] for s, p in positions.items())
            equity_points.append(equity)
            exposure_days += bool(positions)
            halted_next_day = self.risk.daily_loss_breached(prev_equity, equity)
            prev_equity = equity

            # ---- 5. signals at the close for tomorrow
            for sym, p in positions.items():
                i = loc[sym].get(day)
                if i is None:
                    continue
                sig = self.strategy.exit_signal(sym, frames[sym], p.entry_price, i - p.entry_idx, i)
                if sig.signal == Signal.SELL:
                    pending_exits[sym] = sig.reason_text
            pending_reasons = {}
            for sym in order:
                i = loc[sym].get(day)
                if i is None or sym in positions:
                    continue
                if sym in last_exit_day and day_no - last_exit_day[sym] < rcfg.reentry_cooldown_days:
                    skipped["cooldown"] += 1
                    continue
                sig = self.strategy.entry_signal(sym, frames[sym], i)
                if sig.signal == Signal.BUY:
                    pending_entries.append(sym)
                    pending_reasons[sym] = sig.reason_text
            if halted_next_day and pending_entries:
                skipped["daily_loss_halt"] += len(pending_entries)
                pending_entries = []

        # ---- close anything still open at the last close
        last_day = calendar[-1]
        for sym, p in list(positions.items()):
            close_pos(p, last_close[sym], last_day, "end of backtest", len(frames[sym]) - 1)

        equity_curve = pd.Series(equity_points, index=pd.DatetimeIndex(calendar), name="equity")
        equity_curve.iloc[-1] = cash  # after final liquidation
        trades_df = pd.DataFrame(trades)
        metrics = compute_metrics(trades_df, equity_curve, bt.initial_capital, exposure_days)

        bench = []
        for f in frames.values():
            window = f.close[f.index >= start_ts]
            if len(window) > 1:
                bench.append(window.iloc[-1] / window.iloc[0] - 1)
        benchmark = float(np.mean(bench) * 100) if bench else None
        return BacktestResult(trades_df, equity_curve, metrics, benchmark, skipped)

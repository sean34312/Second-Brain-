"""Swing trading strategy: RSI pullback + MACD confirmation + SMA trend filter.

Long-only. Signals are evaluated on *completed* daily bars.

ENTRY (all must be true on the evaluated bar):
  1. Trend bias   : SMA(fast) > SMA(slow) and (optionally) close > SMA(slow)
  2. RSI pullback : RSI touched <= ``rsi_oversold`` within the last ``rsi_lookback``
                    bars, is now rising, and is still below ``rsi_overbought``
  3. MACD confirm : MACD crossed above its signal line within the last
                    ``macd_cross_lookback`` bars and the histogram is still > 0

EXIT (any one is enough; the stop-loss is handled by the risk manager):
  - RSI >= ``rsi_overbought``
  - MACD crosses below its signal line
  - Close < SMA(slow) (trend break)
  - Price >= entry * (1 + ``take_profit_pct``)
  - Held for ``max_holding_days`` bars
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from .config import StrategyConfig
from .indicators import add_indicators


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NONE = "-"  # not enough data


@dataclass
class SignalResult:
    symbol: str
    signal: Signal
    reasons: list[str] = field(default_factory=list)
    timestamp: pd.Timestamp | None = None
    close: float | None = None
    rsi: float | None = None
    macd_hist: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


class SwingStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    @property
    def min_bars(self) -> int:
        c = self.cfg
        return max(c.sma_slow, c.macd_slow + c.macd_signal, c.rsi_period + 1) + max(
            c.rsi_lookback, c.macd_cross_lookback
        ) + 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Add indicator columns to an OHLCV frame."""
        return add_indicators(bars, self.cfg)

    # ------------------------------------------------------------------ helpers
    def _snapshot(self, symbol: str, df: pd.DataFrame, i: int, signal: Signal,
                  reasons: list[str]) -> SignalResult:
        row = df.iloc[i]

        def val(col):
            v = row.get(col)
            return None if v is None or pd.isna(v) else float(v)

        return SignalResult(
            symbol=symbol, signal=signal, reasons=reasons, timestamp=df.index[i],
            close=val("close"), rsi=val("rsi"), macd_hist=val("macd_hist"),
            sma_fast=val("sma_fast"), sma_slow=val("sma_slow"),
        )

    def _has_data(self, df: pd.DataFrame, i: int) -> bool:
        if i < 0:
            i += len(df)
        if i + 1 < self.min_bars:
            return False
        lookback = max(self.cfg.rsi_lookback, self.cfg.macd_cross_lookback)
        window = df[["rsi", "macd_hist", "sma_fast", "sma_slow"]].iloc[i - lookback: i + 1]
        return not window.isna().any().any()

    # ------------------------------------------------------------------ signals
    def entry_signal(self, symbol: str, df: pd.DataFrame, i: int = -1) -> SignalResult:
        """Evaluate the entry rules on bar ``i`` of an indicator frame."""
        if i < 0:
            i += len(df)
        if len(df) == 0:
            return SignalResult(symbol, Signal.NONE, ["no data"])
        if not self._has_data(df, i):
            return self._snapshot(symbol, df, i, Signal.NONE, ["insufficient history"])
        c = self.cfg
        row, prev = df.iloc[i], df.iloc[i - 1]
        reasons: list[str] = []
        ok = True

        # 1. Trend bias
        trend_up = row.sma_fast > row.sma_slow
        if c.require_price_above_slow_sma:
            trend_up = trend_up and row.close > row.sma_slow
        if trend_up:
            reasons.append(f"uptrend (SMA{c.sma_fast} {row.sma_fast:.2f} > SMA{c.sma_slow} {row.sma_slow:.2f})")
        else:
            ok = False
            reasons.append("no uptrend")

        # 2. RSI pullback that is now recovering
        rsi_window = df["rsi"].iloc[i - c.rsi_lookback + 1: i + 1]
        touched_oversold = rsi_window.min() <= c.rsi_oversold
        rising = row.rsi > prev.rsi
        if touched_oversold and rising and row.rsi < c.rsi_overbought:
            reasons.append(f"RSI recovering from {rsi_window.min():.1f} to {row.rsi:.1f}")
        else:
            ok = False
            if not touched_oversold:
                reasons.append(f"RSI {row.rsi:.1f} no recent oversold (<= {c.rsi_oversold:g})")
            elif not rising:
                reasons.append(f"RSI {row.rsi:.1f} not rising")
            else:
                reasons.append(f"RSI {row.rsi:.1f} overbought")

        # 3. MACD bullish crossover confirmation
        hist = df["macd_hist"].iloc[i - c.macd_cross_lookback: i + 1]
        crossed_up = any(hist.iloc[k - 1] <= 0 < hist.iloc[k] for k in range(1, len(hist)))
        if crossed_up and row.macd_hist > 0:
            reasons.append(f"MACD bullish cross (hist {row.macd_hist:.3f})")
        else:
            ok = False
            reasons.append(f"no MACD bullish cross (hist {row.macd_hist:.3f})")

        if ok:
            return self._snapshot(symbol, df, i, Signal.BUY, reasons)
        return self._snapshot(symbol, df, i, Signal.HOLD, reasons)

    def exit_signal(self, symbol: str, df: pd.DataFrame, entry_price: float,
                    bars_held: int, i: int = -1) -> SignalResult:
        """Evaluate indicator/time based exit rules for an open position on bar ``i``."""
        if i < 0:
            i += len(df)
        if i < 1 or len(df) == 0:
            return SignalResult(symbol, Signal.HOLD, ["no data"])
        c = self.cfg
        row, prev = df.iloc[i], df.iloc[i - 1]
        reasons: list[str] = []

        if c.exit_on_rsi_overbought and pd.notna(row.rsi) and row.rsi >= c.rsi_overbought:
            reasons.append(f"RSI overbought ({row.rsi:.1f})")
        if (c.exit_on_macd_bearish_cross and pd.notna(row.macd_hist) and pd.notna(prev.macd_hist)
                and prev.macd_hist >= 0 > row.macd_hist):
            reasons.append("MACD bearish cross")
        if c.exit_on_trend_break and pd.notna(row.sma_slow) and row.close < row.sma_slow:
            reasons.append(f"close below SMA{c.sma_slow} (trend break)")
        if c.take_profit_pct > 0 and row.close >= entry_price * (1 + c.take_profit_pct / 100):
            reasons.append(f"take profit (+{c.take_profit_pct:g}%)")
        if c.max_holding_days > 0 and bars_held >= c.max_holding_days:
            reasons.append(f"max holding period ({c.max_holding_days} days)")

        if reasons:
            return self._snapshot(symbol, df, i, Signal.SELL, reasons)
        return self._snapshot(symbol, df, i, Signal.HOLD, ["exit rules not met"])

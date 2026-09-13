"""Rule-based swing trading strategy combining RSI, MACD, and moving averages.

Entry (BUY) requires trend + momentum confirmation:
  - Uptrend bias: 20-day SMA above 50-day SMA (bullish regime)
  - MACD bullish crossover on the latest bar (momentum turning up)
  - RSI not already overbought (room to run)

Exit (SELL) triggers on any of:
  - MACD bearish crossover (momentum turning down)
  - RSI overbought (mean-reversion risk)
  - Trend reversal: 20-day SMA crosses below 50-day SMA
Stop-loss / take-profit exits are handled separately by the position
tracker since they depend on the entry price, not just the indicators.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    signal: Signal
    reason: str


def _crossed_above(prev_a: float, prev_b: float, cur_a: float, cur_b: float) -> bool:
    return prev_a <= prev_b and cur_a > cur_b


def _crossed_below(prev_a: float, prev_b: float, cur_a: float, cur_b: float) -> bool:
    return prev_a >= prev_b and cur_a < cur_b


class SwingStrategy:
    def __init__(self, config):
        self.config = config

    def evaluate(self, df: pd.DataFrame) -> SignalResult:
        """df must already contain indicator columns (see indicators.add_indicators)
        and have at least 2 rows (current + previous bar)."""
        required = {"rsi", "macd", "macd_signal", "sma_short", "sma_long", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing indicator columns: {missing}")

        clean = df.dropna(subset=list(required))
        if len(clean) < 2:
            return SignalResult(Signal.HOLD, "insufficient history for indicators")

        prev, cur = clean.iloc[-2], clean.iloc[-1]
        cfg = self.config

        macd_bull_cross = _crossed_above(prev["macd"], prev["macd_signal"], cur["macd"], cur["macd_signal"])
        macd_bear_cross = _crossed_below(prev["macd"], prev["macd_signal"], cur["macd"], cur["macd_signal"])
        uptrend = cur["sma_short"] > cur["sma_long"]
        trend_reversal_down = _crossed_below(
            prev["sma_short"], prev["sma_long"], cur["sma_short"], cur["sma_long"]
        )

        if macd_bear_cross:
            return SignalResult(Signal.SELL, "MACD bearish crossover")
        if cur["rsi"] >= cfg.rsi_overbought:
            return SignalResult(Signal.SELL, f"RSI overbought ({cur['rsi']:.1f})")
        if trend_reversal_down:
            return SignalResult(Signal.SELL, "20/50 SMA bearish crossover")

        if macd_bull_cross and uptrend and cur["rsi"] < cfg.rsi_overbought:
            return SignalResult(
                Signal.BUY,
                f"MACD bullish crossover, uptrend (SMA{cfg.sma_short}>SMA{cfg.sma_long}), RSI={cur['rsi']:.1f}",
            )

        return SignalResult(Signal.HOLD, "no confirmed setup")

"""Momentum breakout signal generation.

Long-only by design: Robinhood cash/standard margin accounts cannot short
stocks outright (that needs options or a margin account with short-selling
enabled, which most retail accounts don't have), so this strategy only ever
looks for long entries. Wire in a short path in ``evaluate`` if your account
supports it.

Entry requires ALL of:
  1. 5-minute close breaks above its rolling N-bar resistance.
  2. Volume on that breakout bar is a spike (> volume_spike_multiple x avg).
  3. Fast stochastic (5,3,3) is turning up out of/through the mid-range,
     not already blown off above the overbought line (avoids chasing tops).
  4. MACD is bullish (line > signal) on BOTH the 5-minute and 15-minute
     charts, i.e. the higher timeframe confirms the lower timeframe trigger.

The stop distance is the tighter of the fixed percentage stop and an
ATR-based stop, per the "0.5-1% hard stop" requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from bot.config import BotConfig
from bot.data_feed import MarketDataFeed
from bot.indicators import atr, breakout_levels, macd, stochastic_oscillator, volume_profile, volume_spike
from bot.logger_setup import setup_logging

logger = setup_logging(__name__)


@dataclass
class Signal:
    symbol: str
    side: str  # "buy" (long only, see module docstring)
    entry_price: float
    stop_price: float
    targets: List[float]
    scale_out_fractions: List[float]
    reason: str
    confidence: float = 0.0


class MomentumBreakoutStrategy:
    def __init__(self, config: BotConfig):
        self.config = config

    def evaluate(self, symbol: str, feed: MarketDataFeed) -> Optional[Signal]:
        df5 = feed.get_frame(symbol, "5minute")
        df15 = feed.get_frame(symbol, "15minute")

        min_bars = max(self.config.breakout_lookback_bars, self.config.macd_slow) + 5
        if len(df5) < min_bars or len(df15) < self.config.macd_slow + 5:
            return None

        stoch = stochastic_oscillator(
            df5,
            self.config.stochastic_k_period,
            self.config.stochastic_k_slowing,
            self.config.stochastic_d_period,
        )
        macd5 = macd(df5, self.config.macd_fast, self.config.macd_slow, self.config.macd_signal)
        macd15 = macd(df15, self.config.macd_fast, self.config.macd_slow, self.config.macd_signal)
        levels = breakout_levels(df5, self.config.breakout_lookback_bars)
        vspike = volume_spike(df5, self.config.breakout_lookback_bars, self.config.volume_spike_multiple)
        atr5 = atr(df5, self.config.atr_period)
        vp = volume_profile(df5, lookback=max(50, self.config.breakout_lookback_bars))

        last = df5.iloc[-1]
        last_close = float(last["close"])
        resistance = levels["resistance"].iloc[-1]
        if pd.isna(resistance):
            return None

        breakout = last_close > resistance
        if not breakout:
            return None

        if not bool(vspike.iloc[-1]):
            return None

        k_now, d_now = stoch["%K"].iloc[-1], stoch["%D"].iloc[-1]
        k_prev = stoch["%K"].iloc[-2]
        if pd.isna(k_now) or pd.isna(d_now) or pd.isna(k_prev):
            return None
        stoch_turning_up = k_now > d_now and k_now > k_prev
        stoch_not_exhausted = k_now < 95
        if not (stoch_turning_up and stoch_not_exhausted):
            return None

        macd_bullish_5m = macd5["macd"].iloc[-1] > macd5["signal"].iloc[-1]
        macd_bullish_15m = macd15["macd"].iloc[-1] > macd15["signal"].iloc[-1]
        if not (macd_bullish_5m and macd_bullish_15m):
            return None

        current_atr = atr5.iloc[-1]
        if pd.isna(current_atr) or current_atr <= 0:
            return None

        pct_stop_distance = last_close * self.config.stop_loss_pct
        atr_stop_distance = current_atr * self.config.atr_stop_multiple
        stop_distance = min(pct_stop_distance, atr_stop_distance)
        stop_price = last_close - stop_distance

        targets = [last_close * (1 + t) for t in self.config.profit_targets]

        confidence = 0.0
        confidence += 0.3 if breakout else 0
        confidence += 0.25 if vspike.iloc[-1] else 0
        confidence += 0.2 if stoch_turning_up else 0
        confidence += 0.25 if (macd_bullish_5m and macd_bullish_15m) else 0

        reason = (
            f"breakout>{resistance:.2f} vol_spike stoch_%K={k_now:.1f}/%D={d_now:.1f} "
            f"macd5m/15m bullish poc={vp.get('poc')}"
        )
        logger.info("Signal: %s %s", symbol, reason)

        return Signal(
            symbol=symbol,
            side="buy",
            entry_price=last_close,
            stop_price=stop_price,
            targets=targets,
            scale_out_fractions=list(self.config.profit_scale_out_fractions),
            reason=reason,
            confidence=confidence,
        )

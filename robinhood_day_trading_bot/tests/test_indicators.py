import numpy as np
import pandas as pd
import pytest

from bot.indicators import atr, breakout_levels, macd, stochastic_oscillator, volume_profile, volume_spike


def make_df(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    highs = highs or [c + 0.5 for c in closes]
    lows = lows or [c - 0.5 for c in closes]
    volumes = volumes or [1000] * n
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="5min"),
    )


def test_stochastic_bounds():
    closes = list(np.linspace(10, 20, 40)) + list(np.linspace(20, 10, 40))
    df = make_df(closes)
    result = stochastic_oscillator(df)
    valid = result.dropna()
    assert (valid["%K"] >= 0).all() and (valid["%K"] <= 100).all()
    assert (valid["%D"] >= 0).all() and (valid["%D"] <= 100).all()


def test_stochastic_uptrend_is_high():
    # a straight uptrend should end pinned near the top of the range
    closes = list(np.linspace(10, 20, 30))
    df = make_df(closes)
    result = stochastic_oscillator(df)
    assert result["%K"].iloc[-1] > 75


def test_macd_bullish_after_uptrend():
    closes = list(np.linspace(10, 30, 60))
    df = make_df(closes)
    result = macd(df)
    assert result["macd"].iloc[-1] > result["signal"].iloc[-1]


def test_atr_is_positive_and_reacts_to_range():
    closes = [10] * 20
    highs = [10.1] * 20
    lows = [9.9] * 20
    df = make_df(closes, highs, lows)
    calm = atr(df).iloc[-1]

    wide_highs = [12] * 20
    wide_lows = [8] * 20
    df_wide = make_df(closes, wide_highs, wide_lows)
    volatile = atr(df_wide).iloc[-1]

    assert calm > 0
    assert volatile > calm


def test_volume_spike_detects_surge():
    closes = [10] * 25
    volumes = [1000] * 24 + [5000]
    df = make_df(closes, volumes=volumes)
    result = volume_spike(df, lookback=20, multiple=2.0)
    assert bool(result.iloc[-1]) is True
    assert bool(result.iloc[10]) is False


def test_breakout_levels_exclude_current_bar():
    closes = [10] * 24 + [50]
    df = make_df(closes)
    levels = breakout_levels(df, lookback=20)
    # the resistance for the breakout bar should be based on the prior 10s,
    # not include the 50 itself
    assert levels["resistance"].iloc[-1] == pytest.approx(10.5)


def test_volume_profile_returns_reasonable_levels():
    closes = list(np.linspace(10, 15, 60))
    df = make_df(closes)
    vp = volume_profile(df, lookback=50, bins=10)
    assert vp["poc"] is not None
    assert 10 <= vp["poc"] <= 15.5

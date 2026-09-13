import numpy as np
import pandas as pd

from swing_bot.indicators import macd, rsi, sma


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5])
    result = sma(s, window=3)
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == 2.0
    assert result.iloc[4] == 4.0


def test_rsi_all_gains_is_100():
    s = pd.Series(range(1, 30))  # strictly increasing -> no losses
    result = rsi(s, period=14)
    assert result.iloc[-1] == 100.0


def test_rsi_all_losses_is_0():
    s = pd.Series(list(range(30, 0, -1)))  # strictly decreasing -> no gains
    result = rsi(s, period=14)
    assert result.iloc[-1] == 0.0


def test_rsi_bounded_between_0_and_100():
    rng = np.random.default_rng(42)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    result = rsi(s, period=14).dropna()
    assert (result >= 0).all() and (result <= 100).all()


def test_macd_shapes_and_relationship():
    rng = np.random.default_rng(0)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 100)))
    macd_line, signal_line, hist = macd(s, fast=12, slow=26, signal=9)
    assert len(macd_line) == len(s) == len(signal_line) == len(hist)
    # histogram is macd - signal by construction
    pd.testing.assert_series_equal(hist, macd_line - signal_line, check_names=False)

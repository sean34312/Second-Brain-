"""Technical indicators used for intraday signal generation.

Implemented directly on pandas DataFrames (columns: open, high, low, close,
volume) instead of depending on TA-Lib, which needs a compiled C library and
is a common source of install failures.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def stochastic_oscillator(
    df: pd.DataFrame, k_period: int = 5, k_slowing: int = 3, d_period: int = 3
) -> pd.DataFrame:
    """Fast stochastic oscillator. Returns a DataFrame with columns %K, %D."""
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()
    raw_k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    k = raw_k.rolling(window=k_slowing).mean()
    d = k.rolling(window=d_period).mean()
    return pd.DataFrame({"%K": k, "%D": d})


def macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": histogram})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range, Wilder-smoothed."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def volume_spike(df: pd.DataFrame, lookback: int = 20, multiple: float = 2.0) -> pd.Series:
    """True where current bar's volume exceeds `multiple` x its trailing average."""
    avg_vol = df["volume"].rolling(window=lookback).mean().shift(1)
    return df["volume"] > (avg_vol * multiple)


def breakout_levels(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """Rolling resistance (prior highs) and support (prior lows), excluding the
    current bar so a bar can be evaluated against levels set before it."""
    resistance = df["high"].rolling(window=lookback).max().shift(1)
    support = df["low"].rolling(window=lookback).min().shift(1)
    return pd.DataFrame({"resistance": resistance, "support": support})


def volume_profile(df: pd.DataFrame, lookback: int = 50, bins: int = 20) -> dict:
    """Simplified volume profile over the trailing `lookback` bars.

    Buckets the typical price ((H+L+C)/3) of each bar into `bins` price bins
    and sums volume per bin. Returns the point of control (highest-volume
    price bin) plus the nearest high-volume bins above/below the last close,
    used as dynamic support/resistance.
    """
    window = df.tail(lookback)
    if len(window) < 2:
        return {"poc": None, "resistance": None, "support": None}

    typical = (window["high"] + window["low"] + window["close"]) / 3
    lo, hi = typical.min(), typical.max()
    if hi <= lo:
        return {"poc": float(typical.iloc[-1]), "resistance": None, "support": None}

    bin_edges = np.linspace(lo, hi, bins + 1)
    bin_idx = np.clip(np.digitize(typical, bin_edges) - 1, 0, bins - 1)
    vol_by_bin = np.zeros(bins)
    for idx, vol in zip(bin_idx, window["volume"]):
        vol_by_bin[idx] += vol

    poc_bin = int(np.argmax(vol_by_bin))
    poc_price = (bin_edges[poc_bin] + bin_edges[poc_bin + 1]) / 2

    last_close = window["close"].iloc[-1]
    above = [
        (bin_edges[i] + bin_edges[i + 1]) / 2
        for i in range(bins)
        if vol_by_bin[i] > 0 and (bin_edges[i] + bin_edges[i + 1]) / 2 > last_close
    ]
    below = [
        (bin_edges[i] + bin_edges[i + 1]) / 2
        for i in range(bins)
        if vol_by_bin[i] > 0 and (bin_edges[i] + bin_edges[i + 1]) / 2 < last_close
    ]

    return {
        "poc": float(poc_price),
        "resistance": float(min(above)) if above else None,
        "support": float(max(below)) if below else None,
    }

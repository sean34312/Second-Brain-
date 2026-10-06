"""Technical indicators (pure pandas, no TA-Lib required).

All indicators are causal: the value at row ``i`` only depends on rows
``<= i``, so they are safe to precompute once for backtesting.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing: seed with the simple mean of the first ``period``
    values, then avg = (prev_avg * (period - 1) + value) / period."""
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < period:
        return out
    start = valid[0] + period - 1
    out[start] = values[valid[0]: start + 1].mean()
    for k in range(start + 1, len(values)):
        out[k] = (out[k - 1] * (period - 1) + values[k]) / period
    return out


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing."""
    delta = close.diff().to_numpy(dtype=float)
    avg_gain = _wilder_smooth(np.where(np.isnan(delta), np.nan, np.clip(delta, 0, None)), period)
    avg_loss = _wilder_smooth(np.where(np.isnan(delta), np.nan, np.clip(-delta, 0, None)), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    # No losses in the window -> RSI 100; flat prices -> RSI 50.
    out = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, out)
    out = np.where((avg_loss == 0) & (avg_gain == 0), 50.0, out)
    return pd.Series(out, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    out = pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_hist": macd_line - signal_line},
        index=close.index,
    )
    # The first `slow + signal` values are dominated by the EMA seed; blank them out.
    out.iloc[: slow + signal - 1] = np.nan
    return out


def add_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Return a copy of OHLCV ``df`` with indicator columns added.

    ``cfg`` is a :class:`swing_bot.config.StrategyConfig`.
    """
    out = df.copy()
    close = out["close"]
    out["rsi"] = rsi(close, cfg.rsi_period)
    out = out.join(macd(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal))
    out["sma_fast"] = sma(close, cfg.sma_fast)
    out["sma_slow"] = sma(close, cfg.sma_slow)
    return out

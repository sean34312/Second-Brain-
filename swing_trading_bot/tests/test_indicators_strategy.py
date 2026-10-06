import numpy as np
import pandas as pd
import pytest

from swing_bot.config import StrategyConfig
from swing_bot.data import SyntheticDataProvider
from swing_bot.indicators import macd, rsi, sma
from swing_bot.strategy import Signal, SwingStrategy


def test_sma():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = sma(s, 3)
    assert out.isna().sum() == 2
    assert out.iloc[-1] == pytest.approx(4.0)


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert rsi(down, 14).iloc[-1] == pytest.approx(0.0)
    flat = pd.Series(np.full(40, 10.0))
    assert rsi(flat, 14).iloc[-1] == pytest.approx(50.0)
    noisy = pd.Series(100 + np.random.default_rng(0).normal(0, 1, 300).cumsum())
    r = rsi(noisy, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_rsi_matches_wilder_reference():
    # Classic example from Wilder / StockCharts (14-period).
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03,
              45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    r = rsi(pd.Series(closes), 14)
    # StockCharts' table (which rounds intermediate averages): 70.53, 66.32, 66.55, 69.41, 66.36, 57.97
    expected = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]
    assert r.iloc[14:].tolist() == pytest.approx(expected, abs=0.1)


def test_macd_hist_is_difference():
    s = pd.Series(100 + np.random.default_rng(1).normal(0, 1, 200).cumsum())
    m = macd(s).dropna()
    assert np.allclose(m["macd_hist"], m["macd"] - m["macd_signal"])


def test_insufficient_history_gives_no_signal():
    strat = SwingStrategy(StrategyConfig())
    df = strat.prepare(pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": np.linspace(10, 12, 30),
                                     "volume": 1.0}, index=pd.bdate_range("2024-01-01", periods=30)))
    assert strat.entry_signal("X", df).signal == Signal.NONE


def test_buy_signals_satisfy_every_rule():
    """Scan synthetic data and check each BUY bar against the documented rules."""
    c = StrategyConfig()
    strat = SwingStrategy(c)
    provider = SyntheticDataProvider()
    buys = 0
    for sym in ["AAA", "BBB", "CCC", "DDD", "EEE"]:
        df = strat.prepare(provider.history(sym, "2018-01-01", "2024-12-31"))
        for i in range(strat.min_bars, len(df)):
            sig = strat.entry_signal(sym, df, i)
            if sig.signal != Signal.BUY:
                continue
            buys += 1
            row, prev = df.iloc[i], df.iloc[i - 1]
            assert row.sma_fast > row.sma_slow and row.close > row.sma_slow
            assert df.rsi.iloc[i - c.rsi_lookback + 1: i + 1].min() <= c.rsi_oversold
            assert row.rsi > prev.rsi and row.rsi < c.rsi_overbought
            assert row.macd_hist > 0
            h = df.macd_hist.iloc[i - c.macd_cross_lookback: i + 1].to_numpy()
            assert any(h[k - 1] <= 0 < h[k] for k in range(1, len(h)))
    assert buys > 0, "expected the synthetic data to produce at least one setup"


def _exit_frame(**last):
    base = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0,
            "rsi": 55.0, "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.5, "sma_fast": 98.0, "sma_slow": 95.0}
    rows = [dict(base), {**base, **last}]
    return pd.DataFrame(rows, index=pd.bdate_range("2024-01-01", periods=2))


@pytest.mark.parametrize("last,needle", [
    ({"rsi": 75.0}, "RSI overbought"),
    ({"macd_hist": -0.1}, "MACD bearish cross"),
    ({"close": 94.0}, "trend break"),
    ({"close": 107.0, "rsi": 60.0}, "take profit"),
])
def test_exit_rules(last, needle):
    strat = SwingStrategy(StrategyConfig())
    sig = strat.exit_signal("X", _exit_frame(**last), entry_price=100.0, bars_held=3)
    assert sig.signal == Signal.SELL
    assert needle in sig.reason_text


def test_exit_max_holding_and_hold():
    strat = SwingStrategy(StrategyConfig(max_holding_days=10))
    assert strat.exit_signal("X", _exit_frame(), 100.0, bars_held=3).signal == Signal.HOLD
    sig = strat.exit_signal("X", _exit_frame(), 100.0, bars_held=10)
    assert sig.signal == Signal.SELL and "max holding" in sig.reason_text

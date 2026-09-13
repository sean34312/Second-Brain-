import numpy as np
import pandas as pd

from bot.config import BotConfig
from bot.strategy import MomentumBreakoutStrategy


class FakeFeed:
    """Stands in for MarketDataFeed so strategy tests don't touch the network."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def get_frame(self, symbol: str, interval: str) -> pd.DataFrame:
        return self.frames.get(interval, pd.DataFrame())


def make_df(closes, volumes=None, freq="5min"):
    n = len(closes)
    volumes = volumes or [1000] * n
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.3 for c in closes],
            "low": [c - 0.3 for c in closes],
            "close": closes,
            "volume": volumes,
        },
        index=pd.date_range("2024-01-01 09:30", periods=n, freq=freq),
    )


def bullish_setup_frames():
    # flat consolidation then a breakout bar with a volume spike
    flat = [100.0] * 40
    breakout = [103.0]
    closes_5m = flat + breakout
    volumes_5m = [1000] * 40 + [5000]
    df5 = make_df(closes_5m, volumes_5m, freq="5min")

    # 15-minute chart trending up so MACD is bullish on the higher timeframe too
    closes_15m = list(np.linspace(95, 103, 40))
    df15 = make_df(closes_15m, freq="15min")

    return {"5minute": df5, "15minute": df15}


def test_generates_buy_signal_on_breakout_with_confirmation():
    config = BotConfig()
    feed = FakeFeed(bullish_setup_frames())
    strategy = MomentumBreakoutStrategy(config)

    signal = strategy.evaluate("TEST", feed)

    assert signal is not None
    assert signal.side == "buy"
    assert signal.entry_price == 103.0
    assert signal.stop_price < signal.entry_price
    assert all(t > signal.entry_price for t in signal.targets)
    assert len(signal.targets) == len(signal.scale_out_fractions)


def test_no_signal_without_enough_history():
    config = BotConfig()
    feed = FakeFeed({"5minute": make_df([100.0] * 5), "15minute": make_df([100.0] * 5, freq="15min")})
    strategy = MomentumBreakoutStrategy(config)

    assert strategy.evaluate("TEST", feed) is None


def test_no_signal_without_breakout():
    config = BotConfig()
    flat = make_df([100.0] * 50, freq="5min")
    trend15 = make_df(list(np.linspace(95, 100, 40)), freq="15min")
    feed = FakeFeed({"5minute": flat, "15minute": trend15})
    strategy = MomentumBreakoutStrategy(config)

    assert strategy.evaluate("TEST", feed) is None


def test_stop_distance_is_tight():
    config = BotConfig()
    feed = FakeFeed(bullish_setup_frames())
    strategy = MomentumBreakoutStrategy(config)

    signal = strategy.evaluate("TEST", feed)

    assert signal is not None
    stop_distance_pct = (signal.entry_price - signal.stop_price) / signal.entry_price
    assert stop_distance_pct <= config.stop_loss_pct + 1e-9

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swing_bot.config import Config  # noqa: E402
from swing_bot.data import DataProvider  # noqa: E402


class FakeData(DataProvider):
    """In-memory provider: fixed bars + mutable quotes."""

    name = "fake"

    def __init__(self, bars: dict[str, pd.DataFrame], quotes: dict[str, float] | None = None):
        self.bars = bars
        self.quotes = dict(quotes or {s: float(df["close"].iloc[-1]) for s, df in bars.items()})

    def history(self, symbol, start=None, end=None):
        return self.bars[symbol]

    def latest_price(self, symbol):
        return self.quotes[symbol]


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(base_dir=tmp_path)
    c.bot.watchlist = ["AAA", "BBB", "CCC", "DDD"]
    c.bot.trade_only_market_hours = False
    c.validate()
    return c


def make_bars(closes, start="2024-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    s = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": s, "high": s * 1.01, "low": s * 0.99, "close": s, "volume": 1e6})

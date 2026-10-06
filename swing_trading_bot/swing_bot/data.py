"""Market data providers (daily OHLCV candles + latest quotes).

Every provider returns a DataFrame indexed by a tz-naive ``DatetimeIndex``
(one row per trading day) with float columns ``open, high, low, close, volume``.
"""

from __future__ import annotations

import functools
import logging
import zlib
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


class DataError(RuntimeError):
    pass


def _wrap_errors(fn):
    """Re-raise any provider failure (network, parsing, library bug) as DataError
    so the bot's error handling and alerting see a single exception type."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except DataError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DataError(f"{self.name}: {fn.__name__} failed: {type(e).__name__}: {e}") from e
    return wrapper


def _to_date(d: date | str | None, default: date) -> date:
    if d is None or d == "":
        return default
    if isinstance(d, str):
        return datetime.strptime(d, "%Y-%m-%d").date()
    return d


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"price data is missing columns: {missing}")
    df = df[COLUMNS].astype(float)
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["close"])


class DataProvider(ABC):
    name = "base"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for meth in ("history", "latest_price", "latest_prices"):
            if meth in cls.__dict__:
                setattr(cls, meth, _wrap_errors(cls.__dict__[meth]))

    @abstractmethod
    def history(self, symbol: str, start: date | str | None = None,
                end: date | str | None = None) -> pd.DataFrame:
        """Daily OHLCV bars between ``start`` and ``end`` (inclusive)."""

    @abstractmethod
    def latest_price(self, symbol: str) -> float:
        """Most recent trade price."""

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.latest_price(s) for s in symbols}

    def recent_history(self, symbol: str, days: int) -> pd.DataFrame:
        return self.history(symbol, date.today() - timedelta(days=days), date.today())


class RobinhoodDataProvider(DataProvider):
    """Uses ``robin_stocks`` (requires an authenticated session)."""

    name = "robinhood"

    def __init__(self):
        import robin_stocks.robinhood as rh  # imported lazily so paper/backtest work without it
        self.rh = rh

    def history(self, symbol, start=None, end=None):
        start_d = _to_date(start, date.today() - timedelta(days=365))
        end_d = _to_date(end, date.today())
        span = "year" if (date.today() - start_d).days <= 370 else "5year"
        raw = self.rh.stocks.get_stock_historicals(symbol, interval="day", span=span, bounds="regular")
        rows = [r for r in (raw or []) if r and r.get("begins_at")]
        if not rows:
            raise DataError(f"Robinhood returned no historical data for {symbol}")
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["begins_at"], utc=True)
        df = df.rename(columns={"open_price": "open", "high_price": "high",
                                "low_price": "low", "close_price": "close"})
        df = _normalize(df)
        return df.loc[pd.Timestamp(start_d): pd.Timestamp(end_d)]

    def latest_price(self, symbol):
        prices = self.rh.stocks.get_latest_price(symbol, includeExtendedHours=False)
        if not prices or prices[0] in (None, ""):
            raise DataError(f"Robinhood returned no quote for {symbol}")
        return float(prices[0])

    def latest_prices(self, symbols):
        prices = self.rh.stocks.get_latest_price(symbols, includeExtendedHours=False) or []
        out = {}
        for sym, p in zip(symbols, prices):
            if p not in (None, ""):
                out[sym] = float(p)
        missing = set(symbols) - set(out)
        if missing:
            raise DataError(f"Robinhood returned no quote for {sorted(missing)}")
        return out


class YFinanceDataProvider(DataProvider):
    """Free Yahoo Finance data - handy for paper trading and backtests without a login."""

    name = "yfinance"

    def __init__(self):
        try:
            import yfinance as yf
        except ImportError as e:  # pragma: no cover
            raise DataError("yfinance is not installed: pip install yfinance") from e
        self.yf = yf

    def history(self, symbol, start=None, end=None):
        start_d = _to_date(start, date.today() - timedelta(days=365))
        end_d = _to_date(end, date.today())
        df = self.yf.Ticker(symbol).history(start=start_d.isoformat(),
                                            end=(end_d + timedelta(days=1)).isoformat(),
                                            interval="1d", auto_adjust=True)
        if df is None or df.empty:
            raise DataError(f"yfinance returned no data for {symbol}")
        return _normalize(df)

    def latest_price(self, symbol):
        info = self.yf.Ticker(symbol).fast_info
        price = info.get("last_price") if hasattr(info, "get") else getattr(info, "last_price", None)
        if price is None or not np.isfinite(price):
            raise DataError(f"yfinance returned no quote for {symbol}")
        return float(price)


class CSVDataProvider(DataProvider):
    """Reads ``<data_dir>/<SYMBOL>.csv`` with columns date,open,high,low,close,volume."""

    name = "csv"

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    def _load(self, symbol: str) -> pd.DataFrame:
        path = self.data_dir / f"{symbol.upper()}.csv"
        if not path.exists():
            raise DataError(f"CSV file not found: {path}")
        df = pd.read_csv(path)
        df.columns = [c.lower().strip() for c in df.columns]
        date_col = next((c for c in ("date", "datetime", "timestamp", "begins_at") if c in df.columns), None)
        if date_col is None:
            raise DataError(f"{path} needs a 'date' column")
        df = df.set_index(pd.to_datetime(df.pop(date_col)))
        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"])
        return _normalize(df)

    def history(self, symbol, start=None, end=None):
        df = self._load(symbol)
        return df.loc[pd.Timestamp(_to_date(start, df.index[0].date())):
                      pd.Timestamp(_to_date(end, df.index[-1].date()))]

    def latest_price(self, symbol):
        return float(self._load(symbol)["close"].iloc[-1])


class SyntheticDataProvider(DataProvider):
    """Deterministic random-walk prices with trending regimes.

    Useful for demos, tests and offline experimentation. NOT real market data.
    """

    name = "synthetic"

    def __init__(self, seed: int = 7):
        self.seed = seed

    ORIGIN = date(2015, 1, 1)

    def _series(self, symbol: str) -> pd.DataFrame:
        # Always generate from a fixed origin so every call sees the same prices.
        idx = pd.bdate_range(self.ORIGIN, date.today())
        n = len(idx)
        rng = np.random.default_rng(self.seed + zlib.crc32(symbol.encode()))
        # Regime-switching drift so the series has real up and down trends.
        drift = np.repeat(rng.normal(0.0006, 0.0025, size=n // 40 + 1), 40)[:n]
        rets = drift + rng.normal(0, 0.017, size=n)
        close = 50 * (1 + rng.random()) * np.exp(np.cumsum(rets))
        open_ = np.empty(n)
        open_[0] = close[0]
        open_[1:] = close[:-1] * np.exp(rng.normal(0, 0.006, size=n - 1))  # overnight gaps
        spread = np.abs(rng.normal(0, 0.009, size=n))
        high = np.maximum(open_, close) * (1 + spread)
        low = np.minimum(open_, close) * (1 - spread)
        vol = rng.integers(1_000_000, 10_000_000, size=n).astype(float)
        return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol},
                            index=idx)

    def history(self, symbol, start=None, end=None):
        df = self._series(symbol)
        df = df.loc[pd.Timestamp(_to_date(start, self.ORIGIN)): pd.Timestamp(_to_date(end, date.today()))]
        if df.empty:
            raise DataError("empty date range")
        return df

    def latest_price(self, symbol):
        return float(self._series(symbol)["close"].iloc[-1])


def make_provider(source: str, *, csv_dir: str | Path = "data", robinhood_ready: bool = False) -> DataProvider:
    """Build a provider. ``auto`` = Robinhood if logged in, else yfinance."""
    if source == "auto":
        source = "robinhood" if robinhood_ready else "yfinance"
    if source == "robinhood":
        if not robinhood_ready:
            raise DataError("data_source 'robinhood' requires Robinhood credentials (see .env.example)")
        return RobinhoodDataProvider()
    if source == "yfinance":
        return YFinanceDataProvider()
    if source == "csv":
        return CSVDataProvider(csv_dir)
    if source == "synthetic":
        return SyntheticDataProvider()
    raise DataError(f"Unknown data source: {source}")

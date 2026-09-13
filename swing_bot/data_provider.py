"""Fetches account state and historical candlestick data from Robinhood."""
from __future__ import annotations

import logging

import pandas as pd
import robin_stocks.robinhood as rh

logger = logging.getLogger("swing_bot")


def get_historicals(symbol: str, interval: str = "day", span: str = "year") -> pd.DataFrame:
    """Return a DataFrame indexed by datetime with columns:
    open, high, low, close, volume."""
    raw = rh.stocks.get_stock_historicals(symbol, interval=interval, span=span)
    if not raw:
        raise RuntimeError(f"No historical data returned for {symbol}")

    df = pd.DataFrame(raw)
    df["begins_at"] = pd.to_datetime(df["begins_at"])
    df = df.set_index("begins_at").sort_index()
    for col in ("open_price", "high_price", "low_price", "close_price", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.rename(
        columns={
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
        }
    )
    return df[["open", "high", "low", "close", "volume"]]


def get_latest_price(symbol: str) -> float:
    quote = rh.stocks.get_latest_price(symbol, priceType=None, includeExtendedHours=True)
    if not quote or quote[0] is None:
        raise RuntimeError(f"Could not fetch latest price for {symbol}")
    return float(quote[0])


def get_account_equity() -> float:
    profile = rh.profiles.load_portfolio_profile()
    return float(profile["equity"])


def get_buying_power() -> float:
    profile = rh.profiles.load_account_profile()
    return float(profile["buying_power"])

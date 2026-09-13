"""Market data polling.

Robinhood has no public streaming/WebSocket market-data API for retail
accounts. What this module calls "real-time" is a short-interval REST poll
of ``robin_stocks`` historicals + quotes endpoints, fanned out across the
watchlist with a thread pool so 50-100 symbols can be refreshed inside one
scan cycle. That is fundamentally slower and noisier than a real tick
stream: expect data to be seconds old, not microseconds.

Each symbol keeps one OHLCV DataFrame per configured timeframe. On every
scan we patch the *last* bar with the latest trade price so indicators react
to intrabar moves, and on a slower cadence we re-pull the historicals
endpoint to true up the actually-closed candles.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import pandas as pd
import robin_stocks.robinhood as rh

from bot.config import BotConfig
from bot.logger_setup import setup_logging

logger = setup_logging(__name__)


def _historicals_to_df(raw: Optional[List[dict]]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    rows = []
    for bar in raw:
        if bar is None:
            continue
        rows.append(
            {
                "timestamp": pd.to_datetime(bar["begins_at"]),
                "open": float(bar["open_price"]),
                "high": float(bar["high_price"]),
                "low": float(bar["low_price"]),
                "close": float(bar["close_price"]),
                "volume": float(bar["volume"]),
            }
        )
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    return df


class MarketDataFeed:
    def __init__(self, config: BotConfig):
        self.config = config
        self.lock = threading.RLock()
        # data[symbol][interval] -> DataFrame
        self.data: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.latest_price: Dict[str, float] = {}
        self.latest_quote_time: Dict[str, float] = {}

    # ------------------------------------------------------------------
    def warm_up(self, symbols: List[str]) -> None:
        logger.info("Warming up historicals for %d symbols...", len(symbols))
        with ThreadPoolExecutor(max_workers=self.config.scan_max_workers) as pool:
            futures = {}
            for symbol in symbols:
                for tf in self.config.timeframes:
                    futures[pool.submit(self._fetch_historicals, symbol, tf.interval, tf.span)] = (
                        symbol,
                        tf.interval,
                    )
            for fut in as_completed(futures):
                symbol, interval = futures[fut]
                try:
                    df = fut.result()
                except Exception:
                    logger.exception("Failed warm-up fetch for %s/%s", symbol, interval)
                    continue
                with self.lock:
                    self.data.setdefault(symbol, {})[interval] = df
        logger.info("Warm-up complete.")

    def _fetch_historicals(self, symbol: str, interval: str, span: str) -> pd.DataFrame:
        raw = rh.stocks.get_stock_historicals(
            symbol, interval=interval, span=span, bounds="regular"
        )
        return _historicals_to_df(raw)

    # ------------------------------------------------------------------
    def refresh_latest_prices(self, symbols: List[str]) -> None:
        """Fast poll: latest trade price only, patched into the most recent bar
        of every timeframe so signals see the current price between candle
        closes."""
        with ThreadPoolExecutor(max_workers=self.config.scan_max_workers) as pool:
            futures = {pool.submit(self._fetch_price, s): s for s in symbols}
            for fut in as_completed(futures):
                symbol = futures[fut]
                try:
                    price = fut.result()
                except Exception:
                    logger.debug("Failed price fetch for %s", symbol, exc_info=True)
                    continue
                if price is None:
                    continue
                now = time.time()
                with self.lock:
                    self.latest_price[symbol] = price
                    self.latest_quote_time[symbol] = now
                    self._patch_last_bar(symbol, price)

    def _fetch_price(self, symbol: str) -> Optional[float]:
        quote = rh.stocks.get_latest_price(symbol, includeExtendedHours=False)
        if not quote or quote[0] is None:
            return None
        return float(quote[0])

    def _patch_last_bar(self, symbol: str, price: float) -> None:
        frames = self.data.get(symbol)
        if not frames:
            return
        for interval, df in frames.items():
            if df.empty:
                continue
            last_idx = df.index[-1]
            df.loc[last_idx, "close"] = price
            if price > df.loc[last_idx, "high"]:
                df.loc[last_idx, "high"] = price
            if price < df.loc[last_idx, "low"]:
                df.loc[last_idx, "low"] = price

    # ------------------------------------------------------------------
    def refresh_historicals(self, symbols: List[str]) -> None:
        """Slow poll: re-pull true candle data so the patched-in estimates above
        get reconciled against Robinhood's own aggregation."""
        with ThreadPoolExecutor(max_workers=self.config.scan_max_workers) as pool:
            futures = {}
            for symbol in symbols:
                for tf in self.config.timeframes:
                    futures[pool.submit(self._fetch_historicals, symbol, tf.interval, tf.span)] = (
                        symbol,
                        tf.interval,
                    )
            for fut in as_completed(futures):
                symbol, interval = futures[fut]
                try:
                    df = fut.result()
                except Exception:
                    logger.debug("Failed historicals refresh for %s/%s", symbol, interval, exc_info=True)
                    continue
                if df.empty:
                    continue
                with self.lock:
                    tf_bars = next(t.bars for t in self.config.timeframes if t.interval == interval)
                    self.data.setdefault(symbol, {})[interval] = df.tail(tf_bars)

    # ------------------------------------------------------------------
    def get_frame(self, symbol: str, interval: str) -> pd.DataFrame:
        with self.lock:
            return self.data.get(symbol, {}).get(interval, pd.DataFrame()).copy()

    def get_price(self, symbol: str) -> Optional[float]:
        with self.lock:
            return self.latest_price.get(symbol)

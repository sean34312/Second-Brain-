"""Orchestrates the scan -> signal -> risk-check -> order -> manage loop.

Read robinhood_day_trading_bot/README.md before running this against a real
account. In short: this is unofficial-API, ToS-violating, financially risky
software; it starts in paper-trading mode and stays there until you edit
.env yourself.
"""
from __future__ import annotations

import signal
import time
from datetime import date

from bot import auth
from bot.config import CONFIG, BotConfig
from bot.data_feed import MarketDataFeed
from bot.logger_setup import setup_logging
from bot.order_manager import OrderManager
from bot.position_manager import Position, PositionManager
from bot.risk_manager import RiskManager
from bot.scheduler import MarketClock
from bot.strategy import MomentumBreakoutStrategy

logger = setup_logging(__name__)


class TradingBot:
    def __init__(self, config: BotConfig = CONFIG):
        self.config = config
        self.clock = MarketClock(config)
        self.feed = MarketDataFeed(config)
        self.strategy = MomentumBreakoutStrategy(config)
        self.risk = RiskManager(config)
        self.orders = OrderManager(config)
        self.positions = PositionManager(config, self.orders, on_close=self._on_position_closed)
        self._stop_requested = False
        self._today_opened: dict[str, date] = {}
        self._last_historicals_refresh = 0.0

    # ------------------------------------------------------------------
    def _on_position_closed(self, pos: Position) -> None:
        opened_date = self._today_opened.pop(pos.symbol, None)
        if opened_date == date.today():
            self.risk.record_day_trade()
        if self.config.paper_trading:
            self.risk.apply_paper_fill_pnl(pos.realized_pnl)

    def request_stop(self) -> None:
        self._stop_requested = True

    # ------------------------------------------------------------------
    def run(self) -> None:
        # Market data still comes from a real, logged-in Robinhood session even
        # in paper-trading mode -- only order placement is simulated.
        auth.login()
        if self.config.paper_trading:
            logger.warning("Running in PAPER TRADING mode: no real orders will be placed.")
        else:
            logger.warning(
                "Running LIVE against a real Robinhood account. This uses an "
                "unofficial API, is aggressive by design, and can lose money "
                "quickly. Ctrl+C stops new entries but does not flatten open "
                "positions for you."
            )

        signal.signal(signal.SIGINT, lambda *_: self.request_stop())
        signal.signal(signal.SIGTERM, lambda *_: self.request_stop())

        self._wait_for_market_open()
        if self._stop_requested:
            return

        self.risk.mark_new_trading_day()
        self.feed.warm_up(self.config.watchlist)
        self._last_historicals_refresh = time.time()

        logger.info("Entering main loop.")
        while not self._stop_requested:
            loop_start = time.perf_counter()

            if self.clock.should_force_close():
                logger.warning("Force-close window reached; flattening all positions.")
                self.positions.close_all(self.feed.get_price, reason="scheduled market-close flatten")
                break

            if not self.clock.is_market_open():
                logger.info("Market closed; stopping.")
                break

            self.feed.refresh_latest_prices(self.config.watchlist)

            if time.time() - self._last_historicals_refresh > 60:
                self.feed.refresh_historicals(self.config.watchlist)
                self._last_historicals_refresh = time.time()

            for symbol in list(self.positions.positions.keys()):
                self.positions.poll_fills(symbol, self.feed.get_price(symbol))

            self.risk.check_drawdown_breach()
            if self.risk.trading_halted_for_day and self.config.flatten_on_drawdown_breach:
                self.positions.close_all(self.feed.get_price, reason="daily drawdown circuit breaker")

            if self.clock.entries_allowed():
                self._scan_for_entries()

            elapsed = time.perf_counter() - loop_start
            sleep_for = max(0.0, self.config.scan_interval_seconds - elapsed)
            time.sleep(sleep_for)

        auth.logout()
        logger.info("Bot stopped.")

    # ------------------------------------------------------------------
    def _scan_for_entries(self) -> None:
        if not self.risk.can_open_new_position(len(self.positions.positions)):
            return

        for symbol in self.config.watchlist:
            if self.positions.has_position(symbol):
                continue
            if not self.risk.can_open_new_position(len(self.positions.positions)):
                return

            signal_ = self.strategy.evaluate(symbol, self.feed)
            if not signal_:
                continue

            shares = self.risk.position_size_shares(signal_.entry_price)
            if shares <= 0:
                continue

            fill = self.orders.market_buy(symbol, shares, signal_.entry_price, signal_.reason)
            if not fill:
                continue

            self.positions.open_position(signal_, fill)
            self._today_opened[symbol] = date.today()

    # ------------------------------------------------------------------
    def _wait_for_market_open(self) -> None:
        if self.clock.is_market_open():
            return
        logger.info("Market not open yet. Waiting...")
        while not self._stop_requested and not self.clock.is_market_open():
            time.sleep(5)

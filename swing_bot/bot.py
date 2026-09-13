"""Main orchestrator: wires together data, indicators, strategy, risk
management, order execution, and position tracking into a polling loop."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from tabulate import tabulate

from . import auth, data_provider as dp
from .indicators import add_indicators
from .logger_setup import TradeLogger, setup_logging
from .order_manager import OrderManager
from .position_tracker import Position, PositionTracker
from .risk_manager import RiskManager
from .strategy import Signal, SwingStrategy

logger = logging.getLogger("swing_bot")


class SwingTradingBot:
    def __init__(self, config):
        config.validate()
        self.config = config
        setup_logging(config.log_dir)
        self.trade_log = TradeLogger(config.log_dir)
        self.strategy = SwingStrategy(config)
        self.risk = RiskManager(config)
        self.orders = OrderManager(config)
        self.positions = PositionTracker()
        self._stop = False

    def start(self) -> None:
        auth.login(self.config)
        mode = "LIVE" if self.orders.live else "DRY RUN (paper trading)"
        logger.info("Swing trading bot starting in %s mode", mode)
        logger.info("Watchlist: %s", ", ".join(self.config.watchlist))

    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop:
                try:
                    self.run_cycle()
                except Exception:
                    logger.exception("Error during trading cycle — will retry next cycle")
                time.sleep(self.config.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Interrupted by user, shutting down")
        finally:
            auth.logout()

    def run_cycle(self) -> None:
        try:
            equity = dp.get_account_equity()
            buying_power = dp.get_buying_power()
        except Exception as exc:
            logger.error("Account/API connection error: %s", exc)
            return

        signals = {}
        for symbol in self.config.watchlist:
            try:
                signals[symbol] = self._evaluate_symbol(symbol, equity, buying_power)
            except Exception:
                logger.exception("Failed to process %s — skipping this cycle", symbol)

        self.print_status(equity, buying_power, signals)

    def _evaluate_symbol(self, symbol: str, equity: float, buying_power: float) -> str:
        df = dp.get_historicals(symbol, self.config.historical_interval, self.config.historical_span)
        df = add_indicators(df, self.config)
        result = self.strategy.evaluate(df)
        current_price = float(df["close"].iloc[-1])

        position = self.positions.get(symbol)
        if position is not None:
            self._manage_open_position(position, current_price, result)
        elif result.signal is Signal.BUY:
            self._try_enter(symbol, current_price, result.reason, equity, buying_power)

        return f"{result.signal.value} ({result.reason})"

    def _manage_open_position(self, position: Position, current_price: float, result) -> None:
        exit_reason = None
        if position.should_stop_out(current_price):
            exit_reason = f"stop-loss hit at {current_price:.2f}"
        elif position.should_take_profit(current_price):
            exit_reason = f"take-profit hit at {current_price:.2f}"
        elif result.signal is Signal.SELL:
            exit_reason = result.reason

        if exit_reason:
            self._exit_position(position, current_price, exit_reason)

    def _try_enter(
        self, symbol: str, price: float, reason: str, equity: float, buying_power: float
    ) -> None:
        allowed, why_not = self.risk.can_open_new_position(self.positions.count(), equity)
        if not allowed:
            logger.info("Skipping BUY signal for %s: %s", symbol, why_not)
            return

        stop_price = self.risk.stop_loss_price(price)
        size = self.risk.position_size(equity, price, stop_price)
        if size.shares <= 0:
            logger.info("Skipping BUY signal for %s: %s", symbol, size.reason)
            return

        ok, why_not = self.risk.validate_buying_power(buying_power, size.shares, price)
        if not ok:
            logger.info("Skipping BUY signal for %s: %s", symbol, why_not)
            return

        result = self.orders.buy(symbol, size.shares, price)
        if not result.success:
            logger.error("BUY order failed for %s: %s", symbol, result.message)
            return

        position = Position(
            symbol=symbol,
            quantity=size.shares,
            entry_price=result.fill_price,
            stop_loss_price=stop_price,
            take_profit_price=self.risk.take_profit_price(price),
            order_id=result.order_id,
        )
        self.positions.open_position(position)
        self.trade_log.record(symbol, "BUY", size.shares, result.fill_price, reason)
        logger.info(
            "ENTERED %s: %d shares @ %.2f (stop %.2f, target %.2f) — %s",
            symbol,
            size.shares,
            result.fill_price,
            stop_price,
            position.take_profit_price,
            reason,
        )

    def _exit_position(self, position: Position, price: float, reason: str) -> None:
        result = self.orders.sell(position.symbol, position.quantity, price)
        if not result.success:
            logger.error("SELL order failed for %s: %s", position.symbol, result.message)
            return

        pnl = self.positions.close_position(position.symbol, result.fill_price)
        self.risk.record_realized_pnl(pnl)
        self.trade_log.record(position.symbol, "SELL", position.quantity, result.fill_price, reason, pnl)
        logger.info(
            "EXITED %s: %d shares @ %.2f, P&L %.2f — %s",
            position.symbol,
            position.quantity,
            result.fill_price,
            pnl,
            reason,
        )

    def print_status(self, equity: float, buying_power: float, signals: dict) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        open_positions = self.positions.all_open()

        pos_rows = [
            [
                sym,
                pos.quantity,
                f"{pos.entry_price:.2f}",
                f"{pos.stop_loss_price:.2f}",
                f"{pos.take_profit_price:.2f}",
            ]
            for sym, pos in open_positions.items()
        ]
        signal_rows = [[sym, sig] for sym, sig in signals.items()]

        print(f"\n=== Swing Bot Status — {now} ===")
        print(f"Equity: ${equity:,.2f}  Buying power: ${buying_power:,.2f}  "
              f"Realized P&L today: ${self.risk.daily_pnl():,.2f}")
        print(f"Open positions ({len(open_positions)}/{self.config.max_concurrent_positions}):")
        print(tabulate(pos_rows, headers=["Symbol", "Qty", "Entry", "Stop", "Target"], tablefmt="simple") or "  (none)")
        print("Watchlist signals:")
        print(tabulate(signal_rows, headers=["Symbol", "Signal"], tablefmt="simple"))

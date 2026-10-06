"""The live/paper trading loop."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime

import numpy as np
import pandas as pd

from .brokers import Account, Broker, BrokerError, BrokerPosition
from .config import Config
from .data import DataError, DataProvider
from .logging_setup import Alerter, TradeJournal
from .market_clock import CLOSE, now_et, today_et
from .risk import RiskManager
from .state import BotState, ManagedPosition
from .strategy import Signal, SignalResult, SwingStrategy

log = logging.getLogger(__name__)


class SwingBot:
    def __init__(self, cfg: Config, broker: Broker, data: DataProvider,
                 journal: TradeJournal, alerter: Alerter):
        self.cfg = cfg
        self.broker = broker
        self.data = data
        self.journal = journal
        self.alerter = alerter
        self.strategy = SwingStrategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk)
        self.state_path = cfg.path(cfg.bot.state_file)
        self.state = BotState.load(self.state_path)
        self._stop = threading.Event()
        self._bars_cache: dict[str, tuple[tuple, pd.DataFrame]] = {}
        self._warned_unmanaged: set[str] = set()

    # ================================================================ lifecycle
    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.warning("Received %s - finishing current step and shutting down...",
                        signal.Signals(signum).name)
            self._stop.set()
            signal.signal(signal.SIGINT, signal.SIG_DFL)  # a second Ctrl+C kills immediately

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def stop(self) -> None:
        self._stop.set()

    def run(self, once: bool = False) -> None:
        b = self.cfg.bot
        log.info("Starting swing bot | mode=%s broker=%s data=%s watchlist=%s",
                 b.mode.upper(), self.broker.name, self.data.name, ",".join(b.watchlist))
        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    self.cycle()
                    if failures:
                        self.alerter.info(f"API connection restored after {failures} failed cycle(s)")
                    failures = 0
                except (BrokerError, DataError) as e:
                    failures += 1
                    self.alerter.alert(f"API error ({failures}/{b.max_consecutive_api_failures}): {e}")
                    if failures >= b.max_consecutive_api_failures:
                        self.alerter.alert("Repeated API failures - API may be disconnected; "
                                           "attempting to re-authenticate", critical=True)
                        try:
                            self.broker.reconnect()
                        except Exception as re:  # noqa: BLE001
                            self.alerter.alert(f"Reconnect failed: {re}", critical=True)
                except Exception as e:  # noqa: BLE001 - keep the bot alive, but shout about it
                    log.exception("Unexpected error in trading cycle")
                    self.alerter.alert(f"Unexpected error: {type(e).__name__}: {e}", critical=True)
                finally:
                    self._save_state()
                if once:
                    break
                wait = b.poll_interval_seconds if not failures else min(b.poll_interval_seconds, 15 * 2 ** failures)
                self._stop.wait(wait)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("Shutting down...")
        if self.cfg.bot.flatten_on_shutdown and self.state.positions:
            log.warning("flatten_on_shutdown is enabled - closing all managed positions")
            try:
                quotes = self.broker.get_quotes(list(self.state.positions))
                for sym in list(self.state.positions):
                    self._exit_position(sym, quotes.get(sym), "shutdown flatten")
            except Exception as e:  # noqa: BLE001
                self.alerter.alert(f"Failed to flatten positions on shutdown: {e}", critical=True)
        self._save_state()
        if self.state.positions:
            protected = all(p.stop_order_id for p in self.state.positions.values())
            log.info("%d position(s) remain open%s", len(self.state.positions),
                     " (protected by resting stop orders)" if protected else
                     " - WARNING: not all are protected by broker stop orders while the bot is off")
        if self.cfg.bot.logout_on_shutdown:
            self.broker.disconnect()
        log.info("Shutdown complete. State saved to %s", self.state_path)

    def _save_state(self) -> None:
        try:
            self.state.save(self.state_path)
        except OSError as e:
            self.alerter.alert(f"Could not save state: {e}", critical=True)

    # ================================================================ one cycle
    def cycle(self) -> None:
        b = self.cfg.bot
        market_open = self.broker.is_market_open()
        account = self.broker.get_account()
        if self.state.roll_day(today_et().isoformat(), account.equity):
            log.info("New trading day %s - start-of-day equity $%.2f", self.state.day, account.equity)

        broker_positions = self.broker.get_positions()
        self._reconcile(broker_positions)

        symbols = sorted(set(b.watchlist) | set(self.state.positions))
        quotes = self.broker.get_quotes(symbols)
        frames = {s: self._bars(s) for s in symbols}
        entry_signals = {s: self._entry_signal(s, frames[s]) for s in b.watchlist}

        can_trade = market_open or not b.trade_only_market_hours
        if can_trade:
            self._check_daily_loss(account, quotes)
            self._manage_exits(quotes, frames)
            self._manage_entries(entry_signals, quotes, broker_positions)
            account = self.broker.get_account()
        else:
            log.info("Market closed - monitoring only")

        self.print_status(account, quotes, entry_signals, market_open)

    # ================================================================ data
    def _bars(self, symbol: str) -> pd.DataFrame | None:
        """Daily bars with indicators, completed bars only (today's bar is dropped
        until the close). Cached until the date or session phase changes."""
        now = now_et()
        after_close = now.time() >= CLOSE
        key = (now.date(), after_close)
        cached = self._bars_cache.get(symbol)
        if cached and cached[0] == key:
            return cached[1]
        try:
            df = self.data.recent_history(symbol, self.cfg.bot.history_days)
        except DataError as e:
            log.warning("No history for %s: %s", symbol, e)
            return None
        if not after_close:
            df = df[df.index.date < now.date()]
        df = self.strategy.prepare(df)
        self._bars_cache[symbol] = (key, df)
        return df

    def _entry_signal(self, symbol: str, df: pd.DataFrame | None) -> SignalResult:
        if df is None or df.empty:
            return SignalResult(symbol, Signal.NONE, ["no data"])
        return self.strategy.entry_signal(symbol, df)

    # ================================================================ reconciliation
    def _reconcile(self, broker_positions: dict[str, BrokerPosition]) -> None:
        """Sync managed positions with what the broker actually holds."""
        for sym, mp in list(self.state.positions.items()):
            held = broker_positions.get(sym)
            if held and held.quantity >= mp.quantity - 1e-6:
                continue
            if held:  # partially sold outside the bot
                log.warning("%s: broker holds %s shares but bot expected %s - adjusting",
                            sym, held.quantity, mp.quantity)
                mp.quantity = held.quantity
                continue
            # Position is gone: most likely the resting stop-loss filled.
            order = self.broker.get_order(mp.stop_order_id) if mp.stop_order_id else None
            if order and order.status == "filled" and order.avg_fill_price:
                self._record_exit(mp, order.avg_fill_price, mp.quantity, "broker stop-loss filled",
                                  order.order_id, "stop")
            else:
                self.alerter.alert(f"{sym} position closed outside the bot; removing from managed positions")
                self.state.positions.pop(sym, None)
                self.state.last_exit_dates[sym] = today_et().isoformat()
        for sym in broker_positions:
            if sym not in self.state.positions and sym not in self._warned_unmanaged:
                log.info("Ignoring unmanaged position in %s (not opened by this bot)", sym)
                self._warned_unmanaged.add(sym)

    # ================================================================ risk checks
    def _check_daily_loss(self, account: Account, quotes: dict[str, float]) -> None:
        if self.state.halted_today:
            return
        loss = self.risk.daily_loss_pct(self.state.day_start_equity, account.equity)
        if loss >= self.cfg.risk.daily_max_loss_pct:
            self.state.halted_today = True
            self.alerter.alert(
                f"Daily loss limit hit: equity ${account.equity:,.2f} is down {loss:.2f}% from "
                f"${self.state.day_start_equity:,.2f}. No new entries until tomorrow.", critical=True)
            if self.cfg.risk.flatten_on_daily_loss:
                for sym in list(self.state.positions):
                    self._exit_position(sym, quotes.get(sym), "daily loss limit flatten")

    # ================================================================ exits
    def _manage_exits(self, quotes: dict[str, float], frames: dict[str, pd.DataFrame | None]) -> None:
        tp = self.cfg.strategy.take_profit_pct
        for sym, mp in list(self.state.positions.items()):
            price = quotes.get(sym)
            if price is None:
                log.warning("No quote for open position %s - skipping exit checks", sym)
                continue
            if self.risk.stop_hit(price, mp.stop_price):
                self._exit_position(sym, price, f"stop-loss ({price:.2f} <= {mp.stop_price:.2f})")
                continue
            if tp > 0 and price >= mp.entry_price * (1 + tp / 100):
                self._exit_position(sym, price, f"take profit (+{tp:g}%)")
                continue
            df = frames.get(sym)
            if df is None or df.empty:
                continue
            bars_held = int(np.busday_count(mp.entry_date, today_et().isoformat()))
            sig = self.strategy.exit_signal(sym, df, mp.entry_price, bars_held)
            if sig.signal == Signal.SELL:
                self._exit_position(sym, price, sig.reason_text)

    def _exit_position(self, symbol: str, price: float | None, reason: str) -> None:
        mp = self.state.positions.get(symbol)
        if mp is None:
            return
        log.info("EXIT signal %s: %s", symbol, reason)
        o = self.cfg.orders
        # A resting stop order reserves the shares; cancel it before selling.
        if mp.stop_order_id:
            existing = self.broker.get_order(mp.stop_order_id)
            if existing and existing.status == "filled" and existing.avg_fill_price:
                self._record_exit(mp, existing.avg_fill_price, mp.quantity, "broker stop-loss filled",
                                  existing.order_id, "stop")
                return
            self.broker.cancel_order(mp.stop_order_id)
            mp.stop_order_id = None

        order_type = o.exit_order_type
        limit = round(price * (1 - o.limit_offset_pct / 100), 2) if order_type == "limit" and price else None
        if order_type == "limit" and limit is None:
            order_type = "market"
        result = self.broker.sell(symbol, mp.quantity, order_type, limit)
        if result.filled:
            if result.filled_qty < mp.quantity - 1e-6:
                mp.quantity -= result.filled_qty
                self._record_exit(mp, result.avg_fill_price, result.filled_qty, reason + " (partial)",
                                  result.order_id, order_type, keep_open=True)
                self.alerter.alert(f"{symbol}: exit only partially filled; {mp.quantity} shares remain")
                self._protect(mp)
            else:
                self._record_exit(mp, result.avg_fill_price, result.filled_qty, reason, result.order_id, order_type)
        else:
            self.alerter.alert(f"Exit order for {symbol} not filled ({result.status}: {result.message}); "
                               "re-arming stop-loss and retrying next cycle")
            self._protect(mp)

    def _record_exit(self, mp: ManagedPosition, price: float, qty: float, reason: str,
                     order_id: str | None, order_type: str, keep_open: bool = False) -> None:
        pnl = (price - mp.entry_price) * qty
        pnl_pct = (price / mp.entry_price - 1) * 100
        self.state.realized_pnl_today += pnl
        self.journal.record(symbol=mp.symbol, side="SELL", quantity=qty, price=price, order_type=order_type,
                            order_id=order_id, signal=reason, entry_price=mp.entry_price,
                            stop_price=mp.stop_price, pnl=pnl, pnl_pct=pnl_pct)
        self.alerter.info(f"SOLD {qty} {mp.symbol} @ ${price:.2f} | P&L ${pnl:+,.2f} ({pnl_pct:+.2f}%) | {reason}")
        if not keep_open:
            self.state.positions.pop(mp.symbol, None)
            self.state.last_exit_dates[mp.symbol] = today_et().isoformat()

    def _protect(self, mp: ManagedPosition) -> None:
        """Place a resting stop-loss order at the broker for ``mp`` if enabled."""
        if not self.cfg.orders.use_broker_stop_orders or mp.stop_order_id:
            return
        if mp.quantity != int(mp.quantity):
            log.warning("%s: fractional position - broker stop orders unsupported; "
                        "stop is enforced by the bot only while it runs", mp.symbol)
            return
        try:
            mp.stop_order_id = self.broker.place_stop_loss(mp.symbol, mp.quantity, mp.stop_price)
        except BrokerError as e:
            self.alerter.alert(f"Could not place stop-loss for {mp.symbol}: {e}. "
                               "Bot will still enforce the stop while running.", critical=True)

    # ================================================================ entries
    def _manage_entries(self, signals: dict[str, SignalResult], quotes: dict[str, float],
                        broker_positions: dict[str, BrokerPosition]) -> None:
        o, r = self.cfg.orders, self.cfg.risk
        for sym, sig in signals.items():
            if sig.signal != Signal.BUY or sym in self.state.positions or sym in broker_positions:
                continue
            allowed, why = self.risk.can_open_position(len(self.state.positions), self.state.halted_today)
            if not allowed:
                log.info("BUY signal for %s skipped: %s", sym, why)
                continue
            last_exit = self.state.last_exit_dates.get(sym)
            if last_exit and np.busday_count(last_exit, today_et().isoformat()) < r.reentry_cooldown_days:
                log.info("BUY signal for %s skipped: re-entry cooldown (exited %s)", sym, last_exit)
                continue
            quote = quotes.get(sym)
            if quote is None:
                continue
            if sig.close and quote > sig.close * (1 + o.max_entry_gap_pct / 100):
                log.info("BUY signal for %s skipped: price %.2f gapped > %.1f%% above signal close %.2f",
                         sym, quote, o.max_entry_gap_pct, sig.close)
                continue
            self._enter_position(sym, quote, sig)

    def _enter_position(self, symbol: str, quote: float, sig: SignalResult) -> None:
        o = self.cfg.orders
        account = self.broker.get_account()  # fresh buying power right before ordering
        limit = round(quote * (1 + o.limit_offset_pct / 100), 2) if o.entry_order_type == "limit" else None
        est_price = limit or quote
        size = self.risk.position_size(account.equity, est_price, account.buying_power)
        if not size.ok:
            log.info("BUY %s skipped: %s (equity $%.2f, buying power $%.2f)",
                     symbol, size.reason, account.equity, account.buying_power)
            return
        if size.notional > account.buying_power:
            log.warning("BUY %s skipped: insufficient buying power ($%.2f needed, $%.2f available)",
                        symbol, size.notional, account.buying_power)
            return
        log.info("ENTRY %s: %s %s @ ~%.2f (risk $%.2f, stop %.2f, %s) | %s", symbol, o.entry_order_type,
                 size.quantity, est_price, size.risk_amount, size.stop_price, size.reason, sig.reason_text)
        result = self.broker.buy(symbol, size.quantity, o.entry_order_type, limit)
        if not result.filled:
            log.info("BUY %s not filled (%s%s)", symbol, result.status,
                     f": {result.message}" if result.message else "")
            if result.status == "rejected":
                self.alerter.alert(f"BUY order for {symbol} rejected: {result.message}")
            return

        fill = result.avg_fill_price
        stop = self.risk.stop_price(fill)
        now = datetime.now().astimezone()
        mp = ManagedPosition(symbol=symbol, quantity=result.filled_qty, entry_price=fill, stop_price=stop,
                             entry_time=now.isoformat(timespec="seconds"), entry_date=today_et().isoformat(),
                             entry_reasons=sig.reason_text)
        self.state.positions[symbol] = mp
        self._save_state()  # persist immediately - a crash now must not orphan the position
        self.journal.record(symbol=symbol, side="BUY", quantity=result.filled_qty, price=fill,
                            order_type=o.entry_order_type, order_id=result.order_id, signal=sig.reason_text,
                            entry_price=fill, stop_price=stop)
        self.alerter.info(f"BOUGHT {result.filled_qty} {symbol} @ ${fill:.2f} | stop ${stop:.2f}")
        self._protect(mp)

    # ================================================================ status
    def print_status(self, account: Account, quotes: dict[str, float],
                     signals: dict[str, SignalResult], market_open: bool) -> None:
        print(self.format_status(account, quotes, signals, market_open), flush=True)

    def format_status(self, account: Account, quotes: dict[str, float],
                      signals: dict[str, SignalResult], market_open: bool) -> str:
        s = self.state
        day_pnl = account.equity - s.day_start_equity if s.day_start_equity else 0.0
        day_pct = day_pnl / s.day_start_equity * 100 if s.day_start_equity else 0.0
        lines = [
            "=" * 96,
            f" {now_et():%Y-%m-%d %H:%M:%S %Z} | mode {self.cfg.bot.mode.upper()} | "
            f"market {'OPEN' if market_open else 'CLOSED'}"
            f"{' | TRADING HALTED (daily loss limit)' if s.halted_today else ''}",
            f" Equity ${account.equity:,.2f} | Buying power ${account.buying_power:,.2f} | "
            f"Day P&L ${day_pnl:+,.2f} ({day_pct:+.2f}%) | Realized today ${s.realized_pnl_today:+,.2f}",
            "-" * 96,
            f" POSITIONS ({len(s.positions)}/{self.cfg.risk.max_positions})",
        ]
        if s.positions:
            lines.append(f"  {'SYM':<6}{'QTY':>9}{'ENTRY':>10}{'LAST':>10}{'STOP':>10}{'P&L $':>12}{'P&L %':>9}  SINCE")
            total = 0.0
            for sym, p in s.positions.items():
                last = quotes.get(sym)
                pnl = (last - p.entry_price) * p.quantity if last else 0.0
                pct = (last / p.entry_price - 1) * 100 if last else 0.0
                total += pnl
                lines.append(f"  {sym:<6}{p.quantity:>9g}{p.entry_price:>10.2f}"
                             f"{(last or 0):>10.2f}{p.stop_price:>10.2f}{pnl:>+12,.2f}{pct:>+8.2f}%  {p.entry_date}")
            lines.append(f"  {'Unrealized total':<55}{total:>+12,.2f}")
        else:
            lines.append("  (none)")
        lines += ["-" * 96, " WATCHLIST",
                  f"  {'SYM':<6}{'LAST':>10}{'RSI':>7}{'MACD-H':>9}{'SMA20':>10}{'SMA50':>10}  {'SIGNAL':<6} DETAIL"]
        def num(v, width, prec):
            return f"{v:>{width}.{prec}f}" if v is not None else f"{'-':>{width}}"

        for sym, sig in signals.items():
            label = "HELD" if sym in s.positions else sig.signal.value
            lines.append(f"  {sym:<6}{num(quotes.get(sym), 10, 2)}{num(sig.rsi, 7, 1)}"
                         f"{num(sig.macd_hist, 9, 3)}{num(sig.sma_fast, 10, 2)}{num(sig.sma_slow, 10, 2)}"
                         f"  {label:<6} {sig.reason_text[:60]}")
        lines.append("=" * 96)
        return "\n".join(lines)

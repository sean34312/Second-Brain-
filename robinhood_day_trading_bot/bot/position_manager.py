"""Tracks open positions and reconciles them against the resting stop/target
orders placed by OrderManager.

Scale-out is implemented as several independent limit-sell tranches plus one
stop-loss covering whatever quantity is still open. When a target tranche
fills, the stop is cancelled and re-placed for the smaller remaining
quantity -- note this leaves a brief (sub-second, but real) window where the
remaining shares are unprotected while the old stop is cancelled and the new
one submitted. That is an inherent limitation of Robinhood's API not
offering native one-cancels-other bracket orders.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import robin_stocks.robinhood as rh

from bot.config import BotConfig
from bot.logger_setup import setup_logging
from bot.order_manager import Fill, OrderManager, RestingOrder
from bot.strategy import Signal

logger = setup_logging(__name__)


@dataclass
class Tranche:
    order: RestingOrder
    quantity: int
    filled: bool = False


@dataclass
class Position:
    symbol: str
    entry_price: float
    total_quantity: int
    remaining_quantity: int
    opened_at: float = field(default_factory=time.time)
    stop_order: Optional[RestingOrder] = None
    targets: List[Tranche] = field(default_factory=list)
    realized_pnl: float = 0.0


class PositionManager:
    def __init__(
        self,
        config: BotConfig,
        order_manager: OrderManager,
        on_close: Optional[Callable[[Position], None]] = None,
    ):
        self.config = config
        self.orders = order_manager
        self.positions: Dict[str, Position] = {}
        self.on_close = on_close

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def open_position(self, signal: Signal, fill: Fill) -> Position:
        quantity = fill.quantity
        fractions = signal.scale_out_fractions
        target_quantities = self._split_quantity(quantity, fractions)

        pos = Position(
            symbol=signal.symbol,
            entry_price=fill.price,
            total_quantity=quantity,
            remaining_quantity=quantity,
        )

        pos.stop_order = self.orders.place_stop_loss(signal.symbol, quantity, signal.stop_price)

        for target_price, target_qty in zip(signal.targets, target_quantities):
            if target_qty <= 0:
                continue
            order = self.orders.place_limit_target(signal.symbol, target_qty, target_price)
            pos.targets.append(Tranche(order=order, quantity=target_qty))

        self.positions[signal.symbol] = pos
        logger.info(
            "OPENED %s: %d shares @ %.2f, stop=%.2f, targets=%s",
            signal.symbol, quantity, fill.price, signal.stop_price, signal.targets,
        )
        return pos

    @staticmethod
    def _split_quantity(total: int, fractions: List[float]) -> List[int]:
        raw = [math.floor(total * f) for f in fractions]
        allocated = sum(raw)
        # give any rounding remainder to the final (largest-target, smallest
        # weight) tranche so the full position is always covered
        remainder = total - allocated
        if raw:
            raw[-1] += remainder
        return raw

    # -- fill reconciliation -----------------------------------------------------
    def poll_fills(self, symbol: str, current_price: Optional[float]) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return

        if self.config.paper_trading:
            if current_price is not None:
                fills = self.orders.check_paper_resting_fills(symbol, current_price)
                for fill in fills:
                    self._apply_exit_fill(pos, fill, is_stop=(fill.order_id == getattr(pos.stop_order, "order_id", None)))
            return

        # live mode: poll each resting order's status
        if pos.stop_order and self._order_filled(pos.stop_order.order_id):
            fill = Fill(symbol, "sell", pos.remaining_quantity, pos.stop_order.price, pos.stop_order.order_id)
            self._apply_exit_fill(pos, fill, is_stop=True)
            return

        for tranche in pos.targets:
            if tranche.filled:
                continue
            if self._order_filled(tranche.order.order_id):
                fill = Fill(symbol, "sell", tranche.quantity, tranche.order.price, tranche.order.order_id)
                tranche.filled = True
                self._apply_exit_fill(pos, fill, is_stop=False)

    def _order_filled(self, order_id: str) -> bool:
        try:
            info = rh.orders.get_stock_order_info(order_id)
        except Exception:
            logger.exception("Failed to poll order %s", order_id)
            return False
        return bool(info and info.get("state") == "filled")

    def _apply_exit_fill(self, pos: Position, fill: Fill, is_stop: bool) -> None:
        pos.remaining_quantity -= fill.quantity
        pnl = (fill.price - pos.entry_price) * fill.quantity
        pos.realized_pnl += pnl
        logger.info(
            "%s %s x%d @ %.2f (pnl %.2f) remaining=%d",
            "STOP HIT" if is_stop else "TARGET HIT", pos.symbol, fill.quantity, fill.price, pnl,
            pos.remaining_quantity,
        )

        if is_stop:
            for tranche in pos.targets:
                if not tranche.filled:
                    self.orders.cancel_order(tranche.order.order_id)
            del self.positions[pos.symbol]
            if self.on_close:
                self.on_close(pos)
            return

        if pos.remaining_quantity <= 0:
            if pos.stop_order:
                self.orders.cancel_order(pos.stop_order.order_id)
            del self.positions[pos.symbol]
            if self.on_close:
                self.on_close(pos)
            return

        # a target tranche filled but shares remain: shrink the stop to cover
        # only what's left
        if pos.stop_order:
            self.orders.cancel_order(pos.stop_order.order_id)
            pos.stop_order = self.orders.place_stop_loss(
                pos.symbol, pos.remaining_quantity, pos.stop_order.price
            )

    # -- forced exits ------------------------------------------------------------
    def close_position(self, symbol: str, current_price: float, reason: str) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return
        if pos.stop_order:
            self.orders.cancel_order(pos.stop_order.order_id)
        for tranche in pos.targets:
            if not tranche.filled:
                self.orders.cancel_order(tranche.order.order_id)
        if pos.remaining_quantity > 0:
            fill = self.orders.market_sell(symbol, pos.remaining_quantity, current_price, reason)
            if fill:
                pos.realized_pnl += (fill.price - pos.entry_price) * fill.quantity
        logger.info("CLOSED %s (%s), total realized pnl %.2f", symbol, reason, pos.realized_pnl)
        del self.positions[symbol]
        if self.on_close:
            self.on_close(pos)

    def close_all(self, price_lookup, reason: str) -> None:
        for symbol in list(self.positions.keys()):
            price = price_lookup(symbol) or self.positions[symbol].entry_price
            self.close_position(symbol, price, reason)

    def total_unrealized_pnl(self, price_lookup) -> float:
        total = 0.0
        for symbol, pos in self.positions.items():
            price = price_lookup(symbol)
            if price is None:
                continue
            total += (price - pos.entry_price) * pos.remaining_quantity
        return total

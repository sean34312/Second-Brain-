"""Order execution.

Entries are market orders (speed over price, per the brief). Protection is
two resting orders placed immediately after entry so the position is
protected even if the bot's own loop stalls or the process dies:

  * a stop-loss (stop-market) order at the hard stop
  * one or more GTC limit sell orders at the scale-out profit targets

When either side fills, the bot cancels the other resting order(s) for that
slice of the position (a software emulation of an OCO bracket -- Robinhood's
retail API doesn't offer native OCO orders).

In paper-trading mode (the default) nothing touches the real brokerage:
fills are simulated against the live-polled price from the data feed and
everything is written to the trade blotter exactly like a real fill would
be, so the CSV log doubles as a backtest-of-one for the session.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import robin_stocks.robinhood as rh

from bot.config import BotConfig
from bot.logger_setup import TradeBlotter, setup_logging

logger = setup_logging(__name__)


@dataclass
class RestingOrder:
    order_id: str
    symbol: str
    side: str  # "stop" or "limit"
    quantity: int
    price: float  # stop price or limit price


@dataclass
class Fill:
    symbol: str
    side: str
    quantity: int
    price: float
    order_id: str


class OrderManager:
    def __init__(self, config: BotConfig):
        self.config = config
        self.blotter = TradeBlotter()
        # paper-mode simulated resting orders: order_id -> RestingOrder
        self._paper_resting: dict[str, RestingOrder] = {}

    # -- entries --------------------------------------------------------------
    def market_buy(self, symbol: str, quantity: int, reference_price: float, reason: str) -> Optional[Fill]:
        if quantity <= 0:
            return None

        if self.config.paper_trading:
            fill = Fill(symbol=symbol, side="buy", quantity=quantity, price=reference_price,
                        order_id=f"paper-{uuid.uuid4().hex[:8]}")
            self._log_fill(fill, reason)
            return fill

        order = rh.orders.order_buy_market(symbol, quantity)
        if not order or "id" not in order:
            logger.error("Buy order for %s rejected: %s", symbol, order)
            return None
        fill_price = self._poll_fill_price(order["id"], fallback=reference_price)
        fill = Fill(symbol=symbol, side="buy", quantity=quantity, price=fill_price, order_id=order["id"])
        self._log_fill(fill, reason)
        return fill

    def market_sell(self, symbol: str, quantity: int, reference_price: float, reason: str) -> Optional[Fill]:
        if quantity <= 0:
            return None

        if self.config.paper_trading:
            fill = Fill(symbol=symbol, side="sell", quantity=quantity, price=reference_price,
                        order_id=f"paper-{uuid.uuid4().hex[:8]}")
            self._log_fill(fill, reason)
            return fill

        order = rh.orders.order_sell_market(symbol, quantity)
        if not order or "id" not in order:
            logger.error("Sell order for %s rejected: %s", symbol, order)
            return None
        fill_price = self._poll_fill_price(order["id"], fallback=reference_price)
        fill = Fill(symbol=symbol, side="sell", quantity=quantity, price=fill_price, order_id=order["id"])
        self._log_fill(fill, reason)
        return fill

    # -- protective resting orders ---------------------------------------------
    def place_stop_loss(self, symbol: str, quantity: int, stop_price: float) -> RestingOrder:
        if self.config.paper_trading:
            order_id = f"paper-stop-{uuid.uuid4().hex[:8]}"
            resting = RestingOrder(order_id, symbol, "stop", quantity, stop_price)
            self._paper_resting[order_id] = resting
            return resting

        order = rh.orders.order_sell_stop_loss(symbol, quantity, stop_price, timeInForce="gtc")
        order_id = order.get("id", f"unknown-{uuid.uuid4().hex[:8]}") if order else f"unknown-{uuid.uuid4().hex[:8]}"
        return RestingOrder(order_id, symbol, "stop", quantity, stop_price)

    def place_limit_target(self, symbol: str, quantity: int, limit_price: float) -> RestingOrder:
        if self.config.paper_trading:
            order_id = f"paper-target-{uuid.uuid4().hex[:8]}"
            resting = RestingOrder(order_id, symbol, "limit", quantity, limit_price)
            self._paper_resting[order_id] = resting
            return resting

        order = rh.orders.order_sell_limit(symbol, quantity, limit_price, timeInForce="gtc")
        order_id = order.get("id", f"unknown-{uuid.uuid4().hex[:8]}") if order else f"unknown-{uuid.uuid4().hex[:8]}"
        return RestingOrder(order_id, symbol, "limit", quantity, limit_price)

    def cancel_order(self, order_id: str) -> None:
        if self.config.paper_trading:
            self._paper_resting.pop(order_id, None)
            return
        try:
            rh.orders.cancel_stock_order(order_id)
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)

    def check_paper_resting_fills(self, symbol: str, current_price: float) -> List[Fill]:
        """Paper-mode only: simulate stop/limit resting orders triggering against
        the latest polled price."""
        fills = []
        if not self.config.paper_trading:
            return fills
        for order_id, resting in list(self._paper_resting.items()):
            if resting.symbol != symbol:
                continue
            triggered = (
                (resting.side == "stop" and current_price <= resting.price)
                or (resting.side == "limit" and current_price >= resting.price)
            )
            if triggered:
                fill = Fill(symbol, "sell", resting.quantity, resting.price, order_id)
                self._log_fill(fill, f"paper {resting.side} order triggered")
                del self._paper_resting[order_id]
                fills.append(fill)
        return fills

    # -- helpers --------------------------------------------------------------
    def _poll_fill_price(self, order_id: str, fallback: float, timeout_s: float = 5.0) -> float:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            info = rh.orders.get_stock_order_info(order_id)
            if info and info.get("average_price"):
                return float(info["average_price"])
            time.sleep(0.25)
        logger.warning("Order %s not confirmed filled within %.1fs; using reference price %.2f",
                        order_id, timeout_s, fallback)
        return fallback

    def _log_fill(self, fill: Fill, reason: str) -> None:
        logger.info("FILL %s %s x%d @ %.2f (%s)", fill.side.upper(), fill.symbol, fill.quantity,
                    fill.price, reason)
        self.blotter.record(
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            order_type="market",
            reason=reason,
            paper_trading=self.config.paper_trading,
            order_id=fill.order_id,
        )

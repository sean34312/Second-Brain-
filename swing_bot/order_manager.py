"""Places buy/sell orders on Robinhood, or simulates them in dry-run mode.

Live orders are only ever sent when config.confirm_live_trading is True.
Otherwise every order is logged and returned as a simulated fill so the
rest of the bot (position tracking, P&L, logging) can be exercised safely
in paper-trading mode.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import robin_stocks.robinhood as rh

logger = logging.getLogger("swing_bot")


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    fill_price: float
    simulated: bool
    message: str = ""


class OrderManager:
    def __init__(self, config):
        self.config = config

    @property
    def live(self) -> bool:
        return bool(self.config.confirm_live_trading)

    def _limit_price(self, reference_price: float, side: str) -> float:
        slip = self.config.limit_price_slippage_pct
        if side == "buy":
            return round(reference_price * (1 + slip), 2)
        return round(reference_price * (1 - slip), 2)

    def buy(self, symbol: str, quantity: int, reference_price: float) -> OrderResult:
        return self._place(symbol, quantity, reference_price, side="buy")

    def sell(self, symbol: str, quantity: int, reference_price: float) -> OrderResult:
        return self._place(symbol, quantity, reference_price, side="sell")

    def _place(self, symbol: str, quantity: int, reference_price: float, side: str) -> OrderResult:
        order_type = self.config.order_type
        limit_price = self._limit_price(reference_price, side) if order_type == "limit" else None

        if not self.live:
            logger.info(
                "[DRY RUN] Would place %s %s order: %s x%d @ ~%.2f",
                order_type.upper(),
                side.upper(),
                symbol,
                quantity,
                limit_price or reference_price,
            )
            return OrderResult(
                success=True,
                order_id=None,
                fill_price=limit_price or reference_price,
                simulated=True,
                message="dry run — no live order sent",
            )

        try:
            if order_type == "market":
                fn = rh.orders.order_buy_market if side == "buy" else rh.orders.order_sell_market
                resp = fn(symbol, quantity)
            else:
                fn = rh.orders.order_buy_limit if side == "buy" else rh.orders.order_sell_limit
                resp = fn(symbol, quantity, limit_price)

            if resp and "id" in resp:
                logger.info("LIVE %s order placed: %s x%d (order id %s)", side.upper(), symbol, quantity, resp["id"])
                return OrderResult(
                    success=True,
                    order_id=resp["id"],
                    fill_price=limit_price or reference_price,
                    simulated=False,
                )
            logger.error("Order response missing id for %s %s: %s", side, symbol, resp)
            return OrderResult(False, None, 0.0, simulated=False, message=str(resp))
        except Exception as exc:  # noqa: BLE001 - surface any API failure to the caller
            logger.exception("Order placement failed for %s %s %s", side, symbol, quantity)
            return OrderResult(False, None, 0.0, simulated=False, message=str(exc))

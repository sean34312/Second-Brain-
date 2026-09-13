"""In-memory tracking of open swing positions and realized/unrealized P&L."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional


@dataclass
class Position:
    symbol: str
    quantity: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: Optional[str] = None

    def unrealized_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price

    def should_stop_out(self, current_price: float) -> bool:
        return current_price <= self.stop_loss_price

    def should_take_profit(self, current_price: float) -> bool:
        return current_price >= self.take_profit_price


class PositionTracker:
    def __init__(self):
        self._positions: Dict[str, Position] = {}
        self._realized_pnl_total: float = 0.0
        self._closed_trades: list[dict] = []

    def open_position(self, position: Position) -> None:
        self._positions[position.symbol] = position

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def all_open(self) -> Dict[str, Position]:
        return dict(self._positions)

    def count(self) -> int:
        return len(self._positions)

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Remove the position and return the realized P&L."""
        pos = self._positions.pop(symbol, None)
        if pos is None:
            return 0.0
        pnl = (exit_price - pos.entry_price) * pos.quantity
        self._realized_pnl_total += pnl
        self._closed_trades.append(
            {
                "symbol": symbol,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "quantity": pos.quantity,
                "pnl": pnl,
                "entry_time": pos.entry_time,
                "exit_time": datetime.now(timezone.utc),
            }
        )
        return pnl

    def total_unrealized_pnl(self, current_prices: Dict[str, float]) -> float:
        return sum(
            pos.unrealized_pnl(current_prices[sym])
            for sym, pos in self._positions.items()
            if sym in current_prices
        )

    @property
    def realized_pnl_total(self) -> float:
        return self._realized_pnl_total

    @property
    def closed_trades(self) -> list[dict]:
        return list(self._closed_trades)

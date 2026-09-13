"""Logging setup: a console+file logger for operational messages, and a
dedicated CSV trade log for entries/exits/P&L."""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

TRADE_LOG_FIELDS = [
    "timestamp",
    "symbol",
    "side",
    "quantity",
    "price",
    "signal_reason",
    "realized_pnl",
]


def setup_logging(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("swing_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


class TradeLogger:
    """Appends structured trade records to a CSV file for later analysis."""

    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, "trades.csv")
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS).writeheader()

    def record(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        signal_reason: str,
        realized_pnl: float | None = None,
    ) -> None:
        with open(self.path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            writer.writerow(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": price,
                    "signal_reason": signal_reason,
                    "realized_pnl": "" if realized_pnl is None else round(realized_pnl, 2),
                }
            )

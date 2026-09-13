"""Logging setup: console + rotating file log + a CSV trade blotter."""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

from bot.config import CONFIG


def setup_logging(name: str = "bot") -> logging.Logger:
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(CONFIG.log_dir, "bot.log"), maxBytes=10_000_000, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


class TradeBlotter:
    """Appends every simulated or real order to a CSV for after-the-fact review."""

    FIELDS = [
        "timestamp", "symbol", "side", "quantity", "price", "order_type",
        "reason", "paper_trading", "order_id",
    ]

    def __init__(self, path: str | None = None):
        os.makedirs(CONFIG.log_dir, exist_ok=True)
        self.path = path or os.path.join(CONFIG.log_dir, "trades.csv")
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def record(self, **kwargs) -> None:
        row = {field: kwargs.get(field, "") for field in self.FIELDS}
        row["timestamp"] = datetime.utcnow().isoformat()
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writerow(row)

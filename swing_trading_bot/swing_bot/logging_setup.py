"""Logging, trade journal (CSV) and alerting."""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-20s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    """Timestamped logs to the console and to a rotating ``bot.log`` file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(log_dir / "bot.log", maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # Third-party libraries are chatty at INFO/DEBUG.
    for noisy in ("urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class TradeJournal:
    """Append-only CSV of every order fill (entries and exits)."""

    FIELDS = ["timestamp", "mode", "symbol", "side", "quantity", "price", "order_type", "order_id",
              "signal", "entry_price", "stop_price", "pnl", "pnl_pct", "notes"]

    def __init__(self, path: Path, mode: str):
        self.path = path
        self.mode = mode
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def record(self, **row) -> None:
        row.setdefault("timestamp", datetime.now().astimezone().isoformat(timespec="seconds"))
        row.setdefault("mode", self.mode)
        for k in ("price", "entry_price", "stop_price", "pnl"):
            if isinstance(row.get(k), float):
                row[k] = round(row[k], 4)
        if isinstance(row.get("pnl_pct"), float):
            row["pnl_pct"] = round(row["pnl_pct"], 3)
        with self.path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore").writerow(row)
        log.info("TRADE %s", {k: v for k, v in row.items() if v not in (None, "")})


class Alerter:
    """Logs alerts at ERROR/CRITICAL and optionally POSTs them to a webhook
    (Discord / Slack incoming-webhook compatible). Duplicate alerts are
    suppressed for ``cooldown`` seconds so an outage doesn't spam you."""

    def __init__(self, webhook_url: str | None = None, cooldown: float = 300):
        self.webhook_url = webhook_url
        self.cooldown = cooldown
        self._last_sent: dict[str, float] = {}

    def alert(self, message: str, critical: bool = False) -> None:
        level = logging.CRITICAL if critical else logging.ERROR
        log.log(level, "ALERT: %s", message)
        now = time.monotonic()
        if now - self._last_sent.get(message, -1e9) < self.cooldown:
            return
        self._last_sent[message] = now
        if not self.webhook_url:
            return
        try:
            import requests
            text = f"[swing-bot] {'CRITICAL' if critical else 'ERROR'}: {message}"
            requests.post(self.webhook_url, json={"content": text, "text": text}, timeout=10)
        except Exception as e:  # noqa: BLE001 - alerting must never crash the bot
            log.warning("Failed to deliver webhook alert: %s", e)

    def info(self, message: str) -> None:
        """Non-error notification (trade fills etc.) - logged, and sent to the webhook if set."""
        log.info("NOTICE: %s", message)
        if self.webhook_url:
            try:
                import requests
                requests.post(self.webhook_url, json={"content": message, "text": message}, timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("Failed to deliver webhook notice: %s", e)

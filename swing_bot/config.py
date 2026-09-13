"""Configuration loading for the swing trading bot.

All tunables come from environment variables (optionally loaded from a
.env file) so credentials and risk parameters never need to be hard-coded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


def _get_list(name: str, default: List[str]) -> List[str]:
    val = os.getenv(name)
    if not val:
        return default
    return [s.strip().upper() for s in val.split(",") if s.strip()]


@dataclass
class BotConfig:
    # Credentials (never logged or printed)
    rh_username: str = field(default_factory=lambda: os.getenv("RH_USERNAME", ""))
    rh_password: str = field(default_factory=lambda: os.getenv("RH_PASSWORD", ""))
    rh_totp_secret: str = field(default_factory=lambda: os.getenv("RH_TOTP_SECRET", ""))

    # Safety switch: live orders are refused unless this is explicitly true.
    confirm_live_trading: bool = field(default_factory=lambda: _get_bool("CONFIRM_LIVE_TRADING", False))

    # Watchlist
    watchlist: List[str] = field(default_factory=lambda: _get_list("WATCHLIST", ["AAPL", "MSFT", "NVDA"]))

    # Indicators
    rsi_period: int = field(default_factory=lambda: _get_int("RSI_PERIOD", 14))
    rsi_oversold: float = field(default_factory=lambda: _get_float("RSI_OVERSOLD", 30))
    rsi_overbought: float = field(default_factory=lambda: _get_float("RSI_OVERBOUGHT", 70))
    macd_fast: int = field(default_factory=lambda: _get_int("MACD_FAST", 12))
    macd_slow: int = field(default_factory=lambda: _get_int("MACD_SLOW", 26))
    macd_signal: int = field(default_factory=lambda: _get_int("MACD_SIGNAL", 9))
    sma_short: int = field(default_factory=lambda: _get_int("SMA_SHORT", 20))
    sma_long: int = field(default_factory=lambda: _get_int("SMA_LONG", 50))

    # Risk management
    risk_per_trade_pct: float = field(default_factory=lambda: _get_float("RISK_PER_TRADE_PCT", 0.01))
    stop_loss_pct: float = field(default_factory=lambda: _get_float("STOP_LOSS_PCT", 0.02))
    take_profit_pct: float = field(default_factory=lambda: _get_float("TAKE_PROFIT_PCT", 0.06))
    max_concurrent_positions: int = field(default_factory=lambda: _get_int("MAX_CONCURRENT_POSITIONS", 3))
    daily_loss_limit_pct: float = field(default_factory=lambda: _get_float("DAILY_LOSS_LIMIT_PCT", 0.03))

    # Orders
    order_type: str = field(default_factory=lambda: os.getenv("ORDER_TYPE", "limit").lower())
    limit_price_slippage_pct: float = field(default_factory=lambda: _get_float("LIMIT_PRICE_SLIPPAGE_PCT", 0.001))

    # Data
    historical_interval: str = field(default_factory=lambda: os.getenv("HISTORICAL_INTERVAL", "day"))
    historical_span: str = field(default_factory=lambda: os.getenv("HISTORICAL_SPAN", "year"))

    # Loop
    poll_interval_seconds: int = field(default_factory=lambda: _get_int("POLL_INTERVAL_SECONDS", 300))

    # Logging
    log_dir: str = field(default_factory=lambda: os.getenv("LOG_DIR", "logs"))

    def validate(self) -> None:
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"ORDER_TYPE must be 'market' or 'limit', got {self.order_type!r}")
        if not (0 < self.risk_per_trade_pct <= 0.05):
            raise ValueError("RISK_PER_TRADE_PCT should be a small fraction, e.g. 0.01 for 1%")
        if self.max_concurrent_positions <= 0:
            raise ValueError("MAX_CONCURRENT_POSITIONS must be positive")
        if not self.watchlist:
            raise ValueError("WATCHLIST must contain at least one symbol")

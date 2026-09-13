"""Central configuration for the day trading bot.

Every risk knob lives here so the aggressive parameters the strategy asks
for (tight stops, 2-5% position sizing, margin, etc.) are visible in one
place instead of scattered through the codebase. Read the README's "Risk"
section before changing PAPER_TRADING or ENFORCE_PDT_RULE.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


DEFAULT_WATCHLIST: List[str] = [
    # Large-cap, high-liquidity, high-average-volume names chosen for tight
    # spreads (this strategy cannot tolerate wide spreads at 1-2% targets).
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "GOOGL", "META", "NFLX",
    "AVGO", "CRM", "ADBE", "INTC", "MU", "QCOM", "ORCL", "CSCO", "IBM",
    "UBER", "ABNB", "PLTR", "SNOW", "SHOP", "SQ", "PYPL", "COIN", "MARA",
    "RIOT", "SOFI", "F", "GM", "NIO", "RIVN", "LCID", "BA", "DIS", "NKE",
    "SBUX", "MCD", "WMT", "TGT", "COST", "HD", "LOW", "XOM", "CVX", "OXY",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "PFE", "MRNA", "JNJ",
    "UNH", "CVS", "T", "VZ", "TMUS", "SPY", "QQQ", "IWM", "DIA", "XLF",
    "XLE", "XLK", "SMH", "ARKK", "GME", "AMC", "DKNG", "ROKU", "SNAP",
    "PINS", "ZM", "DOCU", "CRWD", "PANW", "NET", "DDOG", "MDB", "ABNB",
]


@dataclass
class TimeframeConfig:
    interval: str  # robin_stocks historicals interval string
    span: str
    bars: int  # how many bars to keep in the rolling window


@dataclass
class BotConfig:
    # --- Safety -----------------------------------------------------
    paper_trading: bool = field(default_factory=lambda: _bool_env("PAPER_TRADING", True))
    enforce_pdt_rule: bool = field(default_factory=lambda: _bool_env("ENFORCE_PDT_RULE", True))
    pdt_equity_threshold: float = field(default_factory=lambda: _float_env("PDT_EQUITY_THRESHOLD", 25_000.0))
    max_day_trades_per_5_days: int = field(default_factory=lambda: _int_env("MAX_DAY_TRADES_PER_5_DAYS", 3))

    # --- Watchlist / scanning ----------------------------------------
    watchlist: List[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    scan_interval_seconds: float = field(default_factory=lambda: _float_env("SCAN_INTERVAL_SECONDS", 5.0))
    scan_max_workers: int = field(default_factory=lambda: _int_env("SCAN_MAX_WORKERS", 16))

    # --- Indicators ----------------------------------------------------
    stochastic_k_period: int = 5
    stochastic_k_slowing: int = 3
    stochastic_d_period: int = 3
    stochastic_oversold: float = 20.0
    stochastic_overbought: float = 80.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    volume_spike_multiple: float = field(default_factory=lambda: _float_env("VOLUME_SPIKE_MULTIPLE", 2.0))
    breakout_lookback_bars: int = field(default_factory=lambda: _int_env("BREAKOUT_LOOKBACK_BARS", 20))

    # --- Position sizing / leverage ------------------------------------
    position_size_pct: float = field(default_factory=lambda: _float_env("POSITION_SIZE_PCT", 0.03))
    min_position_size_pct: float = field(default_factory=lambda: _float_env("MIN_POSITION_SIZE_PCT", 0.02))
    max_position_size_pct: float = field(default_factory=lambda: _float_env("MAX_POSITION_SIZE_PCT", 0.05))
    use_margin: bool = field(default_factory=lambda: _bool_env("USE_MARGIN", False))
    leverage: float = field(default_factory=lambda: _float_env("LEVERAGE", 1.0))
    max_concurrent_positions: int = field(default_factory=lambda: _int_env("MAX_CONCURRENT_POSITIONS", 10))

    # --- Exits -----------------------------------------------------------
    stop_loss_pct: float = field(default_factory=lambda: _float_env("STOP_LOSS_PCT", 0.0075))
    profit_targets: List[float] = field(default_factory=lambda: [0.01, 0.015, 0.025])
    profit_scale_out_fractions: List[float] = field(default_factory=lambda: [0.5, 0.25, 0.25])
    atr_stop_multiple: float = field(default_factory=lambda: _float_env("ATR_STOP_MULTIPLE", 1.5))

    # --- Risk circuit breakers -------------------------------------------
    max_daily_drawdown_pct: float = field(default_factory=lambda: _float_env("MAX_DAILY_DRAWDOWN_PCT", 0.03))
    flatten_on_drawdown_breach: bool = field(default_factory=lambda: _bool_env("FLATTEN_ON_DRAWDOWN_BREACH", True))

    # --- Market hours (US/Eastern) ---------------------------------------
    market_open: str = field(default_factory=lambda: os.getenv("MARKET_OPEN", "09:30"))
    no_new_entries_after: str = field(default_factory=lambda: os.getenv("NO_NEW_ENTRIES_AFTER", "15:40"))
    force_close_at: str = field(default_factory=lambda: os.getenv("FORCE_CLOSE_AT", "15:45"))
    market_close: str = field(default_factory=lambda: os.getenv("MARKET_CLOSE", "16:00"))
    timezone: str = field(default_factory=lambda: os.getenv("BOT_TIMEZONE", "America/New_York"))

    # --- Timeframes used for signal confirmation --------------------------
    timeframes: List[TimeframeConfig] = field(default_factory=lambda: [
        TimeframeConfig(interval="5minute", span="day", bars=150),
        TimeframeConfig(interval="15minute", span="week", bars=100),
        TimeframeConfig(interval="hour", span="month", bars=100),
    ])

    # --- Persistence -------------------------------------------------------
    state_dir: str = field(default_factory=lambda: os.getenv("BOT_STATE_DIR", "state"))
    log_dir: str = field(default_factory=lambda: os.getenv("BOT_LOG_DIR", "logs"))


CONFIG = BotConfig()

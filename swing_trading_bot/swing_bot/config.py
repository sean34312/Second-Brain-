"""Configuration loading and validation.

Strategy/risk settings live in a YAML file (``config.yaml``). Secrets
(Robinhood credentials, webhook URLs) are read ONLY from environment
variables and are never written to disk by this project.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


@dataclass
class StrategyConfig:
    # RSI
    rsi_period: int = 14
    rsi_oversold: float = 40.0
    rsi_overbought: float = 70.0
    rsi_lookback: int = 10  # bars in which RSI must have touched oversold
    # MACD
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_cross_lookback: int = 5  # bars in which a bullish MACD cross must have occurred
    # Moving averages
    sma_fast: int = 20
    sma_slow: int = 50
    require_price_above_slow_sma: bool = True
    # Exits (stop-loss lives in RiskConfig)
    exit_on_rsi_overbought: bool = True
    exit_on_macd_bearish_cross: bool = True
    exit_on_trend_break: bool = True
    take_profit_pct: float = 6.0  # 0 disables
    max_holding_days: int = 20  # trading days, 0 disables


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0  # % of equity risked per trade
    stop_loss_pct: float = 2.5  # % below entry price
    max_positions: int = 3
    daily_max_loss_pct: float = 3.0  # halt new entries once equity is down this much today
    flatten_on_daily_loss: bool = False  # also close all positions when limit is hit
    max_position_pct: float = 25.0  # cap a single position's notional at % of equity
    allow_fractional: bool = False
    reentry_cooldown_days: int = 1  # days to wait before re-entering a symbol after an exit


@dataclass
class OrderConfig:
    entry_order_type: str = "limit"  # "market" or "limit"
    exit_order_type: str = "market"  # "market" or "limit"
    limit_offset_pct: float = 0.2  # buy limit = quote * (1 + x%), sell limit = quote * (1 - x%)
    order_timeout_seconds: int = 120  # cancel unfilled orders after this long
    time_in_force: str = "gfd"  # "gfd" (good for day) or "gtc"
    use_broker_stop_orders: bool = True  # place a resting stop order at the broker after entry
    max_entry_gap_pct: float = 2.0  # skip entry if quote is this far above the signal bar's close


@dataclass
class BotConfig:
    mode: str = "paper"  # "paper" or "live"
    watchlist: list[str] = field(default_factory=lambda: ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"])
    poll_interval_seconds: int = 300
    trade_only_market_hours: bool = True
    data_source: str = "auto"  # "auto", "robinhood", "yfinance", "csv", "synthetic"
    csv_data_dir: str = "data"
    history_days: int = 365
    paper_starting_cash: float = 25_000.0
    paper_slippage_pct: float = 0.05
    log_dir: str = "logs"
    trade_log_file: str = "logs/trades.csv"
    state_file: str = "state/bot_state.json"
    paper_account_file: str = "state/paper_account.json"
    max_consecutive_api_failures: int = 3
    logout_on_shutdown: bool = False
    flatten_on_shutdown: bool = False


@dataclass
class BacktestConfig:
    start: str = "2021-01-01"
    end: str = ""  # empty = today
    initial_capital: float = 25_000.0
    slippage_pct: float = 0.05
    commission_per_trade: float = 0.0
    data_source: str = "yfinance"  # "yfinance", "csv", "synthetic", "robinhood"
    output_dir: str = "backtest_results"


@dataclass
class Credentials:
    username: str | None
    password: str | None
    mfa_secret: str | None
    alert_webhook_url: str | None

    @property
    def has_robinhood(self) -> bool:
        return bool(self.username and self.password)

    @classmethod
    def from_env(cls) -> "Credentials":
        def env(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        return cls(
            username=env("ROBINHOOD_USERNAME"),
            password=env("ROBINHOOD_PASSWORD"),
            mfa_secret=env("ROBINHOOD_MFA_SECRET"),
            alert_webhook_url=env("ALERT_WEBHOOK_URL"),
        )

    def __repr__(self) -> str:  # never leak secrets into logs
        return f"Credentials(username={'set' if self.username else 'unset'}, password=***)"


@dataclass
class Config:
    bot: BotConfig = field(default_factory=BotConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    orders: OrderConfig = field(default_factory=OrderConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    base_dir: Path = field(default_factory=Path.cwd)

    def path(self, relative: str) -> Path:
        """Resolve a configured path relative to the config file's directory."""
        p = Path(relative)
        return p if p.is_absolute() else self.base_dir / p

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("base_dir")
        return d

    def validate(self) -> None:
        b, s, r, o = self.bot, self.strategy, self.risk, self.orders
        errors: list[str] = []
        if b.mode not in ("paper", "live"):
            errors.append("bot.mode must be 'paper' or 'live'")
        if not b.watchlist:
            errors.append("bot.watchlist must contain at least one symbol")
        if b.data_source not in ("auto", "robinhood", "yfinance", "csv", "synthetic"):
            errors.append("bot.data_source is invalid")
        if b.poll_interval_seconds < 10:
            errors.append("bot.poll_interval_seconds must be >= 10 (avoid hammering the API)")
        if not 0 < s.rsi_oversold < s.rsi_overbought < 100:
            errors.append("strategy: require 0 < rsi_oversold < rsi_overbought < 100")
        if not 0 < s.macd_fast < s.macd_slow:
            errors.append("strategy: require 0 < macd_fast < macd_slow")
        if not 0 < s.sma_fast < s.sma_slow:
            errors.append("strategy: require 0 < sma_fast < sma_slow")
        if s.rsi_lookback < 1 or s.macd_cross_lookback < 1:
            errors.append("strategy: lookbacks must be >= 1")
        if not 0 < r.risk_per_trade_pct <= 5:
            errors.append("risk.risk_per_trade_pct must be in (0, 5]")
        if not 0 < r.stop_loss_pct < 50:
            errors.append("risk.stop_loss_pct must be in (0, 50)")
        if not 1 <= r.max_positions <= 10:
            errors.append("risk.max_positions must be between 1 and 10")
        if not 0 < r.daily_max_loss_pct < 100:
            errors.append("risk.daily_max_loss_pct must be in (0, 100)")
        if not 0 < r.max_position_pct <= 100:
            errors.append("risk.max_position_pct must be in (0, 100]")
        for name in ("entry_order_type", "exit_order_type"):
            if getattr(o, name) not in ("market", "limit"):
                errors.append(f"orders.{name} must be 'market' or 'limit'")
        if o.time_in_force not in ("gfd", "gtc"):
            errors.append("orders.time_in_force must be 'gfd' or 'gtc'")
        if r.allow_fractional and o.entry_order_type == "limit":
            errors.append("Robinhood only supports fractional shares with market orders; "
                          "set orders.entry_order_type to 'market' or disable risk.allow_fractional")
        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))


def _build(cls, data: dict[str, Any] | None, section: str):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unknown key(s) in '{section}': {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | Path | None = None) -> Config:
    """Load ``config.yaml`` (or defaults if ``path`` is None) and validate it."""
    if path is None:
        cfg = Config()
    else:
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        raw = yaml.safe_load(path.read_text()) or {}
        unknown = set(raw) - {"bot", "strategy", "risk", "orders", "backtest"}
        if unknown:
            raise ConfigError(f"Unknown config section(s): {', '.join(sorted(unknown))}")
        cfg = Config(
            bot=_build(BotConfig, raw.get("bot"), "bot"),
            strategy=_build(StrategyConfig, raw.get("strategy"), "strategy"),
            risk=_build(RiskConfig, raw.get("risk"), "risk"),
            orders=_build(OrderConfig, raw.get("orders"), "orders"),
            backtest=_build(BacktestConfig, raw.get("backtest"), "backtest"),
            base_dir=path.resolve().parent,
        )
    cfg.bot.watchlist = [s.strip().upper() for s in cfg.bot.watchlist if s and s.strip()]
    cfg.validate()
    return cfg

import pytest

from swing_bot.config import BotConfig
from swing_bot.risk_manager import RiskManager


def make_config(**overrides):
    cfg = BotConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_position_size_respects_risk_per_trade():
    cfg = make_config(risk_per_trade_pct=0.01, stop_loss_pct=0.02)
    rm = RiskManager(cfg)
    entry = 100.0
    stop = rm.stop_loss_price(entry)  # 98.0
    size = rm.position_size(account_equity=10_000, entry_price=entry, stop_loss_price=stop)

    max_risk = 10_000 * 0.01
    actual_risk = size.shares * (entry - stop)
    assert actual_risk <= max_risk
    assert size.shares > 0


def test_position_size_zero_when_risk_too_small():
    cfg = make_config(risk_per_trade_pct=0.01)
    rm = RiskManager(cfg)
    size = rm.position_size(account_equity=100, entry_price=1000.0, stop_loss_price=980.0)
    assert size.shares == 0


def test_position_size_invalid_stop_above_entry():
    cfg = make_config()
    rm = RiskManager(cfg)
    size = rm.position_size(account_equity=10_000, entry_price=100.0, stop_loss_price=105.0)
    assert size.shares == 0
    assert "below entry" in size.reason


def test_max_concurrent_positions_enforced():
    cfg = make_config(max_concurrent_positions=2)
    rm = RiskManager(cfg)
    allowed, _ = rm.can_open_new_position(open_position_count=2, account_equity=10_000)
    assert allowed is False
    allowed, _ = rm.can_open_new_position(open_position_count=1, account_equity=10_000)
    assert allowed is True


def test_daily_loss_limit_blocks_new_entries():
    cfg = make_config(daily_loss_limit_pct=0.03)
    rm = RiskManager(cfg)
    rm.record_realized_pnl(-350.0)  # 3.5% of 10,000
    allowed, reason = rm.can_open_new_position(open_position_count=0, account_equity=10_000)
    assert allowed is False
    assert "daily loss" in reason


def test_buying_power_validation():
    ok, _ = RiskManager.validate_buying_power(buying_power=1000, shares=5, price=100)
    assert ok is True
    ok, msg = RiskManager.validate_buying_power(buying_power=400, shares=5, price=100)
    assert ok is False
    assert "insufficient" in msg

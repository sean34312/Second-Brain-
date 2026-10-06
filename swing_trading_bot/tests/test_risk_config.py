import pytest

from swing_bot.config import ConfigError, RiskConfig, load_config
from swing_bot.risk import RiskManager


def test_position_size_risks_one_percent():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, stop_loss_pct=2.0, max_position_pct=100))
    size = rm.position_size(equity=100_000, entry_price=100.0, buying_power=100_000)
    # $1,000 risk / $2 per share = 500 shares
    assert size.quantity == 500
    assert size.stop_price == 98.0
    assert size.risk_amount == pytest.approx(1000.0)
    assert size.risk_amount <= 100_000 * 0.01


def test_position_size_capped_by_max_position_pct():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, stop_loss_pct=2.0, max_position_pct=25))
    size = rm.position_size(equity=100_000, entry_price=100.0, buying_power=100_000)
    assert size.quantity == 250  # $25k cap
    assert "max position" in size.reason


def test_position_size_capped_by_buying_power():
    rm = RiskManager(RiskConfig(max_position_pct=100))
    size = rm.position_size(equity=100_000, entry_price=100.0, buying_power=1_050)
    assert size.quantity == 10
    assert size.notional <= 1_050
    assert rm.position_size(100_000, 100.0, buying_power=50).quantity == 0


def test_fractional_sizing():
    rm = RiskManager(RiskConfig(allow_fractional=True, max_position_pct=100))
    size = rm.position_size(equity=1_000, entry_price=333.0, buying_power=1_000)
    assert 0 < size.quantity < 2 and size.quantity != int(size.quantity)


def test_limits():
    rm = RiskManager(RiskConfig(max_positions=2, daily_max_loss_pct=3))
    assert rm.can_open_position(1, halted=False)[0]
    assert not rm.can_open_position(2, halted=False)[0]
    assert not rm.can_open_position(0, halted=True)[0]
    assert rm.daily_loss_breached(10_000, 9_700)
    assert not rm.daily_loss_breached(10_000, 9_800)


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  stop_los_pct: 2\n")
    with pytest.raises(ConfigError, match="stop_los_pct"):
        load_config(p)


def test_config_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("strategy:\n  rsi_oversold: 80\n  rsi_overbought: 70\nrisk:\n  max_positions: 0\n")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "rsi_oversold" in str(e.value) and "max_positions" in str(e.value)


def test_shipped_config_loads():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.bot.mode == "paper"
    assert cfg.risk.risk_per_trade_pct <= 1.0
    assert 2 <= cfg.risk.max_positions <= 3

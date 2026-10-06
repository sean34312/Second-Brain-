import csv
import math

import numpy as np
import pandas as pd
import pytest
from conftest import FakeData, make_bars

from swing_bot.backtest import Backtester, compute_metrics
from swing_bot.bot import SwingBot
from swing_bot.brokers import PaperBroker
from swing_bot.config import Config
from swing_bot.data import SyntheticDataProvider
from swing_bot.logging_setup import Alerter, TradeJournal
from swing_bot.strategy import Signal, SignalResult


# --------------------------------------------------------------------------- paper broker
def _paper(cfg, data, tmp_path, cash=10_000):
    return PaperBroker(data, cfg.orders, tmp_path / "paper.json", starting_cash=cash, slippage_pct=0)


def test_paper_broker_buy_sell_and_persist(cfg, tmp_path):
    data = FakeData({"AAA": make_bars([100.0] * 5)})
    b = _paper(cfg, data, tmp_path)
    r = b.buy("AAA", 10, "market")
    assert r.filled and r.avg_fill_price == 100.0
    assert b.get_account().cash == pytest.approx(9_000)
    # limit below market does not fill
    assert not b.buy("AAA", 1, "limit", limit_price=95.0).filled
    # cannot overspend
    assert b.buy("AAA", 1_000, "market").status == "rejected"
    data.quotes["AAA"] = 110.0
    assert b.get_account().equity == pytest.approx(9_000 + 1_100)
    # state survives a restart
    b2 = _paper(cfg, data, tmp_path)
    assert b2.get_positions()["AAA"].quantity == 10
    r = b2.sell("AAA", 10, "market")
    assert r.filled and b2.get_account().cash == pytest.approx(10_100)


def test_paper_stop_order_triggers(cfg, tmp_path):
    data = FakeData({"AAA": make_bars([100.0] * 5)})
    b = _paper(cfg, data, tmp_path)
    b.buy("AAA", 10, "market")
    oid = b.place_stop_loss("AAA", 10, 97.5)
    assert b.get_order(oid).status == "confirmed"
    data.quotes["AAA"] = 97.0
    assert "AAA" not in b.get_positions()
    o = b.get_order(oid)
    assert o.status == "filled" and o.avg_fill_price == 97.0


# --------------------------------------------------------------------------- bot
def _bot(cfg: Config, tmp_path, quotes=None):
    bars = {s: make_bars(np.linspace(90, 100, 120)) for s in cfg.bot.watchlist}
    data = FakeData(bars, quotes)
    broker = _paper(cfg, data, tmp_path, cash=100_000)
    journal = TradeJournal(tmp_path / "trades.csv", "paper")
    bot = SwingBot(cfg, broker, data, journal, Alerter())
    return bot, data, broker


def _force_buy(bot, symbols):
    def fake(sym, df, i=-1):
        sig = Signal.BUY if sym in symbols else Signal.HOLD
        return SignalResult(sym, sig, ["test signal"], close=float(df.close.iloc[i]))
    bot.strategy.entry_signal = fake


def _journal(tmp_path):
    with (tmp_path / "trades.csv").open() as f:
        return list(csv.DictReader(f))


def test_bot_enters_with_risk_sizing_and_stop(cfg, tmp_path):
    bot, data, broker = _bot(cfg, tmp_path)
    _force_buy(bot, {"AAA"})
    bot.cycle()
    pos = bot.state.positions["AAA"]
    # 1% of $100k = $1000 risk; stop 2.5% of ~$100.2 -> ~399 shares, capped by 25% notional = 249
    assert pos.quantity == 249
    assert pos.stop_price == pytest.approx(pos.entry_price * 0.975, abs=0.01)
    assert pos.stop_order_id in broker.stop_orders
    rows = _journal(tmp_path)
    assert rows[-1]["side"] == "BUY" and rows[-1]["symbol"] == "AAA"


def test_bot_respects_max_positions(cfg, tmp_path):
    cfg.risk.max_positions = 2
    bot, *_ = _bot(cfg, tmp_path)
    _force_buy(bot, set(cfg.bot.watchlist))
    bot.cycle()
    assert len(bot.state.positions) == 2


def test_bot_stop_loss_exit_records_pnl(cfg, tmp_path):
    cfg.orders.use_broker_stop_orders = False  # bot-enforced stop
    bot, data, broker = _bot(cfg, tmp_path)
    _force_buy(bot, {"AAA"})
    bot.cycle()
    entry = bot.state.positions["AAA"].entry_price
    _force_buy(bot, set())
    data.quotes["AAA"] = entry * 0.97
    bot.cycle()
    assert "AAA" not in bot.state.positions
    sell = _journal(tmp_path)[-1]
    assert sell["side"] == "SELL" and "stop-loss" in sell["signal"]
    assert float(sell["pnl"]) < 0
    assert bot.state.realized_pnl_today < 0


def test_bot_detects_broker_stop_fill(cfg, tmp_path):
    bot, data, broker = _bot(cfg, tmp_path)
    _force_buy(bot, {"AAA"})
    bot.cycle()
    _force_buy(bot, set())
    data.quotes["AAA"] = 50.0  # gap down: resting broker stop fills
    bot.cycle()
    assert "AAA" not in bot.state.positions
    assert "broker stop-loss" in _journal(tmp_path)[-1]["signal"]


def test_daily_loss_limit_halts_entries(cfg, tmp_path):
    bot, data, broker = _bot(cfg, tmp_path)
    bot.state.day = __import__("swing_bot.market_clock", fromlist=["x"]).today_et().isoformat()
    bot.state.day_start_equity = 110_000  # equity is 100k -> down 9%
    _force_buy(bot, {"AAA"})
    bot.cycle()
    assert bot.state.halted_today
    assert not bot.state.positions


def test_bot_skips_when_no_buying_power(cfg, tmp_path):
    bot, data, broker = _bot(cfg, tmp_path)
    broker.cash = 10.0
    _force_buy(bot, {"AAA"})
    bot.cycle()
    assert not bot.state.positions


def test_status_output(cfg, tmp_path):
    bot, data, broker = _bot(cfg, tmp_path)
    _force_buy(bot, {"AAA"})
    bot.cycle()
    text = bot.format_status(broker.get_account(), data.quotes,
                             {s: SignalResult(s, Signal.HOLD, ["x"]) for s in cfg.bot.watchlist}, True)
    assert "POSITIONS (1/3)" in text and "AAA" in text and "HELD" in text


# --------------------------------------------------------------------------- backtest
def test_metrics():
    trades = pd.DataFrame({"pnl": [100.0, -50.0, 200.0, -50.0], "holding_days": [3, 2, 5, 1]})
    equity = pd.Series([1000, 1100, 990, 1200], index=pd.bdate_range("2024-01-01", periods=4), dtype=float)
    m = compute_metrics(trades, equity, 1000)
    assert m["win_rate_pct"] == 50
    assert m["profit_factor"] == pytest.approx(3.0)
    assert m["max_drawdown_pct"] == pytest.approx(10.0)
    assert m["total_return_pct"] == pytest.approx(20.0)
    m2 = compute_metrics(pd.DataFrame({"pnl": [5.0], "holding_days": [1]}), equity, 1000)
    assert math.isinf(m2["profit_factor"])


def test_backtest_runs_without_lookahead(cfg):
    cfg.bot.watchlist = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    cfg.backtest.slippage_pct = 0
    p = SyntheticDataProvider()
    data = {s: p.history(s, "2018-01-01", "2024-12-31") for s in cfg.bot.watchlist}
    res = Backtester(cfg).run(data, start="2019-01-01")
    t = res.trades
    assert len(t) > 0
    for _, tr in t.iterrows():
        bars = data[tr.symbol]
        entry_day = pd.Timestamp(tr.entry_date)
        # entries happen at the open of the bar AFTER the signal bar
        assert tr.entry_price == pytest.approx(bars.loc[entry_day, "open"], abs=1e-3)
        assert tr.exit_date >= tr.entry_date
    # never more than max_positions open at once
    for day in pd.bdate_range("2019-01-01", "2024-12-31"):
        d = day.date()
        open_n = ((t.entry_date <= d) & (t.exit_date > d)).sum()
        assert open_n <= cfg.risk.max_positions
    assert res.equity_curve.iloc[-1] == pytest.approx(res.metrics["final_equity"], abs=0.01)
    assert "Win rate" in res.report()

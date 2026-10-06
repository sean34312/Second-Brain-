"""RobinhoodBroker against a mocked ``robin_stocks`` module - no network, no account."""

from types import SimpleNamespace

import pytest

from swing_bot.brokers import AuthenticationError, BrokerError, RobinhoodBroker
from swing_bot.config import Credentials, OrderConfig


class FakeRH:
    def __init__(self, fill_state="filled"):
        self.calls = []
        self.fill_state = fill_state
        self.cancelled = []
        self.profiles = SimpleNamespace(
            load_account_profile=lambda: {"buying_power": "5000.50", "cash": "5000.50"},
            load_portfolio_profile=lambda: {"equity": "12000.00", "extended_hours_equity": None},
        )
        self.account = SimpleNamespace(build_holdings=lambda: {
            "AAPL": {"quantity": "10.00000000", "average_buy_price": "150.0000"},
            "ZERO": {"quantity": "0", "average_buy_price": "1"}})
        self.orders = SimpleNamespace(
            order_buy_limit=self._order("buy_limit"), order_sell_limit=self._order("sell_limit"),
            order_buy_market=self._order("buy_market"), order_sell_market=self._order("sell_market"),
            order_buy_fractional_by_quantity=self._order("buy_frac"),
            order_sell_fractional_by_quantity=self._order("sell_frac"),
            order_sell_stop_loss=self._order("stop"),
            get_stock_order_info=self._info, cancel_stock_order=self.cancelled.append)
        self.login = lambda **kw: {"access_token": "tok"} if kw["password"] == "good" else {"detail": "bad creds"}

    def _order(self, kind):
        def fn(*args, **kwargs):
            self.calls.append((kind, args, kwargs))
            return {"id": f"oid-{len(self.calls)}"}
        return fn

    def _info(self, oid):
        if self.fill_state == "filled":
            return {"state": "filled", "cumulative_quantity": "10.00000", "average_price": "101.25",
                    "side": "buy", "type": "limit", "quantity": "10"}
        return {"state": "cancelled" if oid in self.cancelled else "confirmed", "cumulative_quantity": "0",
                "average_price": None, "side": "buy", "type": "limit", "quantity": "10"}


def make(fake, timeout=120):
    b = RobinhoodBroker.__new__(RobinhoodBroker)
    b.rh, b.data = fake, None
    b.order_cfg = OrderConfig(order_timeout_seconds=timeout)
    b.creds = Credentials("user", "good", None, None)
    return b


def test_account_and_positions():
    b = make(FakeRH())
    a = b.get_account()
    assert a.equity == 12000.0 and a.buying_power == 5000.50
    pos = b.get_positions()
    assert list(pos) == ["AAPL"] and pos["AAPL"].quantity == 10


def test_limit_buy_fills():
    fake = FakeRH()
    r = make(fake).buy("AAPL", 10, "limit", 101.456)
    assert r.filled and r.avg_fill_price == 101.25 and r.filled_qty == 10 and r.symbol == "AAPL"
    kind, args, kwargs = fake.calls[0]
    assert kind == "buy_limit" and args == ("AAPL", 10, 101.46) and kwargs["timeInForce"] == "gfd"


def test_fractional_uses_fractional_endpoint():
    fake = FakeRH()
    make(fake).sell("AAPL", 0.5, "market")
    assert fake.calls[0][0] == "sell_frac"
    with pytest.raises(BrokerError):
        make(fake).buy("AAPL", 0.5, "limit", 100)


def test_unfilled_order_is_cancelled(monkeypatch):
    monkeypatch.setattr("swing_bot.brokers.time.sleep", lambda s: None)
    fake = FakeRH(fill_state="open")
    r = make(fake, timeout=0).buy("AAPL", 10, "limit", 100)
    assert not r.filled
    assert fake.cancelled == ["oid-1"] and r.status == "cancelled"


def test_retry_then_error(monkeypatch):
    monkeypatch.setattr("swing_bot.brokers.time.sleep", lambda s: None)
    fake = FakeRH()
    fake.profiles.load_account_profile = lambda: None  # robin_stocks returns None on HTTP errors
    with pytest.raises(BrokerError, match="after 3 attempts"):
        make(fake).get_account()


def test_login():
    fake = FakeRH()
    b = make(fake)
    b.connect()
    b.creds = Credentials("user", "bad", None, None)
    with pytest.raises(AuthenticationError, match="bad creds"):
        b.connect()

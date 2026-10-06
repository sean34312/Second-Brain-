"""Broker adapters: live Robinhood (via ``robin_stocks``) and a simulated paper broker.

Both expose the same interface so the bot logic is identical in paper and
live mode - the only difference is where orders go.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Credentials, OrderConfig
from .data import DataProvider
from .market_clock import is_regular_session

log = logging.getLogger(__name__)

TERMINAL_STATES = {"filled", "cancelled", "canceled", "rejected", "failed"}


class BrokerError(RuntimeError):
    """An API call failed (after retries) or returned an unusable response."""


class AuthenticationError(BrokerError):
    pass


@dataclass
class Account:
    equity: float
    buying_power: float
    cash: float


@dataclass
class BrokerPosition:
    symbol: str
    quantity: float
    avg_price: float


@dataclass
class OrderResult:
    order_id: str | None
    symbol: str
    side: str
    order_type: str
    requested_qty: float
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    status: str = "unknown"
    message: str = ""

    @property
    def filled(self) -> bool:
        return self.filled_qty > 0 and self.avg_fill_price is not None


class Broker(ABC):
    name = "base"

    def __init__(self, data: DataProvider, order_cfg: OrderConfig):
        self.data = data
        self.order_cfg = order_cfg

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def reconnect(self) -> None:
        self.connect()

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> dict[str, BrokerPosition]: ...

    @abstractmethod
    def buy(self, symbol: str, qty: float, order_type: str, limit_price: float | None = None) -> OrderResult: ...

    @abstractmethod
    def sell(self, symbol: str, qty: float, order_type: str, limit_price: float | None = None) -> OrderResult: ...

    @abstractmethod
    def place_stop_loss(self, symbol: str, qty: float, stop_price: float) -> str | None: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    def get_order(self, order_id: str) -> OrderResult | None: ...

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        return self.data.latest_prices(symbols) if symbols else {}

    def is_market_open(self) -> bool:
        return is_regular_session()


# --------------------------------------------------------------------------- live
class RobinhoodBroker(Broker):
    """Live trading through Robinhood.

    Authentication: ``robin_stocks`` performs Robinhood's OAuth2 password grant
    against ``https://api.robinhood.com/oauth2/token/`` and caches the resulting
    access/refresh token in ``~/.tokens/robinhood.pickle``. If
    ``ROBINHOOD_MFA_SECRET`` is set, a TOTP code is generated automatically;
    otherwise Robinhood may prompt for an SMS code or app approval on first login.
    """

    name = "robinhood"
    RETRIES = 3

    def __init__(self, data: DataProvider, order_cfg: OrderConfig, creds: Credentials):
        super().__init__(data, order_cfg)
        import robin_stocks.robinhood as rh
        self.rh = rh
        self.creds = creds

    # -- plumbing
    def _call(self, fn: Callable, *args, what: str = "", allow_empty: bool = False, **kwargs) -> Any:
        """Call a robin_stocks function with retries + exponential backoff.

        robin_stocks swallows many HTTP errors and returns ``None``/``[]``, so an
        empty response is treated as a failure unless ``allow_empty``.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                result = fn(*args, **kwargs)
                if result is None or (result == [] and not allow_empty) or result == [None]:
                    raise BrokerError(f"empty response from {what or fn.__name__}")
                return result
            except Exception as e:  # noqa: BLE001 - network/JSON errors surface as many types
                last_exc = e
                if attempt < self.RETRIES:
                    delay = 2 ** attempt
                    log.warning("API call %s failed (attempt %d/%d): %s - retrying in %ds",
                                what or fn.__name__, attempt, self.RETRIES, e, delay)
                    time.sleep(delay)
        raise BrokerError(f"{what or fn.__name__} failed after {self.RETRIES} attempts: {last_exc}")

    def connect(self) -> None:
        if not self.creds.has_robinhood:
            raise AuthenticationError("ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD are not set")
        mfa_code = None
        if self.creds.mfa_secret:
            import pyotp
            mfa_code = pyotp.TOTP(self.creds.mfa_secret).now()
        try:
            resp = self.rh.login(username=self.creds.username, password=self.creds.password,
                                 mfa_code=mfa_code, store_session=True, expiresIn=86400)
        except Exception as e:  # noqa: BLE001
            raise AuthenticationError(f"Robinhood login failed: {e}") from e
        if not resp or "access_token" not in resp:
            raise AuthenticationError(f"Robinhood login failed: {(resp or {}).get('detail', 'no token returned')}")
        log.info("Authenticated with Robinhood (OAuth2 token acquired)")

    def disconnect(self) -> None:
        try:
            self.rh.logout()
            log.info("Logged out of Robinhood")
        except Exception as e:  # noqa: BLE001
            log.warning("Logout failed: %s", e)

    # -- account
    def get_account(self) -> Account:
        profile = self._call(self.rh.profiles.load_account_profile, what="load_account_profile")
        portfolio = self._call(self.rh.profiles.load_portfolio_profile, what="load_portfolio_profile")
        equity = portfolio.get("extended_hours_equity") or portfolio.get("equity")
        return Account(equity=float(equity), buying_power=float(profile["buying_power"]),
                       cash=float(profile.get("cash") or 0.0))

    def get_positions(self) -> dict[str, BrokerPosition]:
        holdings = self._call(self.rh.account.build_holdings, what="build_holdings", allow_empty=True)
        return {sym: BrokerPosition(sym, float(h["quantity"]), float(h["average_buy_price"]))
                for sym, h in (holdings or {}).items() if float(h.get("quantity") or 0) > 0}

    def is_market_open(self) -> bool:
        try:
            hours = self._call(self.rh.markets.get_market_today_hours, "XNYS", what="market_hours")
        except BrokerError as e:
            log.warning("Could not fetch market hours (%s); falling back to clock", e)
            return is_regular_session()
        if not hours.get("is_open"):
            return False
        now = datetime.now(timezone.utc)
        opens = datetime.fromisoformat(hours["opens_at"].replace("Z", "+00:00"))
        closes = datetime.fromisoformat(hours["closes_at"].replace("Z", "+00:00"))
        return opens <= now < closes

    # -- orders
    def _submit(self, side: str, symbol: str, qty: float, order_type: str,
                limit_price: float | None) -> OrderResult:
        tif = self.order_cfg.time_in_force
        fractional = qty != int(qty)
        if fractional and order_type != "market":
            raise BrokerError("fractional quantities require market orders")
        if order_type == "limit":
            fn = self.rh.orders.order_buy_limit if side == "buy" else self.rh.orders.order_sell_limit
            args = (symbol, int(qty), round(limit_price, 2))
        elif fractional:
            fn = (self.rh.orders.order_buy_fractional_by_quantity if side == "buy"
                  else self.rh.orders.order_sell_fractional_by_quantity)
            args = (symbol, qty)
            tif = "gfd"  # fractional orders must be good-for-day
        else:
            fn = self.rh.orders.order_buy_market if side == "buy" else self.rh.orders.order_sell_market
            args = (symbol, int(qty))

        # Never retry order submission automatically - a timeout could mean the order DID go through.
        try:
            resp = fn(*args, timeInForce=tif)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"{side} order for {symbol} failed: {e}") from e
        if not resp or "id" not in resp:
            detail = (resp or {}).get("detail") or (resp or {}).get("non_field_errors") or resp
            return OrderResult(None, symbol, side, order_type, qty, status="rejected", message=str(detail))
        log.info("Submitted %s %s order %s: %s x %s", order_type, side, resp["id"], qty, symbol)
        return self._wait_for_fill(resp["id"], symbol, side, order_type, qty)

    def _wait_for_fill(self, order_id: str, symbol: str, side: str, order_type: str, qty: float) -> OrderResult:
        deadline = time.monotonic() + self.order_cfg.order_timeout_seconds
        result = OrderResult(order_id, symbol, side, order_type, qty, status="submitted")
        while time.monotonic() < deadline:
            result = self.get_order(order_id) or result
            result.symbol = symbol  # order info only carries the instrument URL
            if result.status in TERMINAL_STATES:
                return result
            time.sleep(2)
        log.warning("Order %s not filled within %ss - cancelling", order_id, self.order_cfg.order_timeout_seconds)
        self.cancel_order(order_id)
        time.sleep(2)
        final = self.get_order(order_id) or result
        final.symbol = symbol
        if final.status not in TERMINAL_STATES:
            final.message = "timed out; cancel requested"
        return final

    def get_order(self, order_id: str) -> OrderResult | None:
        try:
            info = self._call(self.rh.orders.get_stock_order_info, order_id, what="get_stock_order_info")
        except BrokerError as e:
            log.warning("Could not fetch order %s: %s", order_id, e)
            return None
        filled = float(info.get("cumulative_quantity") or 0)
        avg = info.get("average_price")
        if avg is None and info.get("executions"):
            ex = info["executions"]
            total = sum(float(e["quantity"]) for e in ex)
            avg = sum(float(e["price"]) * float(e["quantity"]) for e in ex) / total if total else None
        sym = info.get("symbol") or ""
        return OrderResult(order_id, sym, info.get("side", ""), info.get("type", ""),
                           float(info.get("quantity") or 0), filled,
                           float(avg) if avg is not None and filled > 0 else None,
                           info.get("state", "unknown"), info.get("reject_reason") or "")

    def buy(self, symbol, qty, order_type, limit_price=None):
        return self._submit("buy", symbol, qty, order_type, limit_price)

    def sell(self, symbol, qty, order_type, limit_price=None):
        return self._submit("sell", symbol, qty, order_type, limit_price)

    def place_stop_loss(self, symbol, qty, stop_price):
        whole = int(qty)
        if whole <= 0:
            return None
        try:
            resp = self.rh.orders.order_sell_stop_loss(symbol, whole, round(stop_price, 2), timeInForce="gtc")
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"stop-loss order for {symbol} failed: {e}") from e
        if not resp or "id" not in resp:
            raise BrokerError(f"stop-loss order for {symbol} rejected: {(resp or {}).get('detail', resp)}")
        log.info("Placed GTC stop-loss %s: sell %d %s @ %.2f", resp["id"], whole, symbol, stop_price)
        return resp["id"]

    def cancel_order(self, order_id):
        try:
            self.rh.orders.cancel_stock_order(order_id)
            log.info("Cancel requested for order %s", order_id)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"cancel of {order_id} failed: {e}") from e


# --------------------------------------------------------------------------- paper
class PaperBroker(Broker):
    """Simulated broker. Fills at the latest quote (+/- slippage) and persists
    its cash/positions to a JSON file so paper sessions survive restarts."""

    name = "paper"

    def __init__(self, data: DataProvider, order_cfg: OrderConfig, state_file: Path,
                 starting_cash: float = 25_000.0, slippage_pct: float = 0.05):
        super().__init__(data, order_cfg)
        self.state_file = Path(state_file)
        self.slippage = slippage_pct / 100
        self.cash = starting_cash
        self.positions: dict[str, dict[str, float]] = {}
        self.stop_orders: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.state_file.exists():
            s = json.loads(self.state_file.read_text())
            self.cash = s["cash"]
            self.positions = s.get("positions", {})
            self.stop_orders = s.get("stop_orders", {})
            self.orders = s.get("orders", {})
            log.info("Loaded paper account from %s (cash $%.2f)", self.state_file, self.cash)

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # Keep only recent order records so the file doesn't grow forever.
        recent = dict(list(self.orders.items())[-200:])
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cash": self.cash, "positions": self.positions,
                                   "stop_orders": self.stop_orders, "orders": recent}, indent=2))
        tmp.replace(self.state_file)

    def _record(self, r: OrderResult) -> OrderResult:
        if r.order_id is None:
            r.order_id = f"paper-{uuid.uuid4().hex[:12]}"
        self.orders[r.order_id] = asdict(r)
        self._save()
        return r

    def _process_stops(self, quotes: dict[str, float] | None = None) -> None:
        """Trigger resting stop orders whose stop price has been touched."""
        if not self.stop_orders:
            return
        quotes = quotes or self.get_quotes(sorted({o["symbol"] for o in self.stop_orders.values()}))
        for oid, o in list(self.stop_orders.items()):
            price = quotes.get(o["symbol"])
            if price is None or price > o["stop_price"]:
                continue
            fill = price * (1 - self.slippage)
            pos = self.positions.get(o["symbol"])
            qty = min(o["qty"], pos["qty"] if pos else 0)
            del self.stop_orders[oid]
            if qty <= 0:
                continue
            self._apply_sell(o["symbol"], qty, fill)
            self._record(OrderResult(oid, o["symbol"], "sell", "stop", o["qty"], qty, fill, "filled"))
            log.info("[PAPER] Stop-loss %s triggered: sold %s %s @ %.2f", oid, qty, o["symbol"], fill)

    def _apply_sell(self, symbol: str, qty: float, price: float) -> None:
        pos = self.positions[symbol]
        pos["qty"] = round(pos["qty"] - qty, 6)
        self.cash += qty * price
        if pos["qty"] <= 0:
            del self.positions[symbol]

    def get_account(self) -> Account:
        self._process_stops()
        quotes = self.get_quotes(list(self.positions))
        mv = sum(p["qty"] * quotes.get(s, p["avg_price"]) for s, p in self.positions.items())
        return Account(equity=self.cash + mv, buying_power=self.cash, cash=self.cash)

    def get_positions(self) -> dict[str, BrokerPosition]:
        self._process_stops()
        return {s: BrokerPosition(s, p["qty"], p["avg_price"]) for s, p in self.positions.items()}

    def buy(self, symbol, qty, order_type, limit_price=None):
        quote = self.data.latest_price(symbol)
        fill = quote * (1 + self.slippage)
        if order_type == "limit":
            if limit_price is None or quote > limit_price:
                return self._record(OrderResult(None, symbol, "buy", order_type, qty, status="cancelled",
                                                message=f"limit {limit_price} not reached (quote {quote:.2f})"))
            fill = min(fill, limit_price)
        cost = qty * fill
        if cost > self.cash + 1e-6:
            return self._record(OrderResult(None, symbol, "buy", order_type, qty, status="rejected",
                                            message="insufficient buying power"))
        pos = self.positions.setdefault(symbol, {"qty": 0.0, "avg_price": 0.0})
        new_qty = pos["qty"] + qty
        pos["avg_price"] = (pos["qty"] * pos["avg_price"] + cost) / new_qty
        pos["qty"] = new_qty
        self.cash -= cost
        log.info("[PAPER] Bought %s %s @ %.2f", qty, symbol, fill)
        return self._record(OrderResult(None, symbol, "buy", order_type, qty, qty, fill, "filled"))

    def sell(self, symbol, qty, order_type, limit_price=None):
        pos = self.positions.get(symbol)
        if not pos or pos["qty"] < qty - 1e-9:
            return self._record(OrderResult(None, symbol, "sell", order_type, qty, status="rejected",
                                            message="insufficient shares"))
        quote = self.data.latest_price(symbol)
        fill = quote * (1 - self.slippage)
        if order_type == "limit":
            if limit_price is None or quote < limit_price:
                return self._record(OrderResult(None, symbol, "sell", order_type, qty, status="cancelled",
                                                message=f"limit {limit_price} not reached (quote {quote:.2f})"))
            fill = max(fill, limit_price)
        self._apply_sell(symbol, qty, fill)
        log.info("[PAPER] Sold %s %s @ %.2f", qty, symbol, fill)
        return self._record(OrderResult(None, symbol, "sell", order_type, qty, qty, fill, "filled"))

    def place_stop_loss(self, symbol, qty, stop_price):
        oid = f"paper-stop-{uuid.uuid4().hex[:12]}"
        self.stop_orders[oid] = {"symbol": symbol, "qty": qty, "stop_price": round(stop_price, 2)}
        self._save()
        log.info("[PAPER] Placed stop-loss %s: sell %s %s @ %.2f", oid, qty, symbol, stop_price)
        return oid

    def cancel_order(self, order_id):
        if self.stop_orders.pop(order_id, None) is not None:
            if order_id not in self.orders:
                self.orders[order_id] = {"status": "cancelled"}
            self._save()

    def get_order(self, order_id):
        self._process_stops()
        if order_id in self.stop_orders:
            o = self.stop_orders[order_id]
            return OrderResult(order_id, o["symbol"], "sell", "stop", o["qty"], status="confirmed")
        rec = self.orders.get(order_id)
        if not rec or "symbol" not in rec:
            return OrderResult(order_id, "", "sell", "stop", 0, status=(rec or {}).get("status", "unknown"))
        return OrderResult(**rec)

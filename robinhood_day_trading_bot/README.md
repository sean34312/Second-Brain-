# Robinhood Momentum Day Trading Bot

An aggressive, intraday-only momentum bot: scans a large watchlist for
volume-confirmed breakouts, enters with market orders, scales out at 1-3%
targets, and hard-stops at 0.5-1%. All positions are flattened before the
close. **Read the whole "Risk and reality check" section before you point
this at a real account.**

## Risk and reality check

This is the part of the README you're most likely to skip, so it comes
first.

- **Robinhood has no official trading API.** This bot uses
  [`robin_stocks`](https://github.com/jmfernandes/robin_stocks), a
  community library that logs in as if it were the mobile app and drives
  the same private endpoints. **This is against Robinhood's Terms of
  Service.** Using it is a choice you're making for your own account, at
  your own risk, including the risk of the account being restricted,
  frozen, or closed.
- **There is no real-time WebSocket feed here**, because Robinhood doesn't
  expose one publicly. "Real-time" means polling REST endpoints every few
  seconds (`SCAN_INTERVAL_SECONDS`) across the whole watchlist with a
  thread pool. Expect data that is seconds old, occasional rate-limit
  errors, and no sub-second reaction time.
- **"Microsecond-level order timing" is not achievable this way.** Every
  order is an HTTPS request-response round trip to Robinhood's servers,
  same as the app. There is no colocation, no direct exchange access, and
  no way around normal internet + broker latency (realistically tens to
  hundreds of milliseconds, sometimes worse). The bot logs wall-clock
  timestamps around order placement so you can see your actual latency;
  don't expect it to look like real HFT infrastructure.
- **The Pattern Day Trader (PDT) rule is real and enforced by Robinhood,
  not just a suggestion.** Accounts under $25,000 equity are limited to 3
  day trades in a rolling 5-business-day window; breaking it can freeze
  your account. This bot enforces that limit itself by default
  (`ENFORCE_PDT_RULE=true`) so it refuses to open a trade that would push
  you over the limit -- but that's a courtesy check on top of Robinhood's
  own enforcement, not a replacement for it, and it does not know about
  day trades you placed manually or from elsewhere.
- **Leverage/margin multiplies losses as fast as gains.** With
  `USE_MARGIN=true` and `LEVERAGE=2` or `3`, a normal losing streak can
  produce a margin call. This is off by default.
- **High win-rate, 1-2% target strategies are extremely sensitive to
  costs.** Bid/ask spread, slippage on market orders, and the occasional
  bad fill eat directly into a 1% target. Nothing here has been
  backtested against real fills; the included unit tests only check the
  indicator/signal math, not profitability.
- **This code is provided for educational/experimentation purposes.**
  Nothing here is financial advice, and past-performance-style claims
  don't apply because there is no track record. Start in paper mode, watch
  a few full sessions of logs, and only consider real money with size you
  can afford to lose entirely.

## Safety defaults

- `PAPER_TRADING=true` by default (`.env.example`). In this mode the bot
  still logs into Robinhood and polls **real** market data, but every
  "fill" is simulated against the polled price and written to
  `logs/trades.csv` -- nothing is sent to the brokerage. You have to
  explicitly flip this to `false` to place real orders.
- `ENFORCE_PDT_RULE=true` by default.
- A daily drawdown circuit breaker (`MAX_DAILY_DRAWDOWN_PCT`, default 3%)
  halts new entries and, by default, flattens every open position the
  moment realized+unrealized losses hit that threshold for the day.
- Margin/leverage (`USE_MARGIN`, `LEVERAGE`) is off (`1.0x`) by default.
- Every open position gets a resting stop-loss order placed immediately,
  so a crashed bot process doesn't leave a position unprotected (in live
  mode this is a real stop order sitting at the broker; Robinhood doesn't
  offer native OCO brackets, so target fills briefly cancel-and-replace
  the stop for the smaller remaining size -- see `bot/position_manager.py`
  for the exact mechanics and its limitation).

## Strategy summary

Long-only (Robinhood retail accounts generally can't short stock outright).
For each watchlist symbol, on every scan:

1. **Breakout:** 5-minute close breaks above its rolling N-bar high
   (`BREAKOUT_LOOKBACK_BARS`, default 20).
2. **Volume confirmation:** that bar's volume is at least
   `VOLUME_SPIKE_MULTIPLE` (default 2x) its trailing average.
3. **Momentum confirmation:** fast Stochastic (5,3,3) is turning up
   (`%K > %D` and rising) and not already blown off above 95 (avoids
   chasing an exhausted move).
4. **Trend confirmation:** MACD is bullish (line above signal) on *both*
   the 5-minute and 15-minute charts.
5. **Sizing:** `POSITION_SIZE_PCT` of equity (2-5%, default 3%), optionally
   multiplied by `LEVERAGE` if `USE_MARGIN=true`, capped by buying power.
6. **Stop:** the tighter of a fixed percentage (`STOP_LOSS_PCT`, default
   0.75%) and an ATR-based stop (`ATR_STOP_MULTIPLE x ATR(14)`), placed as
   a real stop-loss order.
7. **Targets:** scale out in tranches (`profit_targets` /
   `profit_scale_out_fractions` in `bot/config.py`, default 50% at +1%,
   25% at +1.5%, 25% at +2.5%), placed as GTC limit sell orders.
8. **Session close:** no new entries after `NO_NEW_ENTRIES_AFTER` (default
   15:40 ET); everything still open is force-flattened with market orders
   at `FORCE_CLOSE_AT` (default 15:45 ET).

An hourly chart timeframe and a volume-profile point-of-control/support/
resistance calculation are also computed (`bot/indicators.py`) and logged
with every signal for context, even though the entry rule above keys off
the 5m/15m combination.

## Setup

```bash
cd robinhood_day_trading_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # add -r requirements-dev.txt for tests
cp .env.example .env                 # fill in your Robinhood credentials
```

Robinhood will typically challenge a fresh login with an MFA code (SMS or
authenticator app). Fill `ROBINHOOD_MFA_CODE` in `.env` right before you run
the bot, or extend `bot/auth.py` to prompt interactively / generate a TOTP
code if you use an authenticator app.

Run it:

```bash
python main.py
```

It will wait for market open if started early, warm up historicals for the
whole watchlist, then loop until 15:45 ET flattening everything, or until
you Ctrl+C (which stops new entries on the next loop iteration -- it does
**not** flatten existing positions for you; use `PositionManager.close_all`
yourself, or just let the scheduled close handle it, if the market is still
open).

## Configuration

Everything tunable lives in `bot/config.py` as dataclass fields, each
overridable by an environment variable (see the `field(default_factory=...)`
entries). The watchlist defaults to ~85 liquid large-caps/ETFs; edit
`DEFAULT_WATCHLIST` or set your own list in code to change it.

## Testing

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. pytest tests/
```

The tests cover indicator math and signal generation against synthetic
OHLCV data -- no network access or Robinhood credentials required. They do
not test order execution, fill reconciliation, or profitability; those
paths only run against paper trading or a live account.

## Project layout

```
bot/
  config.py           all tunables
  auth.py              Robinhood login/logout
  data_feed.py         polled "real-time" market data across the watchlist
  indicators.py        stochastic, MACD, ATR, breakout levels, volume profile
  strategy.py          momentum breakout signal generation
  risk_manager.py       position sizing, PDT rule, daily drawdown breaker
  order_manager.py       market orders + resting stop/limit orders (or paper sim)
  position_manager.py   open-position bookkeeping and fill reconciliation
  scheduler.py           market-hours clock (US/Eastern)
  bot_engine.py           the scan -> signal -> risk -> order -> manage loop
  logger_setup.py        logging + CSV trade blotter
main.py                  entrypoint
tests/                   indicator/strategy unit tests (no network needed)
```

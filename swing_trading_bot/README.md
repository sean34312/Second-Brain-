# Robinhood Swing Trading Bot (educational)

A Python swing trading bot for Robinhood. It watches a list of stocks, looks for
RSI pullbacks inside an uptrend confirmed by MACD, sizes each trade so a stopped-out
trade costs at most 1% of the account, and manages exits with a stop-loss, take-profit
and indicator-based rules. It has a **paper trading** mode and a **backtester**, and both
run the same strategy and risk code as live trading.

> ## ⚠️ Disclaimer — read this first
> **This project is for educational purposes only and is not financial advice.**
> Trading stocks carries substantial risk, and you can lose money quickly. Backtest and
> paper results do **not** predict live results: real fills, gaps, outages and
> bugs all make live trading worse.
>
> - `robin_stocks` is an **unofficial**, reverse-engineered client. Robinhood has no
>   official public API for stock trading. Robinhood can change its API without notice,
>   and automated trading may break Robinhood's Terms of Service. Your account could be
>   restricted.
> - You alone are responsible for every order this software places. Read the code,
>   run it in paper mode for weeks, and start live trading with a small amount.
> - The software comes with no warranty of any kind.

---

## Contents

- [How the strategy works](#how-the-strategy-works)
- [Setup](#setup)
- [Credentials and environment variables](#credentials-and-environment-variables)
- [Usage](#usage)
- [Configuration reference](#configuration-reference)
- [Risk management](#risk-management)
- [Logs, trade journal and alerts](#logs-trade-journal-and-alerts)
- [Backtesting](#backtesting)
- [Project layout](#project-layout)
- [Limitations and known caveats](#limitations-and-known-caveats)

---

## How the strategy works

The strategy is long-only. It uses daily candles and evaluates only **completed** bars,
so today's unfinished bar is ignored until the market closes.

**Entry: all three conditions must hold**

| # | Rule | Default |
|---|------|---------|
| 1 | **Trend bias:** SMA20 > SMA50 and close > SMA50 | `sma_fast: 20`, `sma_slow: 50` |
| 2 | **RSI pullback:** RSI(14) was ≤ `rsi_oversold` at some point in the last `rsi_lookback` bars, is now rising, and is still below `rsi_overbought` | 40 / 10 bars / 70 |
| 3 | **MACD confirmation:** MACD(12,26,9) crossed above its signal line in the last `macd_cross_lookback` bars, and the histogram is still positive | 5 bars |

**Exit: any one condition triggers it**

- **Stop-loss:** price ≤ entry × (1 − `stop_loss_pct`). The bot checks this every
  cycle, and by default it also places a resting GTC stop order at Robinhood.
- **Take-profit:** price ≥ entry × (1 + `take_profit_pct`).
- RSI ≥ `rsi_overbought`.
- MACD crosses back below its signal line.
- Close falls below SMA50 (trend break).
- The position has been held for `max_holding_days`.

**Timing:** a signal on day *t*'s close is acted on during day *t+1*. The backtester
uses the same rule and fills at the next open.

---

## Setup

Requires **Python 3.10+**.

```bash
cd swing_trading_bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit .env (see below)
python -m pytest -q                # optional: run the test suite (no network needed)
```

---

## Credentials and environment variables

Secrets are read **only from environment variables**. A `.env` file in the project
directory is loaded automatically. `.env` is git-ignored, so never commit it and never
put credentials in `config.yaml`.

| Variable | Required | Purpose |
|----------|----------|---------|
| `ROBINHOOD_USERNAME` | live mode | Robinhood login email |
| `ROBINHOOD_PASSWORD` | live mode | Robinhood password |
| `ROBINHOOD_MFA_SECRET` | recommended | Base32 TOTP secret so the bot can generate 2FA codes itself |
| `ALERT_WEBHOOK_URL` | optional | Discord or Slack incoming-webhook URL for alerts and fill notices |

### How authentication works

`robin_stocks` logs in with Robinhood's **OAuth2** password grant
(`POST https://api.robinhood.com/oauth2/token/`). It receives an access token and a
refresh token and caches them in `~/.tokens/robinhood.pickle`, so later starts don't
need to log in again until the token expires. Protect that file: it grants access to
your account.

**Getting `ROBINHOOD_MFA_SECRET`:**
1. In the Robinhood app, go to Account → Security → Two-Factor Authentication and
   choose **Authenticator app**.
2. When the QR code appears, choose the option to show the code as text. That text is
   your base32 secret.
3. Enter the secret in your normal authenticator app too, and finish setup with the
   code it shows.
4. Put the secret in `.env` as `ROBINHOOD_MFA_SECRET`.

Without a TOTP secret, Robinhood may ask for an SMS code or approval in the app the
first time you log in. `robin_stocks` prompts for it in the terminal, so run the first
login interactively.

**Paper mode does not need credentials.** It then gets prices from Yahoo Finance
(`yfinance`). If credentials are set and `data_source: auto`, paper mode logs in to
Robinhood only to read real-time quotes. It never places orders in paper mode.

---

## Usage

All commands run from the `swing_trading_bot/` directory.

```bash
# Paper trading loop (the default mode). Stop with Ctrl+C.
python main.py run

# Run one cycle and exit (useful with cron, or to check your setup)
python main.py run --once

# Snapshot of account, positions and watchlist signals (places no orders)
python main.py status

# Backtest (see the Backtesting section)
python main.py backtest --start 2021-01-01

# LIVE trading: asks you to type "I UNDERSTAND THE RISKS"
python main.py run --mode live

# Use another config file, with debug logging
python main.py --config my_config.yaml -v run
```

Live mode needs three things: `bot.mode: live` (or `--mode live`), Robinhood
credentials, and an interactive confirmation. For unattended runs you can pass
`--yes-i-understand-the-risks` to skip the confirmation.

### What happens each cycle (every `poll_interval_seconds`)

1. Check whether the market is open. Live mode asks Robinhood for the exchange
   calendar, so holidays are handled.
2. Fetch the account (equity and buying power). On a new day, record start-of-day equity.
3. Reconcile with the broker. If a resting stop order filled while the bot was busy
   or offline, record the exit and its P&L.
4. Fetch quotes and daily candles, then compute RSI, MACD and the SMAs.
5. If the market is open:
   - Check the daily loss limit.
   - Run the exit rules on open positions.
   - Run the entry rules on the watchlist, applying every risk check.
6. Print a status table:

```
================================================================================================
 2026-10-06 10:35:00 EDT | mode PAPER | market OPEN
 Equity $25,214.10 | Buying power $18,951.33 | Day P&L $+81.40 (+0.32%) | Realized today $+0.00
------------------------------------------------------------------------------------------------
 POSITIONS (1/3)
  SYM         QTY     ENTRY      LAST      STOP       P&L $    P&L %  SINCE
  MSFT         14    441.20    447.01    430.17      +81.34   +1.32%  2026-10-02
------------------------------------------------------------------------------------------------
 WATCHLIST
  SYM         LAST    RSI   MACD-H     SMA20     SMA50  SIGNAL DETAIL
  AAPL      231.40   55.1    0.412    228.10    221.95  HOLD   uptrend (...); RSI 55.1 no recent oversold ...
  MSFT      447.01   58.3    1.020    440.12    431.80  HELD   ...
================================================================================================
```

### Graceful shutdown

`Ctrl+C` or `SIGTERM` lets the current step finish (an order already in flight is
followed until it fills or is cancelled), saves state and exits. A second `Ctrl+C`
kills the process immediately. Open positions **stay open**, protected by their resting
broker stop orders. Set `flatten_on_shutdown: true` if you want the bot to sell
everything when it stops.

State lives in `state/bot_state.json`, so a restarted bot picks up the positions it
was managing.

---

## Configuration reference

`config.yaml` is the strategy configuration. Every key is optional; anything you leave
out uses the default. Unknown keys are rejected, so a typo such as `stop_los_pct`
stops the bot at startup instead of being silently ignored.

### `bot`
| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `paper` | `paper` simulates orders; `live` sends real orders |
| `watchlist` | 8 large caps | Symbols to scan |
| `poll_interval_seconds` | `300` | Seconds between cycles (minimum 10) |
| `trade_only_market_hours` | `true` | Outside market hours the bot only monitors |
| `data_source` | `auto` | `auto`, `robinhood`, `yfinance`, `csv` or `synthetic` |
| `history_days` | `365` | Calendar days of candles loaded for indicators |
| `paper_starting_cash` | `25000` | Starting balance of the paper account |
| `paper_slippage_pct` | `0.05` | Slippage applied to every paper fill |
| `max_consecutive_api_failures` | `3` | Failed cycles before a CRITICAL alert and a re-login |
| `logout_on_shutdown` | `false` | Revoke the cached token on exit |
| `flatten_on_shutdown` | `false` | Sell all bot positions on exit |

### `strategy`
| Key | Default | Tuning notes |
|-----|---------|--------------|
| `rsi_period` | `14` | Shorter is faster and noisier |
| `rsi_oversold` | `40` | Lower means deeper pullbacks and fewer trades. 30 is rare in an uptrend |
| `rsi_overbought` | `70` | Exit level, and the ceiling for entries |
| `rsi_lookback` | `10` | How recently RSI must have been oversold |
| `macd_fast` / `macd_slow` / `macd_signal` | `12 / 26 / 9` | Standard MACD |
| `macd_cross_lookback` | `5` | How recent the bullish cross must be |
| `sma_fast` / `sma_slow` | `20 / 50` | Trend filter |
| `require_price_above_slow_sma` | `true` | Also require close > SMA50 |
| `exit_on_rsi_overbought` | `true` | |
| `exit_on_macd_bearish_cross` | `true` | Turn off to let winners run longer |
| `exit_on_trend_break` | `true` | Exit on a close below SMA50 |
| `take_profit_pct` | `6.0` | `0` turns it off |
| `max_holding_days` | `20` | `0` turns it off |

### `risk`
| Key | Default | Description |
|-----|---------|-------------|
| `risk_per_trade_pct` | `1.0` | Most of equity a single stopped-out trade can lose (capped at 5) |
| `stop_loss_pct` | `2.5` | Stop distance below the entry fill |
| `max_positions` | `3` | Maximum concurrent positions |
| `daily_max_loss_pct` | `3.0` | Once equity is down this much on the day, entries stop until tomorrow |
| `flatten_on_daily_loss` | `false` | Also close every position when the limit is hit |
| `max_position_pct` | `25.0` | Largest single position as a % of equity |
| `allow_fractional` | `false` | Fractional shares need market entry orders, and broker stop orders don't support them |
| `reentry_cooldown_days` | `1` | Wait this long before buying a symbol again after exiting it |

### `orders`
| Key | Default | Description |
|-----|---------|-------------|
| `entry_order_type` | `limit` | `market` or `limit` |
| `exit_order_type` | `market` | Market orders make sure exits fill |
| `limit_offset_pct` | `0.2` | Buy limit = quote + 0.2%; sell limit = quote − 0.2% |
| `order_timeout_seconds` | `120` | Orders still unfilled after this are cancelled |
| `time_in_force` | `gfd` | `gfd` (good for day) or `gtc` |
| `use_broker_stop_orders` | `true` | Places a GTC stop at Robinhood after each entry |
| `max_entry_gap_pct` | `2.0` | Skip the entry if price is already this far above the signal close |

### `backtest`
| Key | Default | Description |
|-----|---------|-------------|
| `start` / `end` | `2021-01-01` / today | Backtest window (extra history before `start` warms up the indicators) |
| `initial_capital` | `25000` | |
| `slippage_pct` | `0.05` | Applied to every fill |
| `commission_per_trade` | `0.0` | Robinhood charges no commission |
| `data_source` | `yfinance` | `yfinance`, `csv`, `synthetic` or `robinhood` |
| `output_dir` | `backtest_results` | Where trades, the equity curve and metrics are written |

---

## Risk management

**Position sizing.** Each trade risks a fixed fraction of equity:

```
risk budget     = equity × risk_per_trade_pct          e.g. $25,000 × 1%   = $250
per-share risk  = entry − stop = entry × stop_loss_pct e.g. $100 × 2.5%    = $2.50
shares          = risk budget / per-share risk                             = 100
```

The share count is then capped so the position is no larger than `max_position_pct`
of equity and fits in the **available buying power**. Buying power is fetched again
just before every order. If the size rounds down to 0, the trade is skipped and the
log says why.

| Control | Behaviour |
|---------|-----------|
| Max concurrent positions | New BUY signals are skipped once `max_positions` is reached |
| Daily loss limit | Start-of-day equity is recorded. At a loss of `daily_max_loss_pct` or more, entries stop for the day and a CRITICAL alert is sent. With `flatten_on_daily_loss`, all positions are also closed |
| Stop-loss | Enforced by the bot every cycle, and by a resting broker stop order when the bot is offline |
| Buying-power check | Orders larger than the buying power are never sent |
| Entry gap guard | No buying after a large gap above the signal price |
| Unmanaged positions | Positions you opened yourself are ignored and never sold. They don't count toward `max_positions` |

> A stop is not a guaranteed exit price. A stock that gaps down overnight fills below
> the stop, so a single trade can lose more than 1%.

---

## Logs, trade journal and alerts

| File | Contents |
|------|----------|
| `logs/bot.log` | Every operation, timestamped to the millisecond (rotating, 5 × 5 MB) |
| `logs/trades.csv` | One row per fill: timestamp, mode, symbol, side, qty, price, order type, order id, signal reason, entry price, stop, **P&L $**, **P&L %** |
| `state/bot_state.json` | Managed positions, start-of-day equity, halt flag |
| `state/paper_account.json` | Paper account cash and positions |

**Alerts.** API errors, a lost connection (N failed cycles in a row, followed by an
automatic re-login), rejected orders, a hit daily loss limit, and failure to place a
stop are logged at ERROR or CRITICAL. If `ALERT_WEBHOOK_URL` is set they are also
posted to Discord or Slack, as are fill notices. Repeats of the same alert are
suppressed for 5 minutes.

API calls are retried with exponential backoff. **Order submissions are never retried
automatically**, because a timed-out request may still have placed the order.

---

## Backtesting

```bash
python main.py backtest                                   # config.yaml settings, Yahoo data
python main.py backtest --start 2022-01-01 --end 2024-12-31
python main.py backtest --symbols SPY QQQ IWM
python main.py backtest --source csv                      # data/<SYMBOL>.csv (date,open,high,low,close,volume)
python main.py backtest --source synthetic                # offline random-walk demo data (not real!)
```

The backtester uses the same `SwingStrategy` and `RiskManager` code as the live bot.
Its execution model avoids look-ahead bias:

- Signals come from day *t*'s close. Entries and signal exits fill at day *t+1*'s open,
  plus slippage.
- The stop-loss is checked against each day's **low**. A gap through the stop fills
  at the open.
- Take-profit is checked against the **high**. When the stop and the target could both
  trigger on the same bar, the stop is assumed to hit first.
- The position limits, the gap guard and the daily loss limit match the live bot.

**Reported metrics:** total return, CAGR, **max drawdown**, Sharpe, number of trades,
**win rate**, **profit factor** (gross profit ÷ gross loss), average win and average
loss, expectancy per trade, average holding period, time in market, a breakdown of
exit reasons, and an equal-weight buy-and-hold benchmark of the watchlist. Results are
written to `backtest_results/trades.csv`, `equity_curve.csv` and `metrics.csv`.

Avoid overfitting. If you tune parameters on one period, check them on a period the
tuning never saw before you trust them.

---

## Project layout

```
swing_trading_bot/
├── main.py              # CLI: run / status / backtest, live-mode confirmation
├── config.yaml          # strategy, risk, order and backtest settings
├── requirements.txt
├── .env.example         # credential template (copy to .env)
├── swing_bot/
│   ├── config.py        # typed config loading and validation; credentials from env
│   ├── indicators.py    # RSI (Wilder), MACD, SMA/EMA in pure pandas
│   ├── strategy.py      # entry and exit rules
│   ├── risk.py          # position sizing, stops, position and daily-loss limits
│   ├── data.py          # Robinhood / yfinance / CSV / synthetic candle providers
│   ├── brokers.py       # RobinhoodBroker (live) and PaperBroker (simulated)
│   ├── bot.py           # trading loop, reconciliation, status display, shutdown
│   ├── backtest.py      # portfolio backtester and metrics
│   ├── state.py         # persistent JSON state (atomic writes)
│   ├── logging_setup.py # logging, CSV trade journal, webhook alerts
│   └── market_clock.py  # market-hours helpers
└── tests/               # pytest suite (offline; Robinhood is mocked)
```

---

## Limitations and known caveats

- **Long only.** It uses daily bars and isn't meant for intraday or high-frequency trading.
- **Market hours.** Paper mode uses a weekday 9:30–16:00 ET clock with no holiday
  calendar. Live mode asks Robinhood for the real calendar.
- **Fractional positions.** These can't have resting broker stop orders. Their stop is
  enforced only while the bot is running.
- **Pattern Day Trader rule.** A margin account under $25k is limited to 3 day trades
  in 5 business days. This bot holds positions for days, but a stop hit on the entry
  day counts as a day trade.
- **Taxes.** Short-term gains are taxed as ordinary income, and wash-sale rules apply.
- **Robinhood API changes.** If the API changes, `robin_stocks` calls can start
  failing. Keep `robin_stocks` up to date and watch the alerts.
- **No guarantee of profit.** Expect the defaults to lose money in some market
  regimes. Backtest and paper trade before risking capital.

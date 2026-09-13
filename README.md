# Second-Brain-
second brain

## Robinhood Swing Trading Bot

A Python swing trading bot that monitors a watchlist, generates buy/sell
signals from RSI + MACD + moving-average confirmation, sizes and manages
positions with fixed-fractional risk, and executes orders on Robinhood via
[`robin_stocks`](https://github.com/jmfernandes/robin_stocks).

**This is for educational/research use. Trading involves real financial
risk. Read the safety notes below before ever setting `CONFIRM_LIVE_TRADING=true`.**

### Project layout

```
swing_bot/
  config.py          Environment-driven configuration
  auth.py             Robinhood OAuth2 login (username/password/TOTP MFA)
  data_provider.py    Historical candlesticks, account equity, buying power
  indicators.py        RSI, MACD, SMA calculations (pandas-only)
  strategy.py         Signal generation (BUY/SELL/HOLD) from indicators
  risk_manager.py      Position sizing, exposure limits, daily loss breaker
  position_tracker.py Open position bookkeeping and P&L
  order_manager.py     Places market/limit orders, or simulates them (dry run)
  logger_setup.py       Console/file logging + CSV trade log
  bot.py               Main polling loop tying everything together
  backtest.py          Historical strategy backtester + performance metrics
main.py                CLI entry point (`run` / `backtest`)
tests/                  Unit tests for indicators, strategy, risk, positions
```

### Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your Robinhood credentials & settings
```

Never commit `.env` — it's already in `.gitignore`.

### Safety: dry run by default

The bot **will not place a live order** unless `CONFIRM_LIVE_TRADING=true`
is set in the environment. With it unset/false, every signal is logged and
simulated (a "paper" fill at the intended limit/market price) so you can
watch the bot's decisions, position tracking, and logging end-to-end with
zero financial risk. Flip it to `true` only after you've reviewed the
strategy and are comfortable with live execution.

### Running

```bash
# Paper trading loop (safe default) or live trading if CONFIRM_LIVE_TRADING=true
python main.py run

# Backtest the strategy over historical data before ever going live
python main.py backtest --symbols AAPL,MSFT,NVDA --span 5year --equity 10000 --verbose
```

### Strategy

- **Entry (BUY):** MACD bullish crossover, confirmed by an uptrend bias
  (20-day SMA above 50-day SMA) and RSI not already overbought.
- **Exit (SELL):** MACD bearish crossover, RSI overbought, a 20/50 SMA
  bearish crossover, or the position's stop-loss/take-profit being hit.

### Risk management

- Position size is capped so a stop-out risks at most `RISK_PER_TRADE_PCT`
  (default 1%) of account equity.
- At most `MAX_CONCURRENT_POSITIONS` (default 3) open positions at once.
- Trading new entries halts for the day once realized losses reach
  `DAILY_LOSS_LIMIT_PCT` of equity (open positions are still monitored and
  can still be exited on stop-loss/take-profit/sell signal).
- Buying power is validated before every order.

### Logging

- `logs/bot.log` — rotating operational log (signals, orders, errors).
- `logs/trades.csv` — one row per entry/exit with price, signal reason, and
  realized P&L.

### Tests

```bash
pytest tests/ -v
```

Covers indicator math, signal generation, position sizing/risk limits, and
position P&L bookkeeping — all pure functions that don't require network
access or Robinhood credentials.

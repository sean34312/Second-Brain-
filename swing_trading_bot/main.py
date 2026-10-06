#!/usr/bin/env python3
"""Robinhood swing trading bot - command line entry point.

    python main.py run                 # paper trading loop (default)
    python main.py run --once          # single cycle, then exit
    python main.py run --mode live     # LIVE trading (asks for confirmation)
    python main.py status              # account, positions and signals - no trading
    python main.py backtest            # backtest the strategy on historical data

EDUCATIONAL USE ONLY. Trading involves substantial risk of loss. See README.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from swing_bot import DISCLAIMER
from swing_bot.backtest import Backtester
from swing_bot.bot import SwingBot
from swing_bot.brokers import AuthenticationError, Broker, PaperBroker, RobinhoodBroker
from swing_bot.config import Config, ConfigError, Credentials, load_config
from swing_bot.data import DataError, DataProvider, make_provider
from swing_bot.logging_setup import Alerter, TradeJournal, setup_logging

log = logging.getLogger("main")

LIVE_CONFIRMATION = "I UNDERSTAND THE RISKS"


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def build_runtime(cfg: Config, creds: Credentials) -> tuple[Broker, DataProvider]:
    """Create the data provider and broker for the configured mode."""
    mode = cfg.bot.mode
    logged_in = False
    rh_broker = None
    if mode == "live" or (creds.has_robinhood and cfg.bot.data_source in ("auto", "robinhood")):
        # Live mode, or paper mode that should use Robinhood's real-time data.
        from swing_bot.data import RobinhoodDataProvider
        data = RobinhoodDataProvider()
        rh_broker = RobinhoodBroker(data, cfg.orders, creds)
        rh_broker.connect()
        logged_in = True
    data = make_provider(cfg.bot.data_source, csv_dir=cfg.path(cfg.bot.csv_data_dir), robinhood_ready=logged_in)
    if mode == "live":
        rh_broker.data = data
        return rh_broker, data
    broker = PaperBroker(data, cfg.orders, cfg.path(cfg.bot.paper_account_file),
                         starting_cash=cfg.bot.paper_starting_cash, slippage_pct=cfg.bot.paper_slippage_pct)
    return broker, data


def confirm_live(skip_prompt: bool) -> bool:
    print("\n" + "!" * 78)
    print(" LIVE TRADING MODE - real orders will be sent to your Robinhood account.")
    print(" " + DISCLAIMER.replace("\n", "\n "))
    print("!" * 78)
    if skip_prompt:
        log.warning("Live trading confirmed via --yes-i-understand-the-risks")
        return True
    if not sys.stdin.isatty():
        print("Refusing to start live trading without an interactive confirmation "
              "(pass --yes-i-understand-the-risks to override).")
        return False
    answer = input(f'Type "{LIVE_CONFIRMATION}" to continue: ').strip()
    return answer == LIVE_CONFIRMATION


def cmd_run(cfg: Config, args) -> int:
    creds = Credentials.from_env()
    if cfg.bot.mode == "live":
        if not creds.has_robinhood:
            log.error("Live mode needs ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD (see .env.example)")
            return 2
        if args.command == "run" and not confirm_live(args.yes_i_understand_the_risks):
            print("Live trading not confirmed - exiting.")
            return 1
    alerter = Alerter(creds.alert_webhook_url)
    try:
        broker, data = build_runtime(cfg, creds)
    except (AuthenticationError, DataError) as e:
        alerter.alert(f"Startup failed: {e}", critical=True)
        return 2
    journal = TradeJournal(cfg.path(cfg.bot.trade_log_file), cfg.bot.mode)
    bot = SwingBot(cfg, broker, data, journal, alerter)
    if args.command == "status":
        account = broker.get_account()
        bot._reconcile(broker.get_positions())
        symbols = sorted(set(cfg.bot.watchlist) | set(bot.state.positions))
        quotes = broker.get_quotes(symbols)
        signals = {s: bot._entry_signal(s, bot._bars(s)) for s in cfg.bot.watchlist}
        bot.print_status(account, quotes, signals, broker.is_market_open())
        return 0
    bot.install_signal_handlers()
    bot.run(once=args.once)
    return 0


def cmd_backtest(cfg: Config, args) -> int:
    bt = cfg.backtest
    if args.symbols:
        cfg.bot.watchlist = [s.upper() for s in args.symbols]
    start = args.start or bt.start
    end = args.end or bt.end or date.today().isoformat()
    source = args.source or bt.data_source
    creds = Credentials.from_env()
    logged_in = False
    if source == "robinhood":
        RobinhoodBroker(None, cfg.orders, creds).connect()  # type: ignore[arg-type]
        logged_in = True
    provider = make_provider(source, csv_dir=cfg.path(cfg.bot.csv_data_dir), robinhood_ready=logged_in)

    # Fetch extra history before `start` so indicators are warmed up on day one.
    warmup_days = int(max(cfg.strategy.sma_slow, cfg.strategy.macd_slow + cfg.strategy.macd_signal) * 1.6) + 30
    fetch_start = (date.fromisoformat(start) - timedelta(days=warmup_days)).isoformat()
    data = {}
    for sym in cfg.bot.watchlist:
        try:
            data[sym] = provider.history(sym, fetch_start, end)
            log.info("Loaded %d bars for %s (%s)", len(data[sym]), sym, provider.name)
        except DataError as e:
            log.error("Skipping %s: %s", sym, e)
    if not data:
        log.error("No data loaded - nothing to backtest")
        return 2
    if source == "synthetic":
        print("NOTE: synthetic random-walk data - results say nothing about real markets.")

    result = Backtester(cfg).run(data, start=start)
    print(result.report())
    out_dir = cfg.path(bt.output_dir)
    result.save(out_dir)
    print(f"Trades, equity curve and metrics written to {out_dir}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Robinhood swing trading bot (educational).")
    parser.add_argument("--config", default="config.yaml", help="path to config YAML (default: config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the trading loop")
    run.add_argument("--mode", choices=["paper", "live"], help="override bot.mode from the config")
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.add_argument("--yes-i-understand-the-risks", action="store_true",
                     help="skip the interactive live-trading confirmation")

    status = sub.add_parser("status", help="print account, positions and signals (no trading)")
    status.add_argument("--mode", choices=["paper", "live"], help="override bot.mode from the config")

    bt = sub.add_parser("backtest", help="backtest the strategy")
    bt.add_argument("--start", help="YYYY-MM-DD (default: backtest.start)")
    bt.add_argument("--end", help="YYYY-MM-DD (default: today)")
    bt.add_argument("--source", choices=["yfinance", "csv", "synthetic", "robinhood"])
    bt.add_argument("--symbols", nargs="+", help="override the watchlist")

    args = parser.parse_args(argv)
    load_env()
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if getattr(args, "mode", None):
        cfg.bot.mode = args.mode
    setup_logging(cfg.path(cfg.bot.log_dir), args.verbose)
    log.info("Disclaimer: %s", DISCLAIMER.replace("\n", " "))

    if args.command in ("run", "status"):
        return cmd_run(cfg, args)
    return cmd_backtest(cfg, args)


if __name__ == "__main__":
    sys.exit(main())

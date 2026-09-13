#!/usr/bin/env python3
"""CLI entry point for the swing trading bot.

Usage:
    python main.py run                          # live/paper trading loop
    python main.py backtest --symbols AAPL,MSFT  # strategy backtest
"""
from __future__ import annotations

import argparse
import sys

from swing_bot import auth, data_provider as dp
from swing_bot.backtest import Backtester
from swing_bot.bot import SwingTradingBot
from swing_bot.config import BotConfig


def cmd_run(args: argparse.Namespace) -> None:
    config = BotConfig()
    bot = SwingTradingBot(config)
    if not bot.orders.live:
        print(
            "NOTE: CONFIRM_LIVE_TRADING is not set to 'true' — running in DRY RUN "
            "(paper trading) mode. No real orders will be placed.\n"
        )
    bot.run_forever()


def cmd_backtest(args: argparse.Namespace) -> None:
    config = BotConfig()
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else config.watchlist

    auth.login(config)
    try:
        price_data = {
            symbol: dp.get_historicals(symbol, config.historical_interval, args.span)
            for symbol in symbols
        }
    finally:
        auth.logout()

    backtester = Backtester(config, starting_equity=args.equity)
    result = backtester.run(price_data)
    print(result.summary())

    if args.verbose:
        print("\nTrade log:")
        for t in result.trades:
            print(
                f"  {t.symbol}: {t.entry_time.date()} @ {t.entry_price:.2f} -> "
                f"{t.exit_time.date()} @ {t.exit_price:.2f} | qty {t.quantity} | "
                f"P&L {t.pnl:+.2f} | {t.exit_reason}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Swing trading bot for Robinhood")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the live/paper trading loop")
    run_p.set_defaults(func=cmd_run)

    bt_p = sub.add_parser("backtest", help="Backtest the strategy on historical data")
    bt_p.add_argument("--symbols", type=str, default=None, help="Comma-separated symbols (default: WATCHLIST)")
    bt_p.add_argument("--span", type=str, default="5year", help="robin_stocks historical span (e.g. year, 5year)")
    bt_p.add_argument("--equity", type=float, default=10_000.0, help="Starting equity for the backtest")
    bt_p.add_argument("--verbose", action="store_true", help="Print every simulated trade")
    bt_p.set_defaults(func=cmd_backtest)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)

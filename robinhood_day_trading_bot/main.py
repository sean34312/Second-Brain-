#!/usr/bin/env python3
"""Entrypoint. See README.md before running this against a real account.

    python main.py

Reads all configuration from environment variables (via .env) and
bot/config.py. The single most important setting is PAPER_TRADING in your
.env file, which defaults to true.
"""
from bot.bot_engine import TradingBot
from bot.config import CONFIG


def main() -> None:
    bot = TradingBot(CONFIG)
    bot.run()


if __name__ == "__main__":
    main()

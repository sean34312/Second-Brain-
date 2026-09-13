"""Robinhood authentication.

Robinhood does not publish an official trading API. This project uses
``robin_stocks``, a community library that drives the same private endpoints
the Robinhood mobile app uses. That means:

  * There is no public WebSocket market-data feed to subscribe to. "Real-time"
    here means polling REST endpoints on a short interval (see
    ``bot/data_feed.py``), not a true push stream.
  * Automated use of these endpoints is against Robinhood's Terms of Service.
    Running this bot against a live account is done at your own risk,
    including the risk of the account being restricted or closed.

Log in once, interactively, so you can hand-enter an MFA code if Robinhood
challenges the login. robin_stocks caches a device token after the first
successful login so subsequent runs usually don't re-prompt.
"""
from __future__ import annotations

import os

import robin_stocks.robinhood as rh

from bot.logger_setup import setup_logging

logger = setup_logging(__name__)


def login() -> None:
    username = os.getenv("ROBINHOOD_USERNAME")
    password = os.getenv("ROBINHOOD_PASSWORD")
    mfa_code = os.getenv("ROBINHOOD_MFA_CODE") or None

    if not username or not password:
        raise RuntimeError(
            "ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD are not set. Copy "
            ".env.example to .env and fill them in."
        )

    logger.info("Logging into Robinhood as %s", username)
    rh.login(
        username=username,
        password=password,
        mfa_code=mfa_code,
        store_session=True,
    )
    logger.info("Login successful.")


def logout() -> None:
    rh.logout()

"""Robinhood OAuth2 authentication via robin_stocks.

Credentials are read only from environment variables (via BotConfig), never
hard-coded or logged. If RH_TOTP_SECRET is set, a fresh TOTP code is
generated for MFA automatically; robin_stocks caches the resulting session
token locally so subsequent logins in the same environment can skip MFA.
"""
from __future__ import annotations

import logging

import robin_stocks.robinhood as rh

logger = logging.getLogger("swing_bot")


def login(config) -> None:
    if not config.rh_username or not config.rh_password:
        raise RuntimeError(
            "RH_USERNAME and RH_PASSWORD must be set (via environment variables or .env)"
        )

    mfa_code = None
    if config.rh_totp_secret:
        import pyotp

        mfa_code = pyotp.TOTP(config.rh_totp_secret).now()

    logger.info("Logging into Robinhood as %s", _mask(config.rh_username))
    rh.login(
        username=config.rh_username,
        password=config.rh_password,
        mfa_code=mfa_code,
        store_session=True,
    )
    logger.info("Robinhood login successful")


def logout() -> None:
    rh.logout()
    logger.info("Logged out of Robinhood")


def _mask(email: str) -> str:
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return f"{name[0]}***@{domain}"
    return f"{name[0]}{'*' * (len(name) - 2)}{name[-1]}@{domain}"

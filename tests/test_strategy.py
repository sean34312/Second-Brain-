import pandas as pd

from swing_bot.config import BotConfig
from swing_bot.strategy import Signal, SwingStrategy


def make_df(rsi_prev, rsi_cur, macd_prev, macd_sig_prev, macd_cur, macd_sig_cur, sma_s, sma_l, close=100.0):
    return pd.DataFrame(
        {
            "close": [close, close],
            "rsi": [rsi_prev, rsi_cur],
            "macd": [macd_prev, macd_cur],
            "macd_signal": [macd_sig_prev, macd_sig_cur],
            "sma_short": [sma_s, sma_s],
            "sma_long": [sma_l, sma_l],
        }
    )


def test_buy_signal_on_bullish_macd_cross_in_uptrend():
    cfg = BotConfig()
    strat = SwingStrategy(cfg)
    df = make_df(
        rsi_prev=45, rsi_cur=50,
        macd_prev=-0.5, macd_sig_prev=-0.2,  # macd below signal
        macd_cur=0.3, macd_sig_cur=0.1,      # macd now above signal -> bullish cross
        sma_s=105, sma_l=100,                # uptrend
    )
    result = strat.evaluate(df)
    assert result.signal is Signal.BUY


def test_sell_signal_on_bearish_macd_cross():
    cfg = BotConfig()
    strat = SwingStrategy(cfg)
    df = make_df(
        rsi_prev=55, rsi_cur=50,
        macd_prev=0.3, macd_sig_prev=0.1,
        macd_cur=-0.2, macd_sig_cur=0.0,  # bearish cross
        sma_s=105, sma_l=100,
    )
    result = strat.evaluate(df)
    assert result.signal is Signal.SELL


def test_sell_signal_on_overbought_rsi():
    cfg = BotConfig()
    strat = SwingStrategy(cfg)
    df = make_df(
        rsi_prev=68, rsi_cur=72,  # overbought
        macd_prev=0.1, macd_sig_prev=0.05,
        macd_cur=0.2, macd_sig_cur=0.1,
        sma_s=105, sma_l=100,
    )
    result = strat.evaluate(df)
    assert result.signal is Signal.SELL


def test_hold_when_no_setup():
    cfg = BotConfig()
    strat = SwingStrategy(cfg)
    df = make_df(
        rsi_prev=50, rsi_cur=51,
        macd_prev=0.1, macd_sig_prev=0.2,
        macd_cur=0.15, macd_sig_cur=0.25,  # still below signal, no cross
        sma_s=105, sma_l=100,
    )
    result = strat.evaluate(df)
    assert result.signal is Signal.HOLD


def test_insufficient_history_returns_hold():
    cfg = BotConfig()
    strat = SwingStrategy(cfg)
    df = pd.DataFrame(
        {
            "close": [100.0],
            "rsi": [50.0],
            "macd": [0.1],
            "macd_signal": [0.2],
            "sma_short": [105.0],
            "sma_long": [100.0],
        }
    )
    result = strat.evaluate(df)
    assert result.signal is Signal.HOLD

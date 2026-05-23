"""
core.py - shared building blocks used by BOTH the dashboard (app.py) and the
alerts script (alerts.py). Keeping these in one place means a fix or change to
the rule shows up in the dashboard AND the alerts at the same time.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Dad's watchlist. Edit a line here to add/remove a stock.
# The ".NS" suffix tells Yahoo Finance this is an Indian NSE stock.
# ---------------------------------------------------------------------------
STOCKS: dict[str, str] = {
    "TCS": "TCS.NS",
    "Vedanta": "VEDL.NS",
    "Samvardhana Motherson": "MOTHERSON.NS",
    "Sterlite Technologies": "STLTECH.NS",
    "NMDC": "NMDC.NS",
    "Power Finance Corp (PFC)": "PFC.NS",
    "REC Ltd": "RECLTD.NS",
}


# ---------------------------------------------------------------------------
# Data fetching with retries (handles Yahoo's "slow down" rate-limit gracefully).
# ---------------------------------------------------------------------------
def fetch_history(ticker: str, period: str = "6y") -> pd.DataFrame:
    for attempt, wait in enumerate([0, 3, 8, 15], start=1):
        if wait:
            time.sleep(wait)
        try:
            df = yf.download(
                ticker, period=period, interval="1d",
                progress=False, auto_adjust=True, threads=False,
            )
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0] for c in df.columns]
                df = df.reset_index()
                df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
                return df
        except Exception:
            pass
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Indicator maths (kept simple and well-known).
# ---------------------------------------------------------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Trend: short-term vs long-term average price
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()

    # Strength meter (RSI 14): 0-100. >70 = stretched high, <30 = beaten down
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))

    # Momentum (MACD): is the move gaining or losing steam
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_signal"]

    return df


def read_signals(df: pd.DataFrame) -> dict:
    """Turn the numbers into a plain-English read."""
    last = df.iloc[-1]

    trend_up = bool(last["SMA20"] > last["SMA50"]) and bool(
        last["Close"] > last["SMA50"]
    )

    rsi = float(last["RSI"]) if pd.notna(last["RSI"]) else 50.0
    if rsi >= 70:
        rsi_state = "stretched high (overbought)"
    elif rsi <= 30:
        rsi_state = "beaten down (oversold)"
    else:
        rsi_state = "in the normal range"

    momentum_up = bool(last["MACD_hist"] > 0)
    momentum_word = "gaining steam" if momentum_up else "losing steam"

    if trend_up and rsi < 70 and momentum_up:
        sentence = (
            "Uptrend and still has room to run - this looks like a "
            "POSSIBLE ENTRY zone."
        )
        tone = "good"
    elif trend_up and rsi >= 70:
        sentence = (
            "Still in an uptrend but the price is stretched high - be careful "
            "chasing it; a pullback is possible."
        )
        tone = "warn"
    elif trend_up and not momentum_up:
        sentence = (
            "Uptrend, but momentum is fading - hold/watch, and keep an eye "
            "out for a possible EXIT if it weakens further."
        )
        tone = "warn"
    elif (not trend_up) and rsi <= 30:
        sentence = (
            "Downtrend but very beaten down - a bounce is possible, but this "
            "is risky; not a clear entry."
        )
        tone = "warn"
    else:
        sentence = (
            "Downtrend and weak - better to WAIT / stay out until the trend "
            "turns up again."
        )
        tone = "bad"

    return {
        "trend_up": trend_up,
        "rsi": rsi,
        "rsi_state": rsi_state,
        "momentum_word": momentum_word,
        "momentum_up": momentum_up,
        "sentence": sentence,
        "tone": tone,
    }

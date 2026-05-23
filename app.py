"""
Stock Dashboard - Step 2: Data + Charts + Plain-English Signals + Backtest
--------------------------------------------------------------------------
A simple web dashboard for Indian (NSE) stocks.

What it does (the four phases, in plain words):
  1. Get data  -> free daily price history from Yahoo Finance
  2. Charts     -> clean price chart + the two trend lines
  3. Analyze    -> trend, an overbought/oversold meter, and momentum
  4. Signals    -> ONE plain-English line ("possible entry" / "watch exit" /
                   "stay out") + a backtest showing how a simple rule would
                   have done over the past few years.

IMPORTANT: This is a study / decision-support tool. It does NOT predict the
future and it does NOT place trades. The final call is always the user's.
"""

import datetime as dt

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

# ---------------------------------------------------------------------------
# Dad's watchlist (NSE). The ".NS" suffix tells Yahoo Finance this is an
# Indian NSE stock. All tickers verified working on 2026-05-17.
# To add/remove a stock later, just edit a line here.
# ---------------------------------------------------------------------------
STOCKS = {
    "TCS": "TCS.NS",
    "Vedanta": "VEDL.NS",
    "Samvardhana Motherson": "MOTHERSON.NS",
    "Sterlite Technologies": "STLTECH.NS",
    "NMDC": "NMDC.NS",
    "Power Finance Corp (PFC)": "PFC.NS",
    "REC Ltd": "RECLTD.NS",
}

PERIODS = {
    "1 Month": "1mo",
    "3 Months": "3mo",
    "6 Months": "6mo",
    "1 Year": "1y",
    "2 Years": "2y",
    "5 Years": "5y",
}

BACKTEST_PERIODS = {
    "1 Year": 252,
    "2 Years": 504,
    "3 Years": 756,
    "5 Years": 1260,
}


# ---------------------------------------------------------------------------
# Data fetching.
# Yahoo Finance throttles shared cloud IPs, so we do three things:
#   1. Use yf.download (the bulk endpoint is friendlier than .history())
#   2. Retry a few times with growing waits if it gets rate-limited
#   3. Cache the result for 24 hours so we ask Yahoo as little as possible
# We always pull ~6 years so the indicators and backtest have enough history,
# then trim to what the user asked to see.
# ---------------------------------------------------------------------------
import time


@st.cache_data(ttl=86400, show_spinner=False)
def get_full_history(ticker: str) -> pd.DataFrame:
    last_err = None
    for attempt, wait in enumerate([0, 3, 8, 15], start=1):
        if wait:
            time.sleep(wait)
        try:
            df = yf.download(
                ticker,
                period="6y",
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if df is not None and not df.empty:
                # yf.download returns a MultiIndex on columns sometimes -> flatten
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0] for c in df.columns]
                df = df.reset_index()
                df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
                return df
            last_err = "empty response"
        except Exception as e:  # rate-limit, network, etc.
            last_err = str(e)
    # All attempts failed
    return pd.DataFrame()


def format_inr(value: float) -> str:
    return f"₹{value:,.2f}"


# ---------------------------------------------------------------------------
# Indicator maths (kept simple and well-known)
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

    # One plain sentence + a colour ("good" / "warn" / "bad" / "neutral")
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


def run_backtest(df: pd.DataFrame, lookback_days: int) -> dict:
    """
    Simple, easy-to-explain rule:
      Own the stock ONLY while the short-term average (20 days) is above the
      long-term average (50 days). Otherwise sit in cash.
    We compare that against simply buying and holding the whole time.
    """
    data = df.dropna(subset=["SMA20", "SMA50"]).copy()
    data = data.tail(lookback_days).reset_index(drop=True)
    if len(data) < 30:
        return {"ok": False}

    data["in_market"] = (data["SMA20"] > data["SMA50"]).astype(int)
    data["ret"] = data["Close"].pct_change().fillna(0)
    # Act on yesterday's signal (no peeking at today's close)
    data["strat_ret"] = data["in_market"].shift(1).fillna(0) * data["ret"]

    data["hold_curve"] = (1 + data["ret"]).cumprod() * 100
    data["strat_curve"] = (1 + data["strat_ret"]).cumprod() * 100

    # Count completed trades + win rate
    pos = data["in_market"].values
    trades = []
    entry_price = None
    for i in range(1, len(data)):
        if pos[i] == 1 and pos[i - 1] == 0:
            entry_price = data["Close"].iloc[i]
        elif pos[i] == 0 and pos[i - 1] == 1 and entry_price is not None:
            exit_price = data["Close"].iloc[i]
            trades.append((exit_price - entry_price) / entry_price)
            entry_price = None
    if entry_price is not None:  # still holding at the end
        trades.append(
            (data["Close"].iloc[-1] - entry_price) / entry_price
        )

    n_trades = len(trades)
    wins = sum(1 for t in trades if t > 0)
    win_rate = (wins / n_trades * 100) if n_trades else 0.0

    return {
        "ok": True,
        "data": data,
        "strat_return": data["strat_curve"].iloc[-1] - 100,
        "hold_return": data["hold_curve"].iloc[-1] - 100,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "start": data["Date"].iloc[0],
        "end": data["Date"].iloc[-1],
    }


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Stock Dashboard", page_icon="\U0001F4C8", layout="wide")


def check_password() -> bool:
    expected = None
    try:
        expected = st.secrets.get("app_password")
    except Exception:
        expected = None

    if not expected:  # no password set -> no gate (local use)
        return True

    if st.session_state.get("auth_ok"):
        return True

    st.title("\U0001F512 Stock Dashboard")
    st.write("Please enter the password to continue.")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password. Please try again.")
    st.stop()
    return False


check_password()

st.title("\U0001F4C8 Stock Dashboard")
st.caption(
    "Version 2 - Charts + plain-English signals + backtest (Indian NSE, free "
    "daily data). A study tool - it does not predict the future or place trades."
)

# ---- Sidebar controls ----
with st.sidebar:
    st.header("Choose what to look at")
    stock_name = st.selectbox("Stock", list(STOCKS.keys()))
    period_label = st.selectbox("Chart time range", list(PERIODS.keys()), index=3)
    chart_style = st.radio("Chart style", ["Candlestick", "Line"], horizontal=True)
    show_volume = st.checkbox("Show volume", value=True)
    st.divider()
    bt_label = st.selectbox(
        "Backtest range", list(BACKTEST_PERIODS.keys()), index=2
    )
    st.divider()
    st.caption(
        "Data: Yahoo Finance (free, end-of-day). Prices can be delayed and "
        "are for study, not live trading."
    )

ticker = STOCKS[stock_name]

with st.spinner(f"Getting data for {stock_name}..."):
    full = get_full_history(ticker)

if full.empty:
    st.warning(
        f"Yahoo Finance is busy and asked us to slow down for **{stock_name}**.\n\n"
        "This happens sometimes on free hosting because many apps share the "
        "same server. **Wait a minute and refresh the page** "
        "(or pick a different stock first, then come back) - the data is "
        "cached for 24 hours once it loads, so it stays fast after."
    )
    st.stop()

full = add_indicators(full)

# Trim to the chart window the user picked
period_days = {
    "1mo": 22, "3mo": 66, "6mo": 132, "1y": 252, "2y": 504, "5y": 1260,
}[PERIODS[period_label]]
data = full.tail(period_days).reset_index(drop=True)

# ---- Top summary numbers ----
last_row = full.iloc[-1]
prev_close = full.iloc[-2]["Close"] if len(full) > 1 else last_row["Close"]
change = last_row["Close"] - prev_close
change_pct = (change / prev_close) * 100 if prev_close else 0.0

c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "Last close",
    format_inr(last_row["Close"]),
    f"{change:+.2f} ({change_pct:+.2f}%)",
)
c2.metric("Period high", format_inr(data["High"].max()))
c3.metric("Period low", format_inr(data["Low"].min()))
c4.metric("Trading days shown", f"{len(data)}")

st.subheader(f"{stock_name}  -  {period_label}")

tab_chart, tab_signals, tab_backtest = st.tabs(
    ["\U0001F4C8 Chart", "\U0001F6A6 Signals", "\U0001F501 Backtest"]
)

# ===========================================================================
# TAB 1 - CHART (price + the two trend lines)
# ===========================================================================
with tab_chart:
    fig = go.Figure()
    if chart_style == "Candlestick":
        fig.add_trace(
            go.Candlestick(
                x=data["Date"], open=data["Open"], high=data["High"],
                low=data["Low"], close=data["Close"], name="Price",
                increasing_line_color="#16a34a",
                decreasing_line_color="#dc2626",
            )
        )
    else:
        fig.add_trace(
            go.Scatter(
                x=data["Date"], y=data["Close"], mode="lines",
                name="Close price", line=dict(color="#2563eb", width=2),
            )
        )
    fig.add_trace(
        go.Scatter(
            x=data["Date"], y=data["SMA20"], mode="lines",
            name="20-day average (short-term trend)",
            line=dict(color="#f59e0b", width=1.5),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=data["Date"], y=data["SMA50"], mode="lines",
            name="50-day average (long-term trend)",
            line=dict(color="#7c3aed", width=1.5),
        )
    )
    fig.update_layout(
        height=520, margin=dict(l=10, r=10, t=10, b=10),
        xaxis_rangeslider_visible=False, yaxis_title="Price (INR)",
        template="plotly_white", hovermode="x unified",
        legend=dict(orientation="h", y=1.02, x=0),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "The orange line is the recent (20-day) average price; the purple "
        "line is the longer (50-day) average. Orange above purple usually "
        "means an uptrend; orange below usually means a downtrend."
    )

    if show_volume:
        st.subheader("Volume (how many shares traded)")
        vol_fig = go.Figure()
        vol_fig.add_trace(
            go.Bar(x=data["Date"], y=data["Volume"], name="Volume",
                   marker_color="#94a3b8")
        )
        vol_fig.update_layout(
            height=200, margin=dict(l=10, r=10, t=10, b=10),
            template="plotly_white",
        )
        st.plotly_chart(vol_fig, use_container_width=True)

    with st.expander("See the actual numbers (table)"):
        tbl = data[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
        tbl["Date"] = tbl["Date"].dt.strftime("%d %b %Y")
        st.dataframe(
            tbl.iloc[::-1].reset_index(drop=True),
            use_container_width=True, height=320,
        )

# ===========================================================================
# TAB 2 - SIGNALS (plain-English read)
# ===========================================================================
with tab_signals:
    sig = read_signals(full)

    st.markdown("#### What the numbers are saying")

    if sig["tone"] == "good":
        st.success("**" + sig["sentence"] + "**")
    elif sig["tone"] == "warn":
        st.warning("**" + sig["sentence"] + "**")
    elif sig["tone"] == "bad":
        st.error("**" + sig["sentence"] + "**")
    else:
        st.info("**" + sig["sentence"] + "**")

    st.write("")
    s1, s2, s3 = st.columns(3)
    with s1:
        st.metric(
            "Trend",
            "Up" if sig["trend_up"] else "Down",
        )
        st.caption(
            "Up = short-term average is above the long-term average "
            "(price generally rising)."
        )
    with s2:
        st.metric("Strength meter (0-100)", f"{sig['rsi']:.0f}")
        st.caption(
            f"This stock is **{sig['rsi_state']}**. Above 70 = stretched "
            "high; below 30 = beaten down; in between = normal."
        )
    with s3:
        st.metric("Momentum", sig["momentum_word"].title())
        st.caption(
            "Whether the recent move is picking up speed or slowing down."
        )

    st.divider()
    st.caption(
        "This is a plain reading of well-known indicators - a helper, not a "
        "guarantee. It does not know news, results, or the future. The "
        "decision is always yours."
    )

# ===========================================================================
# TAB 3 - BACKTEST (how a simple rule would have done)
# ===========================================================================
with tab_backtest:
    st.markdown("#### The simple rule we are testing")
    st.write(
        "**Own the stock only while the short-term (20-day) average is above "
        "the long-term (50-day) average. Otherwise stay in cash.** "
        "We compare this against simply buying once and holding the whole time."
    )

    bt = run_backtest(full, BACKTEST_PERIODS[bt_label])
    if not bt.get("ok"):
        st.info("Not enough history for this backtest range. Try a shorter one.")
    else:
        st.caption(
            f"Tested from {bt['start'].strftime('%d %b %Y')} to "
            f"{bt['end'].strftime('%d %b %Y')} ({bt_label})."
        )

        b1, b2, b3 = st.columns(3)
        b1.metric("Rule result", f"{bt['strat_return']:+.1f}%")
        b2.metric("Just buy & hold", f"{bt['hold_return']:+.1f}%")
        b3.metric(
            "Rule trades / win rate",
            f"{bt['n_trades']}  /  {bt['win_rate']:.0f}%",
        )

        eq = go.Figure()
        eq.add_trace(
            go.Scatter(
                x=bt["data"]["Date"], y=bt["data"]["strat_curve"],
                mode="lines", name="Following the rule",
                line=dict(color="#16a34a", width=2),
            )
        )
        eq.add_trace(
            go.Scatter(
                x=bt["data"]["Date"], y=bt["data"]["hold_curve"],
                mode="lines", name="Just buy & hold",
                line=dict(color="#94a3b8", width=2, dash="dot"),
            )
        )
        eq.update_layout(
            height=420, margin=dict(l=10, r=10, t=10, b=10),
            template="plotly_white", hovermode="x unified",
            yaxis_title="Value of ₹100 invested",
            legend=dict(orientation="h", y=1.02, x=0),
        )
        st.plotly_chart(eq, use_container_width=True)
        st.caption(
            "Both lines start at ₹100. This shows how each approach would "
            "have grown that ₹100 in the past. **Past results do NOT "
            "guarantee future results** - this only shows whether the rule "
            "has behaved sensibly on this stock's history."
        )

st.caption(
    f"Last updated: {dt.datetime.now().strftime('%d %b %Y, %I:%M %p')}  -  "
    "Study / decision-support tool. It does not predict the future and does "
    "not place trades."
)

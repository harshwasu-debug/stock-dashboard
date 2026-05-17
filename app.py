"""
Stock Dashboard - Step 1: Data + Charts
----------------------------------------
A simple web dashboard for Indian (NSE) stocks.

This is Version 1. It does the first two of the four phases:
  1. Get data  -> pulls free daily price history from Yahoo Finance
  2. Charts     -> draws a clean price chart + volume

Analysis + buy/sell signals come in the next step.
"""

import datetime as dt

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


# ---------------------------------------------------------------------------
# Data fetching. Cached for 1 hour so it does not re-download every click.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def get_data(ticker: str, period: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval="1d")
    if not df.empty:
        df = df.reset_index()
    return df


def format_inr(value: float) -> str:
    return f"₹{value:,.2f}"


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Stock Dashboard", page_icon="\U0001F4C8", layout="wide")


# ---------------------------------------------------------------------------
# Simple password gate.
# The password is read from Streamlit "secrets" (set in the cloud app settings,
# or in a local .streamlit/secrets.toml that is NEVER uploaded to GitHub).
# If no password is configured at all, the app stays open (e.g. local testing).
# ---------------------------------------------------------------------------
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
    "Version 1 - Data + Charts (Indian NSE stocks, free daily data). "
    "Analysis and buy/sell signals come next."
)

# ---- Sidebar controls ----
with st.sidebar:
    st.header("Choose what to look at")
    stock_name = st.selectbox("Stock", list(STOCKS.keys()))
    period_label = st.selectbox("Time range", list(PERIODS.keys()), index=3)
    chart_style = st.radio("Chart style", ["Candlestick", "Line"], horizontal=True)
    show_volume = st.checkbox("Show volume", value=True)
    st.divider()
    st.caption(
        "Data source: Yahoo Finance (free, end-of-day). "
        "Prices can be delayed and are for study, not live trading."
    )

ticker = STOCKS[stock_name]
period = PERIODS[period_label]

# ---- Fetch ----
with st.spinner(f"Getting data for {stock_name}..."):
    data = get_data(ticker, period)

if data.empty:
    st.error(
        f"Could not get data for {stock_name} ({ticker}). "
        "Yahoo Finance may be temporarily unavailable - try again in a moment."
    )
    st.stop()

# ---- Top summary numbers ----
last_row = data.iloc[-1]
prev_close = data.iloc[-2]["Close"] if len(data) > 1 else last_row["Close"]
change = last_row["Close"] - prev_close
change_pct = (change / prev_close) * 100 if prev_close else 0.0

period_high = data["High"].max()
period_low = data["Low"].min()

c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "Last close",
    format_inr(last_row["Close"]),
    f"{change:+.2f} ({change_pct:+.2f}%)",
)
c2.metric("Period high", format_inr(period_high))
c3.metric("Period low", format_inr(period_low))
c4.metric("Trading days", f"{len(data)}")

st.subheader(f"{stock_name}  -  {period_label}")

# ---- Main price chart ----
fig = go.Figure()

if chart_style == "Candlestick":
    fig.add_trace(
        go.Candlestick(
            x=data["Date"],
            open=data["Open"],
            high=data["High"],
            low=data["Low"],
            close=data["Close"],
            name="Price",
            increasing_line_color="#16a34a",
            decreasing_line_color="#dc2626",
        )
    )
else:
    fig.add_trace(
        go.Scatter(
            x=data["Date"],
            y=data["Close"],
            mode="lines",
            name="Close price",
            line=dict(color="#2563eb", width=2),
        )
    )

fig.update_layout(
    height=520,
    margin=dict(l=10, r=10, t=10, b=10),
    xaxis_rangeslider_visible=False,
    yaxis_title="Price (INR)",
    template="plotly_white",
    hovermode="x unified",
)
st.plotly_chart(fig, use_container_width=True)

# ---- Volume chart ----
if show_volume:
    st.subheader("Volume (how many shares traded)")
    vol_fig = go.Figure()
    vol_fig.add_trace(
        go.Bar(x=data["Date"], y=data["Volume"], name="Volume", marker_color="#94a3b8")
    )
    vol_fig.update_layout(
        height=220,
        margin=dict(l=10, r=10, t=10, b=10),
        template="plotly_white",
    )
    st.plotly_chart(vol_fig, use_container_width=True)

# ---- Raw data (optional peek) ----
with st.expander("See the actual numbers (table)"):
    table = data[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    table["Date"] = table["Date"].dt.strftime("%d %b %Y")
    st.dataframe(
        table.iloc[::-1].reset_index(drop=True),
        use_container_width=True,
        height=320,
    )

st.caption(
    f"Last updated: {dt.datetime.now().strftime('%d %b %Y, %I:%M %p')}  -  "
    "This tool helps you study the market. It does not predict the future "
    "and does not place trades."
)

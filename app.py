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

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import STOCKS, fetch_history, add_indicators, read_signals
from analyst import (
    SECTOR_INDEX, BROAD_INDEX,
    fetch_fundamentals, interpret_fundamentals,
    fetch_news, news_summary,
    fetch_earnings_date, days_to,
    fetch_index_change, relative_strength,
    compute_play, assemble_brief,
    compute_peer_comparison, fetch_macro, interpret_macro,
)
from brief_writer import write_brief

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
# Data fetching with Streamlit caching wrapped around the shared fetchers.
# Heavy stuff cached for 24h (price history) or 1h (fundamentals, news, indices).
# ---------------------------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def get_full_history(ticker: str) -> pd.DataFrame:
    return fetch_history(ticker, period="6y")


@st.cache_data(ttl=3600, show_spinner=False)
def cached_fundamentals(ticker: str) -> dict:
    return fetch_fundamentals(ticker)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_news(ticker: str) -> list:
    return fetch_news(ticker, max_items=5)


@st.cache_data(ttl=21600, show_spinner=False)
def cached_earnings_date(ticker: str):
    return fetch_earnings_date(ticker)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_index_change(symbol: str):
    return fetch_index_change(symbol)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_peer_comparison(name: str, fund_tuple: tuple) -> dict:
    # fund_tuple is just used as a stable cache key
    return compute_peer_comparison(name, dict(fund_tuple))


@st.cache_data(ttl=3600, show_spinner=False)
def cached_macro() -> dict:
    return fetch_macro()


@st.cache_data(ttl=21600, show_spinner=True)
def cached_brief(_ctx_key: str, ctx_payload: dict) -> tuple[str, str]:
    # _ctx_key is a stable key (stock + date); ctx_payload carries the data
    return write_brief(ctx_payload)


def format_inr(value: float) -> str:
    return f"₹{value:,.2f}"


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
    "Version 3 - Charts + plain-English signals + company health + news + "
    "sector context + the play (stop/target/size) + backtest. A decision-"
    "support tool - it does not predict the future or place trades."
)

# ---- Sidebar controls ----
with st.sidebar:
    st.header("View")
    mode = st.radio(
        "Mode",
        ["Morning Briefing (all stocks)", "Detail (one stock)"],
        index=0,
    )
    st.divider()
    st.header("Your risk settings")
    capital = st.number_input(
        "Trading capital (₹)", min_value=10000, max_value=100_000_000,
        value=100_000, step=10000, format="%d",
    )
    risk_pct = st.slider(
        "Max risk per trade (% of capital)", min_value=0.5, max_value=5.0,
        value=2.0, step=0.5,
    ) / 100.0
    st.caption(
        "Used to size each suggested trade so you never risk more than this % "
        "of your capital if the stop is hit."
    )
    st.divider()
    if mode == "Detail (one stock)":
        st.header("Choose what to look at")
        stock_name = st.selectbox("Stock", list(STOCKS.keys()))
        period_label = st.selectbox(
            "Chart time range", list(PERIODS.keys()), index=3
        )
        chart_style = st.radio(
            "Chart style", ["Candlestick", "Line"], horizontal=True
        )
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


# ===========================================================================
# Per-stock view assembler (used by both Morning Briefing and Detail mode)
# ===========================================================================
TONE_EMOJI = {"good": "\U0001F7E2", "warn": "\U0001F7E0",
              "bad": "\U0001F534", "neutral": "⚪"}


def build_view(
    name: str, capital: float, risk_pct: float,
    macro: dict | None = None,
) -> dict | None:
    tkr = STOCKS[name]
    df = get_full_history(tkr)
    if df.empty:
        return None
    df = add_indicators(df)
    sig = read_signals(df)
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
    change_pct = (last - prev) / prev * 100 if prev else 0.0

    fund = cached_fundamentals(tkr)
    interp = interpret_fundamentals(fund, last)
    news = cached_news(tkr)
    nsum = news_summary(news)
    edate = cached_earnings_date(tkr)
    edays = days_to(edate)
    sec_sym, sec_name = SECTOR_INDEX.get(name, (None, None))
    sec_ctx = cached_index_change(sec_sym) if sec_sym else None
    broad_ctx = cached_index_change(BROAD_INDEX[0])
    play = compute_play(df, capital=capital, risk_pct=risk_pct)
    peer_cmp = cached_peer_comparison(name, tuple(sorted(fund.items())))
    macro_flags = interpret_macro(macro) if macro else []

    # Build a JSON-safe context for the brief writer (cached separately)
    ctx_for_brief = {
        "name": name, "last_close": last, "change_pct": change_pct,
        "sig": sig, "fund": fund, "interp": interp,
        "news": [{"title": n["title"]} for n in news[:3]],
        "news_sum": nsum,
        "sector_name": sec_name, "sector_ctx": sec_ctx, "broad_ctx": broad_ctx,
        "earnings_in_days": edays,
        "play": play, "peer_cmp": peer_cmp, "macro_flags": macro_flags,
    }
    cache_key = f"{name}-{dt.date.today().isoformat()}"
    brief, brief_source = cached_brief(cache_key, ctx_for_brief)

    return {
        "name": name, "ticker": tkr, "df": df, "sig": sig,
        "last": last, "change_pct": change_pct,
        "fund": fund, "interp": interp,
        "news": news, "news_sum": nsum,
        "earnings_date": edate, "earnings_in_days": edays,
        "sector_name": sec_name, "sector_ctx": sec_ctx, "broad_ctx": broad_ctx,
        "play": play, "peer_cmp": peer_cmp, "macro_flags": macro_flags,
        "brief": brief, "brief_source": brief_source,
    }


# ===========================================================================
# MORNING BRIEFING - analyst-style paragraph cards
# ===========================================================================
def render_macro_banner(macro: dict) -> None:
    if not macro:
        return
    cols = st.columns(len(macro))
    for col, (key, m) in zip(cols, macro.items()):
        col.metric(
            m["label"], f"{m['last']:.2f}",
            f"{m['day_pct']:+.2f}%",
        )
    flags = interpret_macro(macro)
    risky = [f for f in flags
             if "VIX high" in f or "Rupee" in f or "Crude" in f]
    if risky:
        st.caption("**Macro flags:** " + "  ·  ".join(risky))


def render_morning_briefing() -> None:
    st.subheader("\U0001F305 Morning Briefing")
    st.write(
        "An analyst-style note on each of your stocks today. "
        "\U0001F7E2 = looks good · \U0001F7E0 = be careful · "
        "\U0001F534 = stay out. Open **Detail** in the sidebar for the full chart."
    )

    # Macro banner across the top
    macro = cached_macro()
    with st.container(border=True):
        st.markdown("**Today's macro screen**")
        render_macro_banner(macro)

    problems = []
    views: list[dict] = []
    progress = st.progress(0.0, text="Reading your stocks...")
    items = list(STOCKS.keys())
    for i, name in enumerate(items):
        v = build_view(name, capital, risk_pct, macro=macro)
        progress.progress((i + 1) / len(items), text=f"Read {name}")
        if v is None:
            problems.append(name)
            continue
        views.append(v)
    progress.empty()

    if problems:
        st.warning(
            "Yahoo Finance was busy for: " + ", ".join(problems) +
            ". Refresh in a minute to load them."
        )

    # Rank: greens first, then warns, then reds (good news on top)
    tone_order = {"good": 0, "warn": 1, "neutral": 2, "bad": 3}
    views.sort(key=lambda v: tone_order.get(v["sig"]["tone"], 4))

    # ---- Top: grouped actionables (the very-fast-scan version) ----
    if views:
        greens = [v for v in views if v["sig"]["tone"] == "good"]
        oranges = [v for v in views if v["sig"]["tone"] == "warn"]
        reds = [v for v in views if v["sig"]["tone"] == "bad"]
        st.markdown("##### Today's actionables")
        if greens:
            st.success(
                "\U0001F7E2 **Looks good (possible entry):** "
                + ", ".join(v["name"] for v in greens)
            )
        if oranges:
            st.warning(
                "\U0001F7E0 **Be careful (watch / possible exit):** "
                + ", ".join(v["name"] for v in oranges)
            )
        if reds:
            st.error(
                "\U0001F534 **Stay out for now:** "
                + ", ".join(v["name"] for v in reds)
            )
        st.divider()

    # ---- Per-stock analyst card ----
    for v in views:
        with st.container(border=True):
            head_l, head_r = st.columns([3, 2])
            with head_l:
                st.markdown(
                    f"### {TONE_EMOJI[v['sig']['tone']]} {v['name']}"
                )
                st.caption(
                    (v["sector_name"] or "—") + " · "
                    + (v["fund"].get("industry") or "")
                )
            with head_r:
                st.metric(
                    "Last close",
                    f"₹{v['last']:,.2f}",
                    f"{v['change_pct']:+.2f}%",
                )

            st.markdown(f"**{v['brief']}**")

            # Quick fundamental tags
            tag_bits = []
            if v["fund"].get("trailing_pe") is not None:
                tag_bits.append(f"P/E {v['fund']['trailing_pe']:.1f}")
            if v["fund"].get("roe") is not None:
                roe = v["fund"]["roe"]
                roe_pct = roe * 100 if abs(roe) < 5 else roe
                tag_bits.append(f"ROE {roe_pct:.1f}%")
            if v["fund"].get("dividend_yield") is not None:
                tag_bits.append(f"Yield {v['fund']['dividend_yield']:.2f}%")
            if v["interp"].get("pos_in_52w_pct") is not None:
                tag_bits.append(
                    f"At {v['interp']['pos_in_52w_pct']:.0f}% of 52-wk range"
                )
            if v["earnings_in_days"] is not None and 0 <= v["earnings_in_days"] <= 30:
                tag_bits.append(
                    f"⚠️ Earnings in {v['earnings_in_days']}d"
                )
            if tag_bits:
                st.caption("  ·  ".join(tag_bits))

            # Optional: the play box for actionable signals
            if v["play"] and v["sig"]["tone"] in ("good", "warn"):
                p = v["play"]
                pc1, pc2, pc3, pc4 = st.columns(4)
                pc1.metric("Entry near", f"₹{p['entry']:.2f}")
                pc2.metric("Stop loss", f"₹{p['stop']:.2f}")
                pc3.metric("First target", f"₹{p['target']:.2f}")
                pc4.metric(
                    "Suggested size",
                    f"{p['shares']} sh",
                    f"≈ ₹{p['trade_value']:,.0f}",
                )

            # Latest news (collapsed by default)
            if v["news"]:
                with st.expander(f"Latest headlines ({len(v['news'])})"):
                    for n in v["news"]:
                        tone = {"positive": "\U0001F7E2",
                                "negative": "\U0001F534",
                                "neutral": "⚪"}[n["sentiment"]]
                        date_str = (
                            n["date"].strftime("%d %b")
                            if n.get("date") else "recent"
                        )
                        link = n.get("url") or "#"
                        st.markdown(
                            f"- {tone} *{date_str}* — [{n['title']}]({link})"
                        )

    # Source badge (AI or rule)
    sources = {v.get("brief_source", "rule") for v in views}
    if "gemini" in sources:
        st.caption("✨ Briefs written by Google Gemini (free).")
    elif "claude" in sources:
        st.caption("✨ Briefs written by Claude.")
    else:
        st.caption(
            "Briefs are rule-based. To switch on free AI-written briefs, set "
            "the `GEMINI_API_KEY` secret in your Streamlit Cloud app settings."
        )
    st.caption(
        f"Briefing generated: {dt.datetime.now().strftime('%d %b %Y, %I:%M %p')}. "
        "A helper, not a fortune teller. The decision is always yours."
    )


# Route by mode
if mode == "Morning Briefing (all stocks)":
    render_morning_briefing()
    st.stop()

# ---- Detail mode below ----
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

tab_chart, tab_signals, tab_analyst, tab_backtest = st.tabs(
    ["\U0001F4C8 Chart", "\U0001F6A6 Signals", "\U0001F9D1 Analyst",
     "\U0001F501 Backtest"]
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
# TAB 3 - ANALYST (company health + news + sector context + the play)
# ===========================================================================
with tab_analyst:
    v = build_view(stock_name, capital, risk_pct, macro=cached_macro())
    if v is None:
        st.warning(
            "Could not build analyst view (Yahoo busy). Refresh in a minute."
        )
    else:
        st.markdown("##### Today's read")
        if v["sig"]["tone"] == "good":
            st.success(v["brief"])
        elif v["sig"]["tone"] == "warn":
            st.warning(v["brief"])
        elif v["sig"]["tone"] == "bad":
            st.error(v["brief"])
        else:
            st.info(v["brief"])
        source_label = {
            "gemini": "✨ Written by Gemini (free AI)",
            "claude": "✨ Written by Claude",
            "rule":   "Rule-based note (no AI key configured)",
        }.get(v.get("brief_source", "rule"), "")
        st.caption(source_label)

        st.divider()

        # ---- Company Health ----
        st.markdown("##### Company health")
        f = v["fund"]
        h1, h2, h3, h4 = st.columns(4)
        if f.get("trailing_pe") is not None:
            h1.metric("Price/Earnings", f"{f['trailing_pe']:.1f}")
            h1.caption("Lower = cheaper on earnings.")
        if f.get("roe") is not None:
            roe = f["roe"]
            roe_pct = roe * 100 if abs(roe) < 5 else roe
            h2.metric("Return on Equity", f"{roe_pct:.1f}%")
            h2.caption(">15% is generally good.")
        if f.get("debt_to_equity") is not None:
            h3.metric("Debt / Equity", f"{f['debt_to_equity']:.0f}")
            h3.caption("Lower = less indebted. >150 = high.")
        if f.get("dividend_yield") is not None:
            h4.metric("Dividend yield", f"{f['dividend_yield']:.2f}%")
            h4.caption("Annual dividend as % of price.")

        h5, h6, h7, h8 = st.columns(4)
        if f.get("earnings_growth") is not None:
            h5.metric("Earnings growth (YoY)", f"{f['earnings_growth']*100:+.1f}%")
        if f.get("revenue_growth") is not None:
            h6.metric("Revenue growth (YoY)", f"{f['revenue_growth']*100:+.1f}%")
        if f.get("fifty_two_low") and f.get("fifty_two_high"):
            h7.metric(
                "52-week range",
                f"₹{f['fifty_two_low']:,.0f} – ₹{f['fifty_two_high']:,.0f}",
            )
            if v["interp"].get("pos_in_52w_pct") is not None:
                h8.metric(
                    "Where in the range",
                    f"{v['interp']['pos_in_52w_pct']:.0f}%",
                )
                h8.caption("0% = year low, 100% = year high.")

        st.divider()

        # ---- News & Events ----
        st.markdown("##### News and events")
        nc1, nc2 = st.columns([2, 1])
        with nc1:
            if v["news"]:
                for n in v["news"]:
                    tone = {"positive": "\U0001F7E2",
                            "negative": "\U0001F534",
                            "neutral": "⚪"}[n["sentiment"]]
                    date_str = (
                        n["date"].strftime("%d %b")
                        if n.get("date") else "recent"
                    )
                    link = n.get("url") or "#"
                    st.markdown(
                        f"- {tone} *{date_str}* — [{n['title']}]({link})"
                    )
            else:
                st.caption("No recent headlines found.")
        with nc2:
            if v["earnings_date"]:
                st.metric("Next earnings", v["earnings_date"].strftime("%d %b %Y"))
                if v["earnings_in_days"] is not None:
                    if v["earnings_in_days"] <= 10:
                        st.warning(
                            f"Only {v['earnings_in_days']} day(s) to go - "
                            "results can move the stock sharply."
                        )
                    else:
                        st.caption(f"In {v['earnings_in_days']} days.")
            st.caption(
                "Headline tone is rule-based (keyword matching) - good for a "
                "first scan, not a substitute for reading the articles."
            )

        st.divider()

        # ---- Peer comparison ----
        pc = v.get("peer_cmp") or {}
        if pc.get("peer_count"):
            st.markdown(f"##### Vs peers ({pc['peer_count']} similar companies)")
            f = v["fund"]
            pp1, pp2, pp3 = st.columns(3)
            if pc.get("pe_median") is not None and f.get("trailing_pe") is not None:
                delta = f["trailing_pe"] - pc["pe_median"]
                pp1.metric(
                    "P/E vs peer median",
                    f"{f['trailing_pe']:.1f} vs {pc['pe_median']:.1f}",
                    f"{delta:+.1f}",
                    delta_color="inverse",  # lower P/E is better
                )
            if pc.get("roe_median") is not None and f.get("roe") is not None:
                f_roe = f["roe"] * 100 if abs(f["roe"]) < 5 else f["roe"]
                p_roe = pc["roe_median"] * 100 if abs(pc["roe_median"]) < 5 else pc["roe_median"]
                pp2.metric(
                    "ROE vs peer median",
                    f"{f_roe:.1f}% vs {p_roe:.1f}%",
                    f"{f_roe - p_roe:+.1f} pp",
                )
            if pc.get("yield_median") is not None and f.get("dividend_yield") is not None:
                pp3.metric(
                    "Dividend yield vs peers",
                    f"{f['dividend_yield']:.2f}% vs {pc['yield_median']:.2f}%",
                    f"{f['dividend_yield'] - pc['yield_median']:+.2f} pp",
                )
            if pc.get("notes"):
                st.caption("**Reads:** " + "  ·  ".join(pc["notes"]))
            st.divider()

        # ---- Sector & Market Context ----
        st.markdown("##### Sector and market context")
        if v["sector_ctx"] or v["broad_ctx"]:
            cc1, cc2, cc3 = st.columns(3)
            if v["sector_ctx"]:
                cc1.metric(
                    f"{v['sector_name']} (today)",
                    f"{v['sector_ctx']['day_pct']:+.2f}%",
                )
                cc2.metric(
                    f"{v['sector_name']} (1 month)",
                    f"{v['sector_ctx']['month_pct']:+.1f}%",
                )
            if v["broad_ctx"]:
                cc3.metric(
                    "Nifty 50 (1 month)",
                    f"{v['broad_ctx']['month_pct']:+.1f}%",
                )
            # Relative strength sentence
            if v["sector_ctx"]:
                # Stock's 1m change
                if len(v["df"]) >= 22:
                    s_now = float(v["df"]["Close"].iloc[-1])
                    s_then = float(v["df"]["Close"].iloc[-22])
                    s_month_pct = (s_now - s_then) / s_then * 100 if s_then else 0
                    st.write(
                        f"**{stock_name}** is **{relative_strength(s_month_pct, v['sector_ctx']['month_pct'])}**."
                    )
        else:
            st.caption("Sector/market data not available right now.")

        # Macro flags relevant to this stock
        if v.get("macro_flags"):
            risky = [f for f in v["macro_flags"]
                     if "VIX high" in f or "Rupee" in f or "Crude" in f]
            if risky:
                st.caption("**Macro context:** " + "  ·  ".join(risky))

        st.divider()

        # ---- The Play ----
        st.markdown("##### The play (if you choose to enter)")
        if v["play"] is None:
            st.info("Not enough data to compute a play.")
        elif v["sig"]["tone"] == "bad":
            st.warning(
                "Signal is **stay out** - no entry suggested. The numbers "
                "below would only apply if you decide to enter anyway."
            )
        p = v["play"]
        if p:
            pl1, pl2, pl3, pl4 = st.columns(4)
            pl1.metric("Entry near", f"₹{p['entry']:.2f}")
            pl2.metric(
                "Stop loss", f"₹{p['stop']:.2f}",
                f"−{((p['entry']-p['stop'])/p['entry']*100):.1f}%",
            )
            pl3.metric(
                "First target", f"₹{p['target']:.2f}",
                f"+{((p['target']-p['entry'])/p['entry']*100):.1f}%",
            )
            pl4.metric(
                "Suggested size",
                f"{p['shares']} sh",
                f"≈ ₹{p['trade_value']:,.0f}",
            )
            pl5, pl6, pl7 = st.columns(3)
            pl5.metric("₹ at risk", f"₹{p['risk_rupees']:,.0f}")
            pl6.metric("₹ potential reward", f"₹{p['reward_rupees']:,.0f}")
            pl7.metric("Reward : Risk", "2 : 1")
            st.caption(
                "Stop is set ~2× recent daily range below entry. Target is "
                "set at 2× risk so a win pays twice what a loss costs. "
                "Size is calculated so a hit stop loses no more than your "
                f"chosen {risk_pct*100:.1f}% of capital "
                f"(₹{capital:,.0f} × {risk_pct*100:.1f}% = ₹{p['risk_rupees']:,.0f})."
            )

        st.caption(
            "All numbers above are rule-based suggestions, not personal "
            "advice. The decision is always yours."
        )

# ===========================================================================
# TAB 4 - BACKTEST (how a simple rule would have done)
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

        # ---- Price + the two trend lines over the backtest window ----
        st.markdown("**Price with the two trend lines** (green shading = "
                    "periods the rule was holding the stock):")
        bt_df = bt["data"]
        ma_fig = go.Figure()
        ma_fig.add_trace(
            go.Scatter(
                x=bt_df["Date"], y=bt_df["Close"], mode="lines",
                name="Price", line=dict(color="#1f2937", width=1.5),
            )
        )
        ma_fig.add_trace(
            go.Scatter(
                x=bt_df["Date"], y=bt_df["SMA20"], mode="lines",
                name="20-day average (short-term)",
                line=dict(color="#f59e0b", width=1.5),
            )
        )
        ma_fig.add_trace(
            go.Scatter(
                x=bt_df["Date"], y=bt_df["SMA50"], mode="lines",
                name="50-day average (long-term)",
                line=dict(color="#7c3aed", width=1.5),
            )
        )
        # Shade contiguous "in market" stretches in light green
        in_mkt = bt_df["in_market"].values
        dates = bt_df["Date"].values
        run_start = None
        for i in range(len(in_mkt)):
            if in_mkt[i] == 1 and run_start is None:
                run_start = dates[i]
            elif in_mkt[i] == 0 and run_start is not None:
                ma_fig.add_vrect(
                    x0=run_start, x1=dates[i - 1],
                    fillcolor="#16a34a", opacity=0.10, line_width=0,
                )
                run_start = None
        if run_start is not None:
            ma_fig.add_vrect(
                x0=run_start, x1=dates[-1],
                fillcolor="#16a34a", opacity=0.10, line_width=0,
            )
        ma_fig.update_layout(
            height=380, margin=dict(l=10, r=10, t=10, b=10),
            template="plotly_white", hovermode="x unified",
            yaxis_title="Price (INR)",
            legend=dict(orientation="h", y=1.02, x=0),
        )
        st.plotly_chart(ma_fig, use_container_width=True)
        st.caption(
            "When the orange line is above the purple line, the rule holds "
            "the stock (green shaded). When orange dips below purple, the "
            "rule moves to cash (no shading)."
        )

        st.markdown("**How ₹100 would have grown** following the rule vs "
                    "just buying and holding:")

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

"""
analyst.py - the "analyst layer" used by the dashboard and the email alerts.
It adds the four pieces that turn this from a chart viewer into something
more like an analyst note: company health, news + events, sector/market
context, and a "play" with stop/target/size. Then it stitches everything
into one plain-English paragraph per stock.

All inputs are free (yfinance). No AI/LLM dependency at this stage.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Optional

import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Map each stock to its sector benchmark index
# ---------------------------------------------------------------------------
SECTOR_INDEX: dict[str, tuple[str, str]] = {
    "TCS": ("^CNXIT", "Nifty IT"),
    "Vedanta": ("^CNXMETAL", "Nifty Metal"),
    "Samvardhana Motherson": ("^CNXAUTO", "Nifty Auto"),
    "Sterlite Technologies": ("^CNXIT", "Nifty IT"),
    "NMDC": ("^CNXMETAL", "Nifty Metal"),
    "Power Finance Corp (PFC)": ("NIFTY_FIN_SERVICE.NS",
                                  "Nifty Financial Services"),
    "REC Ltd": ("NIFTY_FIN_SERVICE.NS", "Nifty Financial Services"),
}
BROAD_INDEX = ("^NSEI", "Nifty 50")

# ---------------------------------------------------------------------------
# Peer map per stock (for "cheap vs sector" and "growing fast vs peers")
# ---------------------------------------------------------------------------
PEERS: dict[str, list[str]] = {
    "TCS":                       ["INFY.NS", "WIPRO.NS", "HCLTECH.NS", "TECHM.NS"],
    "Vedanta":                   ["HINDZINC.NS", "NATIONALUM.NS", "JINDALSTEL.NS", "SAIL.NS"],
    "Samvardhana Motherson":     ["BOSCHLTD.NS", "BHARATFORG.NS", "EXIDEIND.NS"],
    "Sterlite Technologies":     ["HFCL.NS", "KEC.NS"],
    "NMDC":                      ["HINDZINC.NS", "NATIONALUM.NS", "SAIL.NS"],
    "Power Finance Corp (PFC)":  ["RECLTD.NS", "IRFC.NS", "LICHSGFIN.NS"],
    "REC Ltd":                   ["PFC.NS", "IRFC.NS", "LICHSGFIN.NS"],
}

# ---------------------------------------------------------------------------
# Macro layer - the things on every Indian analyst's screen
# ---------------------------------------------------------------------------
MACRO_SYMBOLS: dict[str, tuple[str, str]] = {
    "usd_inr":     ("INR=X",        "USD/INR"),
    "brent":       ("BZ=F",         "Brent crude (USD)"),
    "india_vix":   ("^INDIAVIX",    "India VIX (fear gauge)"),
    "us_10y":      ("^TNX",         "US 10-year yield"),
    "nifty50":     ("^NSEI",        "Nifty 50"),
}

# Simple sentiment word lists (English; small but practical for headlines)
NEG_WORDS = {
    "loss", "losses", "decline", "declines", "drop", "drops", "fall", "falls",
    "weak", "miss", "misses", "missed", "downgrade", "downgrades", "lawsuit",
    "fraud", "investigation", "probe", "sells off", "cut", "cuts", "fired",
    "layoff", "layoffs", "scandal", "plunge", "slump", "concern", "concerns",
    "worry", "worries", "risk", "risks", "warning", "warns", "delay",
    "default", "downturn", "slowdown", "shrink", "shrinks", "negative",
    "challenges", "headwind", "headwinds", "penalty", "fine", "sanction",
}
POS_WORDS = {
    "surge", "surges", "jump", "jumps", "rally", "rallies", "rise", "rises",
    "gain", "gains", "beat", "beats", "upgrade", "upgrades", "partnership",
    "acquisition", "acquires", "expansion", "expand", "expands", "growth",
    "grows", "profit", "profits", "dividend", "bonus", "win", "wins",
    "contract", "deal", "milestone", "record", "strong", "robust", "positive",
    "outperform", "outperforms", "tailwind", "tailwinds", "approval", "approved",
}


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------
def fetch_fundamentals(ticker: str) -> dict:
    """Pulls company-health numbers. Missing fields just come back as None."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}

    def g(key):
        v = info.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "trailing_pe": g("trailingPE"),
        "forward_pe": g("forwardPE"),
        "roe": g("returnOnEquity"),
        "debt_to_equity": g("debtToEquity"),
        "profit_margin": g("profitMargins"),
        "revenue_growth": g("revenueGrowth"),
        "earnings_growth": g("earningsGrowth"),
        "dividend_yield": g("dividendYield"),
        "market_cap": g("marketCap"),
        "fifty_two_high": g("fiftyTwoWeekHigh"),
        "fifty_two_low": g("fiftyTwoWeekLow"),
        "beta": g("beta"),
        "price_to_book": g("priceToBook"),
    }


def interpret_fundamentals(f: dict, current_price: float) -> dict:
    """Translate numbers into plain words."""
    parts = []

    # Valuation
    pe = f.get("trailing_pe")
    if pe is not None:
        if pe < 15:
            parts.append("looks cheap on earnings")
        elif pe > 40:
            parts.append("looks expensive on earnings")
        else:
            parts.append("fairly priced on earnings")

    # Profitability
    roe = f.get("roe")
    if roe is not None:
        roe_pct = roe * 100 if abs(roe) < 5 else roe  # yfinance sometimes pct
        if roe_pct > 18:
            parts.append("highly profitable")
        elif roe_pct < 5:
            parts.append("weak profits")

    # Debt
    de = f.get("debt_to_equity")
    if de is not None:
        if de < 50:
            parts.append("low debt")
        elif de > 150:
            parts.append("high debt - watch out")

    # Growth
    eg = f.get("earnings_growth")
    if eg is not None:
        if eg > 0.15:
            parts.append("earnings growing fast")
        elif eg < -0.05:
            parts.append("earnings are shrinking")

    # 52-week position
    hi = f.get("fifty_two_high")
    lo = f.get("fifty_two_low")
    pos_pct = None
    if hi and lo and hi > lo:
        pos_pct = (current_price - lo) / (hi - lo) * 100
        if pos_pct < 25:
            parts.append("near its 52-week low")
        elif pos_pct > 85:
            parts.append("near its 52-week high")

    return {
        "summary_phrases": parts,
        "pos_in_52w_pct": pos_pct,
    }


# ---------------------------------------------------------------------------
# News + simple keyword sentiment
# ---------------------------------------------------------------------------
def fetch_news(ticker: str, max_items: int = 5, days_back: int = 14) -> list[dict]:
    out: list[dict] = []
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return out

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)

    for raw in items:
        c = raw.get("content") or raw  # newer yfinance wraps under "content"
        title = c.get("title") or raw.get("title")
        if not title:
            continue

        date_str = c.get("pubDate") or c.get("providerPublishTime")
        date: Optional[dt.datetime] = None
        if isinstance(date_str, (int, float)):
            date = dt.datetime.fromtimestamp(date_str, tz=dt.timezone.utc)
        elif isinstance(date_str, str):
            try:
                date = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                date = None

        if date and date < cutoff:
            continue

        cu = c.get("canonicalUrl")
        url = cu.get("url") if isinstance(cu, dict) else (cu or c.get("link"))
        summary = c.get("summary") or ""

        sentiment, score = score_sentiment(title + " " + summary)
        out.append({
            "title": title,
            "date": date,
            "url": url,
            "publisher": (c.get("provider", {}) or {}).get("displayName")
                         if isinstance(c.get("provider"), dict)
                         else c.get("publisher"),
            "sentiment": sentiment,
            "score": score,
        })
        if len(out) >= max_items:
            break
    return out


def score_sentiment(text: str) -> tuple[str, int]:
    lower = text.lower()
    neg = sum(1 for w in NEG_WORDS if w in lower)
    pos = sum(1 for w in POS_WORDS if w in lower)
    score = pos - neg
    if score > 0:
        return "positive", score
    if score < 0:
        return "negative", score
    return "neutral", score


def news_summary(news: list[dict]) -> dict:
    if not news:
        return {"label": "no recent news", "neg": 0, "pos": 0, "total": 0}
    neg = sum(1 for n in news if n["sentiment"] == "negative")
    pos = sum(1 for n in news if n["sentiment"] == "positive")
    if neg > pos and neg >= 2:
        label = f"mostly negative headlines ({neg} of {len(news)})"
    elif pos > neg and pos >= 2:
        label = f"mostly positive headlines ({pos} of {len(news)})"
    else:
        label = "mixed / neutral headlines"
    return {"label": label, "neg": neg, "pos": pos, "total": len(news)}


# ---------------------------------------------------------------------------
# Earnings date
# ---------------------------------------------------------------------------
def fetch_earnings_date(ticker: str) -> Optional[dt.date]:
    try:
        cal = yf.Ticker(ticker).calendar or {}
    except Exception:
        return None
    e = cal.get("Earnings Date")
    if isinstance(e, list) and e:
        e = e[0]
    if isinstance(e, dt.datetime):
        return e.date()
    if isinstance(e, dt.date):
        return e
    return None


def days_to(target: Optional[dt.date]) -> Optional[int]:
    if not target:
        return None
    return (target - dt.date.today()).days


# ---------------------------------------------------------------------------
# Sector / broad market context
# ---------------------------------------------------------------------------
def fetch_index_change(symbol: str) -> Optional[dict]:
    try:
        d = yf.Ticker(symbol).history(period="1mo", interval="1d")
    except Exception:
        return None
    if d is None or d.empty or len(d) < 2:
        return None
    last = float(d["Close"].iloc[-1])
    prev = float(d["Close"].iloc[-2])
    month_ago = float(d["Close"].iloc[0])
    return {
        "last": last,
        "day_pct": (last - prev) / prev * 100 if prev else 0.0,
        "month_pct": (last - month_ago) / month_ago * 100 if month_ago else 0.0,
    }


def compute_peer_comparison(name: str, fund: dict) -> dict:
    """Compare this stock's key fundamentals against peer median."""
    peer_tickers = PEERS.get(name, [])
    if not peer_tickers:
        return {}
    peer_pes, peer_roes, peer_yields, peer_de = [], [], [], []
    for pt in peer_tickers:
        try:
            pi = yf.Ticker(pt).info or {}
        except Exception:
            continue
        for src, dst in [("trailingPE", peer_pes), ("returnOnEquity", peer_roes),
                         ("dividendYield", peer_yields), ("debtToEquity", peer_de)]:
            v = pi.get(src)
            try:
                if v is not None:
                    dst.append(float(v))
            except (TypeError, ValueError):
                pass

    def median(lst):
        if not lst:
            return None
        s = sorted(lst)
        n = len(s)
        return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)

    out: dict = {"peer_count": len(peer_tickers)}
    out["pe_median"] = median(peer_pes)
    out["roe_median"] = median(peer_roes)
    out["yield_median"] = median(peer_yields)
    out["de_median"] = median(peer_de)

    # Plain-English readings
    notes = []
    if fund.get("trailing_pe") is not None and out["pe_median"] is not None:
        if fund["trailing_pe"] < out["pe_median"] * 0.85:
            notes.append("cheaper than peers")
        elif fund["trailing_pe"] > out["pe_median"] * 1.15:
            notes.append("more expensive than peers")
    if fund.get("roe") is not None and out["roe_median"] is not None:
        f_roe = fund["roe"] * 100 if abs(fund["roe"]) < 5 else fund["roe"]
        p_roe = out["roe_median"] * 100 if abs(out["roe_median"]) < 5 else out["roe_median"]
        if f_roe > p_roe * 1.15:
            notes.append("more profitable than peers")
        elif f_roe < p_roe * 0.85:
            notes.append("less profitable than peers")
    if fund.get("dividend_yield") is not None and out["yield_median"] is not None:
        if fund["dividend_yield"] > out["yield_median"] * 1.2:
            notes.append("higher dividend yield than peers")
    out["notes"] = notes
    return out


def fetch_macro() -> dict:
    """One-shot fetch of the macro screen (5 symbols, ~5s on cold)."""
    out = {}
    for key, (sym, label) in MACRO_SYMBOLS.items():
        ctx = fetch_index_change(sym)
        if ctx:
            out[key] = {"label": label, **ctx}
    return out


def interpret_macro(macro: dict) -> list[str]:
    """Turn macro readings into plain-English flags."""
    flags = []
    if "india_vix" in macro:
        v = macro["india_vix"]["last"]
        if v > 18:
            flags.append(f"India VIX high ({v:.1f}) - jittery market")
        elif v < 12:
            flags.append(f"India VIX low ({v:.1f}) - complacent market")
    if "usd_inr" in macro:
        m = macro["usd_inr"]["month_pct"]
        if m > 1.0:
            flags.append(f"Rupee weakening ({m:+.1f}% / month) - helps IT, hurts oil importers")
        elif m < -1.0:
            flags.append(f"Rupee strengthening ({m:+.1f}% / month) - hurts IT, helps importers")
    if "brent" in macro:
        m = macro["brent"]["month_pct"]
        if m > 5:
            flags.append(f"Crude up {m:+.1f}% / month - bad for oil importers & paints")
        elif m < -5:
            flags.append(f"Crude down {m:+.1f}% / month - tailwind for refiners & users")
    if "nifty50" in macro:
        d = macro["nifty50"]["day_pct"]
        m = macro["nifty50"]["month_pct"]
        flags.append(
            f"Nifty 50 {d:+.2f}% today, {m:+.1f}% this month"
        )
    return flags


def relative_strength(stock_month_pct: float, idx_month_pct: float) -> str:
    diff = stock_month_pct - idx_month_pct
    if diff > 3:
        return f"outperforming its sector by {diff:+.1f}% over the past month"
    if diff < -3:
        return f"lagging its sector by {diff:+.1f}% over the past month"
    return f"moving roughly in line with its sector ({diff:+.1f}% vs sector)"


# ---------------------------------------------------------------------------
# The Play - stop / target / position size
# ---------------------------------------------------------------------------
def compute_play(
    df: pd.DataFrame, capital: float, risk_pct: float = 0.02
) -> Optional[dict]:
    if len(df) < 20:
        return None
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    if not (atr and atr > 0):
        return None
    close = float(df["Close"].iloc[-1])
    stop = max(close - 2 * atr, 0.0)
    target = close + 4 * atr  # 2:1 reward:risk
    risk_per_share = close - stop
    if risk_per_share <= 0:
        return None
    risk_rupees = capital * risk_pct
    shares = int(math.floor(risk_rupees / risk_per_share))
    trade_value = shares * close
    return {
        "entry": round(close, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_per_share": round(risk_per_share, 2),
        "shares": shares,
        "trade_value": round(trade_value, 2),
        "risk_rupees": round(risk_rupees, 2),
        "reward_rupees": round((target - close) * shares, 2),
        "atr": round(float(atr), 2),
    }


# ---------------------------------------------------------------------------
# The analyst brief - one well-written paragraph that combines everything
# ---------------------------------------------------------------------------
def assemble_brief(
    name: str,
    last_close: float,
    sig: dict,
    fund_interp: dict,
    news_sum: dict,
    sector_ctx: Optional[dict],
    broad_ctx: Optional[dict],
    earnings_in_days: Optional[int],
    play: Optional[dict],
    peer_cmp: Optional[dict] = None,
    macro_flags: Optional[list] = None,
) -> str:
    parts: list[str] = []

    # 1. Technical state
    parts.append(sig["sentence"])

    # 2. Fundamental view - merge own-view with peer comparison
    own = fund_interp.get("summary_phrases", []) or []
    peer_notes = (peer_cmp or {}).get("notes", []) or []
    company_bits = own + peer_notes
    if company_bits:
        parts.append("On the company: " + ", ".join(company_bits) + ".")

    # 3. Sector / market context
    if sector_ctx:
        sector_line = (
            f"Its sector is "
            + ("up" if sector_ctx["month_pct"] >= 0 else "down")
            + f" {abs(sector_ctx['month_pct']):.1f}% this month"
        )
        if broad_ctx:
            sector_line += (
                "; the broader Nifty 50 is "
                + ("up" if broad_ctx["month_pct"] >= 0 else "down")
                + f" {abs(broad_ctx['month_pct']):.1f}%."
            )
        else:
            sector_line += "."
        parts.append(sector_line)

    # 4. News + earnings flags
    flag_bits = []
    if news_sum and news_sum["total"]:
        flag_bits.append(news_sum["label"])
    if earnings_in_days is not None:
        if 0 <= earnings_in_days <= 10:
            flag_bits.append(f"earnings in {earnings_in_days} day(s) - be cautious")
        elif 0 <= earnings_in_days <= 30:
            flag_bits.append(f"earnings in about {earnings_in_days} days")
    if flag_bits:
        parts.append("News and events: " + "; ".join(flag_bits) + ".")

    # 5. Macro flag if relevant
    if macro_flags:
        # Only include macro flags that are specifically risky for this stock's sector
        # For now: include India VIX high, and the always-on Nifty line is omitted.
        risky = [f for f in macro_flags
                 if "VIX high" in f or "Rupee" in f or "Crude" in f]
        if risky:
            parts.append("Macro: " + "; ".join(risky[:2]) + ".")

    # 6. The play
    if play and sig["tone"] in ("good", "warn"):
        parts.append(
            f"If considering entry near ₹{play['entry']:.2f}: stop at "
            f"₹{play['stop']:.2f}, first target ₹{play['target']:.2f}, "
            f"about {play['shares']} share(s) at the chosen account risk."
        )

    return " ".join(parts)

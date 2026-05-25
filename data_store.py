"""
data_store.py - disk persistence of daily snapshots.

How this works:
  - The GitHub Actions cron runs every weekday after Indian market close.
  - It fetches fresh prices / fundamentals / news for each stock and saves them
    into the `state/` folder, then commits them back to the repo.
  - The Streamlit dashboard reads from these files FIRST and only falls back
    to live Yahoo if a file is missing.

Result: the dashboard loads instantly and is immune to Yahoo's rate-limiting
on shared cloud IPs. Even if Yahoo is completely down, yesterday's snapshot
still shows up (with a "data is N hours old" note).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path("state")
PRICES_DIR = ROOT / "prices"
FUND_DIR = ROOT / "fundamentals"
NEWS_DIR = ROOT / "news"
INDEX_DIR = ROOT / "indices"
META_PATH = ROOT / "data_meta.json"


def _safe(name: str) -> str:
    return (name.replace("/", "_").replace("\\", "_")
                .replace("^", "").replace("=", "_"))


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def save_price_history(ticker: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    if "Date" in out.columns:
        out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
    out.to_csv(PRICES_DIR / f"{_safe(ticker)}.csv", index=False)


def load_price_history(ticker: str) -> pd.DataFrame:
    p = PRICES_DIR / f"{_safe(ticker)}.csv"
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(p)
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])
        return df
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------
def save_fundamentals(ticker: str, fund: dict) -> None:
    if not fund:
        return
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    (FUND_DIR / f"{_safe(ticker)}.json").write_text(
        json.dumps(fund, default=str, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_fundamentals(ticker: str) -> dict:
    p = FUND_DIR / f"{_safe(ticker)}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
def save_news(ticker: str, news: list) -> None:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    # Convert datetime -> isoformat for JSON
    out = []
    for n in (news or []):
        n2 = dict(n)
        d = n2.get("date")
        if isinstance(d, (dt.datetime, dt.date)):
            n2["date"] = d.isoformat()
        out.append(n2)
    (NEWS_DIR / f"{_safe(ticker)}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_news(ticker: str) -> list:
    p = NEWS_DIR / f"{_safe(ticker)}.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for n in data:
            d = n.get("date")
            if isinstance(d, str):
                try:
                    n["date"] = dt.datetime.fromisoformat(d)
                except ValueError:
                    n["date"] = None
        return data
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Indices (macro + sector)
# ---------------------------------------------------------------------------
def save_index_change(symbol: str, ctx: Optional[dict]) -> None:
    if not ctx:
        return
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (INDEX_DIR / f"{_safe(symbol)}.json").write_text(
        json.dumps(ctx, default=str, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_index_change(symbol: str) -> Optional[dict]:
    p = INDEX_DIR / f"{_safe(symbol)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Earnings date
# ---------------------------------------------------------------------------
def save_earnings_date(ticker: str, date: Optional[dt.date]) -> None:
    if not date:
        return
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    (FUND_DIR / f"{_safe(ticker)}_earnings.json").write_text(
        json.dumps({"date": date.isoformat()}), encoding="utf-8"
    )


def load_earnings_date(ticker: str) -> Optional[dt.date]:
    p = FUND_DIR / f"{_safe(ticker)}_earnings.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8")).get("date")
        return dt.date.fromisoformat(d) if d else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Meta (when was the snapshot last refreshed?)
# ---------------------------------------------------------------------------
def save_meta(meta: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, default=str, indent=2), encoding="utf-8")


def load_meta() -> dict:
    if not META_PATH.exists():
        return {}
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def data_age_hours() -> Optional[float]:
    """How old is the snapshot, in hours? None if no snapshot ever."""
    meta = load_meta()
    ts = meta.get("updated_at")
    if not ts:
        return None
    try:
        when = dt.datetime.fromisoformat(ts)
        return (dt.datetime.now() - when).total_seconds() / 3600
    except Exception:
        return None


def update_meta_now(ok_count: int, fail: list[str]) -> None:
    save_meta({
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stocks_ok": ok_count,
        "stocks_failed": fail,
    })

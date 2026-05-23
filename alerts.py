"""
alerts.py - runs once a day on GitHub Actions after Indian market close,
checks every stock's signal, compares against yesterday's saved signal, and
EMAILS the recipients if any stock has flipped (e.g. red -> green).

Reads three things from environment variables (set as GitHub Secrets):
  GMAIL_USER          - the sending Gmail address (e.g. you@gmail.com)
  GMAIL_APP_PASSWORD  - a 16-char Google "App Password" (NOT your normal password)
  ALERT_RECIPIENTS    - comma-separated list of emails to notify (dad, you, ...)

Saves the current state to state/last_signals.json which the GitHub Action
commits back to the repo. That is how it "remembers" yesterday.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from core import STOCKS, add_indicators, fetch_history, read_signals
from analyst import (
    SECTOR_INDEX, BROAD_INDEX,
    fetch_fundamentals, interpret_fundamentals,
    fetch_news, news_summary,
    fetch_earnings_date, days_to,
    fetch_index_change, compute_play, assemble_brief,
)

ALERT_CAPITAL = float(os.environ.get("ALERT_CAPITAL", "100000"))
ALERT_RISK_PCT = float(os.environ.get("ALERT_RISK_PCT", "2.0")) / 100.0

STATE_PATH = Path("state/last_signals.json")
TONE_EMOJI = {"good": "\U0001F7E2", "warn": "\U0001F7E0",
              "bad": "\U0001F534", "neutral": "⚪"}
TONE_WORDS = {"good": "Looks good (possible entry)",
              "warn": "Be careful (watch / possible exit)",
              "bad": "Stay out for now",
              "neutral": "Mixed / neutral"}
DASHBOARD_URL = "https://stocks-dashboard-wasu.streamlit.app"


def load_previous_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_current_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def compute_today() -> dict:
    """For each stock, fetch + compute current signal + full analyst view.
    Skip on fetch failure (won't email about it, won't update state)."""
    today = {}
    # Broad market index fetched once
    broad_ctx = fetch_index_change(BROAD_INDEX[0])

    for name, ticker in STOCKS.items():
        df = fetch_history(ticker, period="2y")  # enough for SMA50 + RSI + ATR
        if df.empty:
            print(f"[skip] {name}: fetch failed", file=sys.stderr)
            continue
        df = add_indicators(df)
        sig = read_signals(df)
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2]) if len(df) > 1 else last_close
        change_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0

        # Analyst layer
        fund = fetch_fundamentals(ticker)
        interp = interpret_fundamentals(fund, last_close)
        news = fetch_news(ticker, max_items=5)
        nsum = news_summary(news)
        edate = fetch_earnings_date(ticker)
        edays = days_to(edate)
        sec_sym, sec_name = SECTOR_INDEX.get(name, (None, None))
        sec_ctx = fetch_index_change(sec_sym) if sec_sym else None
        play = compute_play(df, capital=ALERT_CAPITAL, risk_pct=ALERT_RISK_PCT)
        brief = assemble_brief(
            name, last_close, sig, interp, nsum, sec_ctx, broad_ctx, edays, play
        )

        today[name] = {
            "tone": sig["tone"],
            "sentence": sig["sentence"],
            "brief": brief,
            "rsi": round(sig["rsi"], 1),
            "trend_up": sig["trend_up"],
            "last_close": round(last_close, 2),
            "change_pct": round(change_pct, 2),
            "earnings_in_days": edays,
            "play": play,
            "sector_name": sec_name,
        }
    return today


def find_flips(prev: dict, today: dict) -> list:
    flips = []
    for name, cur in today.items():
        old_tone = prev.get(name, {}).get("tone")
        if old_tone is None:
            continue  # first-time entry, not a flip
        if old_tone != cur["tone"]:
            flips.append({
                "stock": name,
                "from_tone": old_tone,
                "to_tone": cur["tone"],
                "brief": cur.get("brief", cur["sentence"]),
                "sentence": cur["sentence"],
                "last_close": cur["last_close"],
                "change_pct": cur["change_pct"],
                "play": cur.get("play"),
                "earnings_in_days": cur.get("earnings_in_days"),
            })
    return flips


def _flip_card_html(f: dict) -> str:
    play_html = ""
    if f.get("play"):
        p = f["play"]
        play_html = (
            f"<table cellpadding='4' cellspacing='0' style='margin-top:6px;"
            f"font-size:13px;border-collapse:collapse;'>"
            f"<tr><td>Entry near:</td><td><b>&#8377;{p['entry']:.2f}</b></td>"
            f"<td style='padding-left:18px;'>Stop:</td>"
            f"<td><b>&#8377;{p['stop']:.2f}</b></td></tr>"
            f"<tr><td>Target:</td><td><b>&#8377;{p['target']:.2f}</b></td>"
            f"<td style='padding-left:18px;'>Suggested size:</td>"
            f"<td><b>{p['shares']} shares</b> "
            f"(&#8377;{p['trade_value']:,.0f})</td></tr></table>"
        )
    return (
        f"<div style='border:1px solid #ddd;border-radius:6px;padding:12px;"
        f"margin-bottom:14px;'>"
        f"<div style='font-size:15px;margin-bottom:4px;'>"
        f"<b>{f['stock']}</b>: "
        f"{TONE_EMOJI[f['from_tone']]} {TONE_WORDS[f['from_tone']]} "
        f"&rarr; <b>{TONE_EMOJI[f['to_tone']]} {TONE_WORDS[f['to_tone']]}</b>"
        f" &nbsp;&middot;&nbsp; &#8377;{f['last_close']:,.2f} "
        f"({f['change_pct']:+.2f}%)</div>"
        f"<div style='color:#333;'>{f['brief']}</div>"
        f"{play_html}"
        f"</div>"
    )


def render_email_html(flips: list, today: dict, first_run: bool) -> tuple[str, str]:
    today_str = dt.datetime.now().strftime("%A, %d %B %Y")

    if first_run:
        subject = f"Stock Dashboard alerts are LIVE - today's read ({today_str})"
        intro = (
            "Alerts are now switched on. From tomorrow you will only get an "
            "email when a stock's signal CHANGES. Here is today's read on "
            "every stock as a starting point:"
        )
    elif flips:
        subject = (
            f"Stock Dashboard: {len(flips)} signal change"
            + ("s" if len(flips) != 1 else "")
            + f" - {today_str}"
        )
        intro = "One or more of your stocks changed signal since yesterday:"
    else:
        return ("", "")

    flips_html = "".join(_flip_card_html(f) for f in flips) if flips else ""

    # All-stocks card grid for context (briefs visible too)
    today_html = ""
    for name, cur in today.items():
        today_html += (
            f"<div style='border-left:4px solid #ddd;padding:8px 12px;"
            f"margin-bottom:8px;'>"
            f"<div><b>{TONE_EMOJI[cur['tone']]} {name}</b> "
            f"&nbsp;&middot;&nbsp; &#8377;{cur['last_close']:,.2f} "
            f"({cur['change_pct']:+.2f}%)</div>"
            f"<div style='color:#444;font-size:13px;margin-top:3px;'>"
            f"{cur.get('brief', cur['sentence'])}</div>"
            f"</div>"
        )

    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#111;
      max-width:760px;">
      <h2 style="margin-bottom:4px;">Stock Dashboard &mdash; {today_str}</h2>
      <p style="margin-top:4px;color:#333;">{intro}</p>
      {flips_html}
      <h3 style="margin-top:24px;">Today's read on all stocks</h3>
      {today_html}
      <p style="margin-top:20px;">
        Open the full dashboard: <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a>
      </p>
      <p style="color:#666;font-size:12px;margin-top:30px;">
        Rule-based analyst-style notes. A helper, not a fortune teller.
        The final decision is always yours.
      </p>
    </body></html>
    """

    # Plain text fallback
    text = f"Stock Dashboard - {today_str}\n\n{intro}\n\n"
    for f in flips:
        text += (
            f"{f['stock']}: {TONE_WORDS[f['from_tone']]} -> "
            f"{TONE_WORDS[f['to_tone']]}  (Rs {f['last_close']:,.2f}, "
            f"{f['change_pct']:+.2f}%)\n   {f['brief']}\n"
        )
        if f.get("play"):
            p = f["play"]
            text += (
                f"   Play: entry ~Rs {p['entry']:.2f}, stop "
                f"Rs {p['stop']:.2f}, target Rs {p['target']:.2f}, "
                f"{p['shares']} shares (Rs {p['trade_value']:,.0f}).\n"
            )
        text += "\n"
    text += "\nToday's read on all stocks:\n"
    for name, cur in today.items():
        text += (
            f"  {name}: Rs {cur['last_close']:,.2f} "
            f"({cur['change_pct']:+.2f}%)\n     "
            f"{cur.get('brief', cur['sentence'])}\n"
        )
    text += f"\nDashboard: {DASHBOARD_URL}\n"
    return subject, text + "\n---\n(HTML version below)\n" + html


def send_email(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    raw_recipients = os.environ.get("ALERT_RECIPIENTS", "")
    recipients = [e.strip() for e in raw_recipients.split(",") if e.strip()]

    if not (user and pwd and recipients):
        print("[error] missing GMAIL_USER / GMAIL_APP_PASSWORD / ALERT_RECIPIENTS",
              file=sys.stderr)
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Stock Dashboard <{user}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    # Split text/html if present
    if "---\n(HTML version below)\n" in body:
        text_part, html_part = body.split("---\n(HTML version below)\n", 1)
    else:
        text_part, html_part = body, body
    msg.attach(MIMEText(text_part, "plain", "utf-8"))
    msg.attach(MIMEText(html_part, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, pwd)
        server.sendmail(user, recipients, msg.as_string())
    print(f"[ok] email sent to {len(recipients)} recipient(s)")


def main() -> None:
    prev = load_previous_state()
    today = compute_today()

    if not today:
        print("[warn] no stocks loaded today; not updating state or sending email")
        return

    first_run = len(prev) == 0
    flips = find_flips(prev, today)

    if first_run:
        subject, body = render_email_html(flips=[], today=today, first_run=True)
        send_email(subject, body)
    elif flips:
        subject, body = render_email_html(flips=flips, today=today, first_run=False)
        send_email(subject, body)
    else:
        print("[ok] no signal changes today; no email sent")

    save_current_state(today)


if __name__ == "__main__":
    main()

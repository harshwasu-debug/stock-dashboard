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
    """For each stock, fetch + compute current signal. Skip on fetch failure."""
    today = {}
    for name, ticker in STOCKS.items():
        df = fetch_history(ticker, period="2y")  # enough for SMA50 + RSI
        if df.empty:
            print(f"[skip] {name}: fetch failed", file=sys.stderr)
            continue
        df = add_indicators(df)
        sig = read_signals(df)
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2]) if len(df) > 1 else last_close
        change_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0
        today[name] = {
            "tone": sig["tone"],
            "sentence": sig["sentence"],
            "rsi": round(sig["rsi"], 1),
            "trend_up": sig["trend_up"],
            "last_close": round(last_close, 2),
            "change_pct": round(change_pct, 2),
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
                "sentence": cur["sentence"],
                "last_close": cur["last_close"],
                "change_pct": cur["change_pct"],
            })
    return flips


def render_email_html(flips: list, today: dict, first_run: bool) -> tuple[str, str]:
    today_str = dt.datetime.now().strftime("%A, %d %B %Y")

    if first_run:
        subject = f"Stock Dashboard alerts are LIVE - today's read ({today_str})"
        intro = (
            "Alerts are now switched on. From tomorrow you will only get an "
            "email when a stock's signal CHANGES. Here is the read for today "
            "as a starting point:"
        )
    elif flips:
        subject = (
            f"Stock Dashboard: {len(flips)} signal change"
            + ("s" if len(flips) != 1 else "")
            + f" - {today_str}"
        )
        intro = "One or more of your stocks changed signal since yesterday:"
    else:
        return ("", "")  # caller will skip sending

    flip_rows = ""
    for f in flips:
        flip_rows += (
            f"<tr>"
            f"<td><b>{f['stock']}</b></td>"
            f"<td>{TONE_EMOJI[f['from_tone']]} {TONE_WORDS[f['from_tone']]}</td>"
            f"<td>&rarr;</td>"
            f"<td>{TONE_EMOJI[f['to_tone']]} <b>{TONE_WORDS[f['to_tone']]}</b></td>"
            f"<td>&#8377;{f['last_close']:,.2f} ({f['change_pct']:+.2f}%)</td>"
            f"</tr>"
            f"<tr><td colspan='5' style='color:#555;font-style:italic;padding-bottom:10px;'>{f['sentence']}</td></tr>"
        )

    today_rows = ""
    for name, cur in today.items():
        today_rows += (
            f"<tr>"
            f"<td>{TONE_EMOJI[cur['tone']]}</td>"
            f"<td><b>{name}</b></td>"
            f"<td>&#8377;{cur['last_close']:,.2f} ({cur['change_pct']:+.2f}%)</td>"
            f"<td>{cur['sentence']}</td>"
            f"</tr>"
        )

    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#111;">
      <h2 style="margin-bottom:4px;">Stock Dashboard - {today_str}</h2>
      <p>{intro}</p>
      {"<table cellpadding='6' cellspacing='0' border='0' style='border-collapse:collapse;margin-bottom:18px;'>" + flip_rows + "</table>" if flips else ""}
      <h3 style="margin-top:24px;">All stocks today</h3>
      <table cellpadding='6' cellspacing='0' border='0' style='border-collapse:collapse;'>
        {today_rows}
      </table>
      <p style="margin-top:20px;">
        Open the dashboard: <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a>
      </p>
      <p style="color:#666;font-size:12px;margin-top:30px;">
        This is a helper, not a fortune teller. It does not know news, results,
        or the future. The final decision is always yours.
      </p>
    </body></html>
    """
    text = f"Stock Dashboard - {today_str}\n\n{intro}\n\n"
    for f in flips:
        text += (
            f"{f['stock']}: {TONE_WORDS[f['from_tone']]} -> "
            f"{TONE_WORDS[f['to_tone']]}  (Rs {f['last_close']:,.2f}, "
            f"{f['change_pct']:+.2f}%)\n   {f['sentence']}\n\n"
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

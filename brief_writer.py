"""
brief_writer.py - writes the per-stock analyst paragraph.

Tries (in order):
  1. Google Gemini (free, via GEMINI_API_KEY env var)
  2. Anthropic Claude (paid, via ANTHROPIC_API_KEY env var)
  3. Rule-based fallback (no AI, always works)

This way the dashboard ALWAYS produces a brief - if an AI key is set, the
briefs are richer / more nuanced; if not, we still ship the rule-based one.
"""

from __future__ import annotations

import os
from typing import Optional

from analyst import assemble_brief

# Lazy-imported below so missing packages don't break the app
_genai_module = None
_anthropic_module = None


def _try_import_gemini():
    global _genai_module
    if _genai_module is not None:
        return _genai_module
    try:
        import google.generativeai as genai  # type: ignore
        _genai_module = genai
        return genai
    except Exception:
        _genai_module = False
        return None


def _try_import_anthropic():
    global _anthropic_module
    if _anthropic_module is not None:
        return _anthropic_module
    try:
        import anthropic  # type: ignore
        _anthropic_module = anthropic
        return anthropic
    except Exception:
        _anthropic_module = False
        return None


SYSTEM_PROMPT = (
    "You are a sober Indian-markets analyst writing a SHORT (3-5 sentence) "
    "daily note on one NSE stock for a non-technical reader (the user's father, "
    "who does swing and long-term trading). Plain English, no jargon. If a "
    "technical term appears, explain it inline in 3 words. State only what is "
    "supported by the numbers given - do NOT speculate about prices going up "
    "or down, and do NOT fabricate news. Be honest about uncertainty. End with "
    "the play numbers if provided (entry, stop, target, shares). Always close "
    "with: 'The decision is yours.'"
)


def _build_user_prompt(ctx: dict) -> str:
    """Format the per-stock context into a clean text block for the LLM."""
    lines = [f"STOCK: {ctx['name']}", f"Last close: Rs {ctx['last_close']:.2f}",
             f"Today's change: {ctx['change_pct']:+.2f}%"]

    sig = ctx["sig"]
    lines.append(
        f"Technicals: trend {'UP' if sig['trend_up'] else 'DOWN'}, "
        f"RSI {sig['rsi']:.0f}, momentum {sig['momentum_word']}. "
        f"Rule-based read: \"{sig['sentence']}\""
    )

    f = ctx["fund"]
    fund_bits = []
    if f.get("trailing_pe") is not None:
        fund_bits.append(f"P/E {f['trailing_pe']:.1f}")
    if f.get("roe") is not None:
        r = f["roe"] * 100 if abs(f["roe"]) < 5 else f["roe"]
        fund_bits.append(f"ROE {r:.0f}%")
    if f.get("debt_to_equity") is not None:
        fund_bits.append(f"D/E {f['debt_to_equity']:.0f}")
    if f.get("earnings_growth") is not None:
        fund_bits.append(f"Earnings growth {f['earnings_growth']*100:+.0f}%")
    if f.get("dividend_yield") is not None:
        fund_bits.append(f"Div yield {f['dividend_yield']:.2f}%")
    if ctx["interp"].get("pos_in_52w_pct") is not None:
        fund_bits.append(f"At {ctx['interp']['pos_in_52w_pct']:.0f}% of 52w range")
    if fund_bits:
        lines.append("Company: " + ", ".join(fund_bits))

    if ctx.get("peer_cmp"):
        pc = ctx["peer_cmp"]
        peer_bits = []
        if pc.get("pe_median") is not None:
            peer_bits.append(f"peer median P/E {pc['pe_median']:.1f}")
        if pc.get("roe_median") is not None:
            r = pc["roe_median"] * 100 if abs(pc["roe_median"]) < 5 else pc["roe_median"]
            peer_bits.append(f"peer median ROE {r:.0f}%")
        if pc.get("notes"):
            peer_bits.append("notes: " + ", ".join(pc["notes"]))
        if peer_bits:
            lines.append("Peers: " + "; ".join(peer_bits))

    if ctx.get("sector_ctx"):
        s = ctx["sector_ctx"]
        lines.append(
            f"Sector ({ctx.get('sector_name','sector')}): "
            f"today {s['day_pct']:+.2f}%, 1-month {s['month_pct']:+.1f}%"
        )
    if ctx.get("broad_ctx"):
        b = ctx["broad_ctx"]
        lines.append(f"Nifty 50: today {b['day_pct']:+.2f}%, 1-month {b['month_pct']:+.1f}%")

    if ctx.get("macro_flags"):
        lines.append("Macro context: " + " | ".join(ctx["macro_flags"]))

    nsum = ctx.get("news_sum", {})
    if nsum.get("total"):
        lines.append(f"Headlines (last 14 days): {nsum['label']}")
        # Include top 3 headline titles for context
        titles = [n["title"] for n in (ctx.get("news") or [])[:3]]
        if titles:
            lines.append("Top headlines:\n- " + "\n- ".join(titles))

    if ctx.get("earnings_in_days") is not None and 0 <= ctx["earnings_in_days"] <= 30:
        lines.append(f"Next earnings: in {ctx['earnings_in_days']} days")

    if ctx.get("play"):
        p = ctx["play"]
        lines.append(
            f"Play: entry ~Rs {p['entry']:.2f}, stop Rs {p['stop']:.2f}, "
            f"target Rs {p['target']:.2f}, {p['shares']} shares "
            f"(Rs {p['trade_value']:,.0f})"
        )

    lines.append("\nWrite the note now (3-5 sentences, plain English).")
    return "\n".join(lines)


def _call_gemini(prompt: str, api_key: str) -> Optional[str]:
    genai = _try_import_gemini()
    if not genai:
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash-latest",
            system_instruction=SYSTEM_PROMPT,
        )
        resp = model.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 350},
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception as e:
        print(f"[brief_writer] Gemini call failed: {e}")
        return None


def _call_anthropic(prompt: str, api_key: str) -> Optional[str]:
    anthropic = _try_import_anthropic()
    if not anthropic:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=350,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return (msg.content[0].text or "").strip()
    except Exception as e:
        print(f"[brief_writer] Anthropic call failed: {e}")
        return None


def write_brief(ctx: dict) -> tuple[str, str]:
    """
    ctx is the full per-stock dict built by build_view() in app.py (or by
    alerts.compute_today()). Returns (brief_text, source) where source is
    'gemini' | 'claude' | 'rule'.
    """
    gem_key = os.environ.get("GEMINI_API_KEY")
    ant_key = os.environ.get("ANTHROPIC_API_KEY")

    if gem_key:
        text = _call_gemini(_build_user_prompt(ctx), gem_key)
        if text:
            return text, "gemini"

    if ant_key:
        text = _call_anthropic(_build_user_prompt(ctx), ant_key)
        if text:
            return text, "claude"

    # Rule-based fallback
    text = assemble_brief(
        name=ctx["name"],
        last_close=ctx["last_close"],
        sig=ctx["sig"],
        fund_interp=ctx["interp"],
        news_sum=ctx.get("news_sum", {}),
        sector_ctx=ctx.get("sector_ctx"),
        broad_ctx=ctx.get("broad_ctx"),
        earnings_in_days=ctx.get("earnings_in_days"),
        play=ctx.get("play"),
        peer_cmp=ctx.get("peer_cmp"),
        macro_flags=ctx.get("macro_flags"),
    )
    return text, "rule"

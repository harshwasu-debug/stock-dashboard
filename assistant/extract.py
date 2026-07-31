"""
extract.py - turning a note into structured tasks.

Two paths:

  extract_with_gemini()  the real one. Gemini reads audio natively, so a voice
                         note goes straight in as bytes - there is no separate
                         speech-to-text step to wire up or pay for.
  extract_with_rules()   a keyword/regex fallback used when no API key is set,
                         so the app is never dead in the water. It is dumber,
                         and it says so in the UI.

Two rules matter more than the prompt wording:

  1. The model returns *fields*, never a rewritten file. Nothing it produces is
     written anywhere until you have seen it on the review screen and pressed
     Save.
  2. It must not invent deadlines. A task with no stated deadline comes back
     with due=null. A to-do list that quietly assigns dates you never said is
     worse than no to-do list.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import IST, date_to_str, parse_date, parse_time, today_ist

# Tried in order; the first one the SDK accepts wins. Keeps the app alive
# across Google's model retirements without an edit.
DEFAULT_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash-latest")

MAX_TASKS = 25

SYSTEM_PROMPT = """\
You turn a person's rough note into a clean to-do list.

The person is speaking or typing quickly, to themselves, on a phone. Notes are
messy: half-sentences, asides, thinking out loud. Your job is to pull out the
things they actually intend to DO, and leave the rest as the note body.

Rules:
- Only create a task for a real, actionable commitment. Observations, opinions
  and background context are NOT tasks.
- NEVER invent a deadline. If the person did not state or clearly imply when
  something is due, return null for "due". This matters more than being
  helpful - a made-up date is a bug.
- Resolve relative dates ("tomorrow", "next Friday", "end of the month",
  "in 3 days") against the current date you are given, and return an ISO
  YYYY-MM-DD date.
- Only set "due_time" if a clock time was actually mentioned. Use 24h HH:MM.
- "priority" is "high" only if urgency is explicit (urgent, ASAP, critical,
  "before anything else"). Otherwise "normal", or "low" if they say it can
  wait.
- Task titles are short and start with a verb ("Call the packaging vendor",
  not "packaging vendor call maybe?").
- "tags" are 0-3 short lowercase topic labels, no # prefix.
- If it is a voice note, transcribe it faithfully into "transcript", keeping
  the person's own words. For typed notes, leave "transcript" empty.

Return ONLY a JSON object of this exact shape:

{
  "title": "short title for the note itself",
  "summary": "one sentence on what this note is about",
  "transcript": "verbatim transcript, or empty string for typed notes",
  "tasks": [
    {
      "title": "Call the packaging vendor",
      "details": "any extra context from the note, or empty",
      "due": "2026-08-04" or null,
      "due_time": "16:30" or null,
      "priority": "low" | "normal" | "high",
      "tags": ["packaging"]
    }
  ]
}

If there is nothing actionable, return an empty "tasks" list. Do not pad it.
"""


@dataclass
class Extraction:
    """What came back, already cleaned up and safe to show."""

    title: str = ""
    summary: str = ""
    transcript: str = ""
    tasks: list[dict] = field(default_factory=list)
    source: str = "rules"          # 'gemini' or 'rules'
    warning: str = ""              # shown to the user when we fell back

    @property
    def is_empty(self) -> bool:
        return not self.tasks


class ExtractionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Cleaning whatever the model returned
# ---------------------------------------------------------------------------
def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n", "", stripped)
        stripped = re.sub(r"\n```$", "", stripped.rstrip())
    return stripped.strip()


def _clean_task(raw: Any, today: dt.date) -> Optional[dict]:
    """Coerce one model-produced task into the fields our Task model expects."""
    if not isinstance(raw, dict):
        return None

    title = str(raw.get("title") or "").strip()
    if not title:
        return None

    due = parse_date(raw.get("due"))
    # A model that hallucinates a date in the distant past is more likely to be
    # confused than right; keep it, but never silently. Dates far in the past
    # are almost always a parsing slip, so we drop them.
    if due and due < today - dt.timedelta(days=365):
        due = None

    priority = str(raw.get("priority") or "normal").strip().lower()
    if priority not in ("low", "normal", "high"):
        priority = "normal"

    tags_raw = raw.get("tags") or []
    if isinstance(tags_raw, str):
        tags_raw = tags_raw.split(",")
    tags = []
    for tag in tags_raw[:3]:
        clean = str(tag).strip().lstrip("#").lower()
        if clean:
            tags.append(clean)

    return {
        "title": title[:200],
        "details": str(raw.get("details") or "").strip(),
        "due": date_to_str(due),
        "due_time": parse_time(raw.get("due_time")) if due else None,
        "priority": priority,
        "tags": tags,
    }


def parse_model_json(text: str, today: dt.date) -> Extraction:
    """Parse and sanitise the model's reply. Raises if it isn't usable JSON."""
    cleaned = _strip_code_fence(text or "")
    if not cleaned:
        raise ExtractionError("The model returned nothing.")

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: pull the outermost {...} out of a chatty reply.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ExtractionError("The model did not return JSON.")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"The model's JSON was malformed: {exc}") from exc

    if not isinstance(payload, dict):
        raise ExtractionError("The model returned JSON, but not an object.")

    tasks = []
    for row in (payload.get("tasks") or [])[:MAX_TASKS]:
        cleaned_task = _clean_task(row, today)
        if cleaned_task:
            tasks.append(cleaned_task)

    return Extraction(
        title=str(payload.get("title") or "").strip()[:120],
        summary=str(payload.get("summary") or "").strip(),
        transcript=str(payload.get("transcript") or "").strip(),
        tasks=tasks,
        source="gemini",
    )


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def _context_prompt(today: dt.date, extra: str = "") -> str:
    lines = [
        f"Current date in India (IST): {today.isoformat()} ({today.strftime('%A')}).",
        "Resolve every relative date against that.",
    ]
    if extra:
        lines.append("")
        lines.append("The note follows:")
        lines.append(extra)
    return "\n".join(lines)


def extract_with_gemini(
    api_key: str,
    *,
    text: str = "",
    audio: Optional[tuple[bytes, str]] = None,
    today: Optional[dt.date] = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
) -> Extraction:
    """
    Send a typed note, a voice note, or both to Gemini and get structured
    tasks back. `audio` is (bytes, mime_type) straight off st.audio_input.
    """
    if not api_key:
        raise ExtractionError("No Gemini API key configured.")
    if not text.strip() and not audio:
        raise ExtractionError("Nothing to extract - the note is empty.")

    try:
        import google.generativeai as genai
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise ExtractionError(
            "google-generativeai is not installed. Run: pip install -r requirements.txt"
        ) from exc

    today = today or today_ist()
    genai.configure(api_key=api_key)

    parts: list[Any] = [_context_prompt(today, text.strip())]
    if audio:
        audio_bytes, mime_type = audio
        parts.append("This is a voice note. Transcribe it, then extract tasks.")
        parts.append({"mime_type": mime_type or "audio/webm", "data": audio_bytes})

    last_error: Optional[Exception] = None
    for model_name in models:
        try:
            model = genai.GenerativeModel(
                model_name=model_name, system_instruction=SYSTEM_PROMPT
            )
            response = model.generate_content(
                parts,
                generation_config={
                    "temperature": 0.2,
                    "response_mime_type": "application/json",
                    "max_output_tokens": 2048,
                },
            )
            return parse_model_json(response.text or "", today)
        except ExtractionError:
            raise
        except Exception as exc:  # model retired, quota, network, ...
            last_error = exc
            continue

    raise ExtractionError(f"Gemini call failed: {last_error}")


# ---------------------------------------------------------------------------
# Rule-based fallback (no API key, or Gemini unreachable)
# ---------------------------------------------------------------------------
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
# Longest first so "june" wins over "jun", and \b-anchored so "maybe" is not
# read as the month of May.
_MONTH_PATTERN = "|".join(
    sorted(set(_MONTH_NAMES) | set(_MONTHS), key=len, reverse=True)
)
_HIGH_PRIORITY = ("urgent", "asap", "critical", "immediately", "right away")
_LOW_PRIORITY = ("someday", "eventually", "no rush", "whenever", "if i get time")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def find_due_date(text: str, today: dt.date) -> Optional[dt.date]:
    """Spot the common ways a deadline gets written. Deliberately conservative."""
    lowered = text.lower()

    if "day after tomorrow" in lowered:
        return today + dt.timedelta(days=2)
    if "tomorrow" in lowered:
        return today + dt.timedelta(days=1)
    if "today" in lowered or "tonight" in lowered:
        return today
    if "end of the month" in lowered or "month end" in lowered:
        first_next = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        return first_next - dt.timedelta(days=1)
    if "end of the week" in lowered or "this week" in lowered:
        return today + dt.timedelta(days=(6 - today.weekday()) % 7)

    match = re.search(r"in (\d{1,3}) (day|days|week|weeks)", lowered)
    if match:
        count = int(match.group(1))
        return today + dt.timedelta(days=count * (7 if "week" in match.group(2) else 1))

    # "next monday" / "by friday"
    match = re.search(r"(next |by |on |before )?(" + "|".join(_WEEKDAYS) + r")", lowered)
    if match:
        target = _WEEKDAYS[match.group(2)]
        ahead = (target - today.weekday()) % 7
        if ahead == 0:
            ahead = 7
        if (match.group(1) or "").strip() == "next" and ahead < 7:
            ahead += 7
        return today + dt.timedelta(days=ahead)

    # "5 aug" / "5th august" / "aug 5"
    day = month = None
    match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_PATTERN + r")\b", lowered
    )
    if match:
        day, month = int(match.group(1)), _MONTHS[match.group(2)[:3]]
    else:
        swapped = re.search(
            r"\b(" + _MONTH_PATTERN + r")\b\s+(\d{1,2})(?:st|nd|rd|th)?\b", lowered
        )
        if swapped:
            day, month = int(swapped.group(2)), _MONTHS[swapped.group(1)[:3]]

    if day and month:
        year = today.year
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            return None
        if candidate < today:  # a date already past almost certainly means next year
            try:
                candidate = dt.date(year + 1, month, day)
            except ValueError:
                return None
        return candidate

    # ISO date written out in full
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", lowered)
    if match:
        return parse_date(match.group(1))

    return None


def find_due_time(text: str) -> Optional[str]:
    """Pick up '4pm', '16:30', 'at 9 am'."""
    lowered = text.lower()
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lowered)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3) == "pm":
            hour += 12
        return f"{hour:02d}:{match.group(2) or '00'}"
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", lowered)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def _looks_actionable(line: str) -> bool:
    """A crude filter so pure observations don't become to-dos."""
    stripped = line.strip()
    if len(stripped) < 3:
        return False
    if stripped.endswith("?"):
        return False
    return True


def extract_with_rules(text: str, today: Optional[dt.date] = None) -> Extraction:
    """
    No model involved. Splits the note into candidate lines and reads dates off
    each one. Good enough to keep the app usable; the review screen is where
    you fix what it got wrong.
    """
    today = today or today_ist()
    body = (text or "").strip()
    if not body:
        return Extraction(source="rules")

    lines = [line for line in body.splitlines() if line.strip()]
    bulleted = [line for line in lines if _BULLET_RE.match(line)]

    # If the note is bulleted, treat the bullets as the tasks. Otherwise fall
    # back to sentence splitting.
    if bulleted:
        candidates = [_BULLET_RE.sub("", line).strip() for line in bulleted]
    elif len(lines) > 1:
        candidates = [line.strip() for line in lines]
    else:
        candidates = [s.strip() for s in re.split(r"(?<=[.;!])\s+", body) if s.strip()]

    tasks = []
    for candidate in candidates[:MAX_TASKS]:
        if not _looks_actionable(candidate):
            continue
        lowered = candidate.lower()
        priority = "normal"
        if any(word in lowered for word in _HIGH_PRIORITY):
            priority = "high"
        elif any(word in lowered for word in _LOW_PRIORITY):
            priority = "low"

        due = find_due_date(candidate, today)
        tasks.append(
            {
                "title": candidate[:200],
                "details": "",
                "due": date_to_str(due),
                "due_time": find_due_time(candidate) if due else None,
                "priority": priority,
                "tags": [],
            }
        )

    first = body.splitlines()[0].strip()
    return Extraction(
        title=first[:70] + ("..." if len(first) > 70 else ""),
        summary="",
        transcript="",
        tasks=tasks,
        source="rules",
    )


# ---------------------------------------------------------------------------
# The one function the UI calls
# ---------------------------------------------------------------------------
def extract(
    *,
    text: str = "",
    audio: Optional[tuple[bytes, str]] = None,
    api_key: str = "",
    today: Optional[dt.date] = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
) -> Extraction:
    """
    Try Gemini; fall back to rules with an explanation attached rather than
    failing. A voice note with no API key genuinely cannot be handled, and that
    is the one case we raise.
    """
    today = today or today_ist()

    if api_key:
        try:
            return extract_with_gemini(
                api_key, text=text, audio=audio, today=today, models=models
            )
        except ExtractionError as exc:
            if audio:
                raise
            result = extract_with_rules(text, today)
            result.warning = f"Gemini could not be reached ({exc}). Used simple parsing instead."
            return result

    if audio:
        raise ExtractionError(
            "Voice notes need a Gemini API key - there is no offline transcriber. "
            "Add gemini_api_key in the app settings, or type the note instead."
        )

    result = extract_with_rules(text, today)
    result.warning = (
        "No Gemini API key set, so this used simple keyword parsing. "
        "Check the dates and titles below."
    )
    return result

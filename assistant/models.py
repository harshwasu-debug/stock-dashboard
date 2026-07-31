"""
models.py - the two records this app stores, and the rules for reading them
back safely.

A guiding principle here: data coming back off disk is never trusted. It may
have been written by an older version of this app, hand-edited in the GitHub
web UI, or half-written by a model. Every from_dict() therefore coerces rather
than crashes - a task with a nonsense priority becomes "normal", a task with a
garbled due date becomes a task with no due date. Losing one field is
recoverable; refusing to open your to-do list is not.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# Everything user-facing is in Indian Standard Time. Streamlit Cloud runs in
# UTC, so we never rely on the server's local clock.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

PRIORITIES = ("low", "normal", "high")
STATUSES = ("open", "done")
SOURCES = ("typed", "voice")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def now_ist() -> dt.datetime:
    """Current wall-clock time in IST."""
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    """Today's date in IST (not the server's date, which may be a day behind)."""
    return now_ist().date()


def iso_now() -> str:
    """Timestamp for created_at / updated_at fields."""
    return now_ist().replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Small coercion helpers - used by both from_dict() methods below.
# ---------------------------------------------------------------------------
def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = _as_str(value).lower()
    return text if text in allowed else default


def _as_tags(value: Any) -> list[str]:
    """Accepts a list, or a comma-separated string, and normalises to lowercase."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        return []

    tags: list[str] = []
    for part in parts:
        tag = _as_str(part).lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def parse_date(value: Any) -> Optional[dt.date]:
    """
    Turn whatever we were handed into a date, or None.

    Accepts a date, a datetime, or a 'YYYY-MM-DD' string (with or without a
    time component tacked on, which is what a model will sometimes return
    despite being asked for a bare date).
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _as_str(value)
    if not text:
        return None
    # Tolerate '2026-08-04T00:00:00' and '2026-08-04 09:00'.
    text = text.replace("T", " ").split(" ")[0]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def parse_time(value: Any) -> Optional[str]:
    """Normalise a clock time to 'HH:MM', or None if it isn't one."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    text = _as_str(value)
    if not text:
        return None
    match = _TIME_RE.match(text)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def date_to_str(value: Optional[dt.date]) -> Optional[str]:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------
@dataclass
class Task:
    """One to-do item. `due` is optional - plenty of tasks have no deadline."""

    id: str = field(default_factory=lambda: new_id("task"))
    title: str = ""
    details: str = ""
    due: Optional[str] = None            # 'YYYY-MM-DD'
    due_time: Optional[str] = None       # 'HH:MM' in IST, only if you gave one
    priority: str = "normal"
    tags: list[str] = field(default_factory=list)
    status: str = "open"
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)
    completed_at: Optional[str] = None
    note_id: Optional[str] = None        # which note this came out of
    calendar_event_id: Optional[str] = None

    # -- derived ------------------------------------------------------------
    @property
    def due_date(self) -> Optional[dt.date]:
        return parse_date(self.due)

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    def is_overdue(self, today: Optional[dt.date] = None) -> bool:
        due = self.due_date
        if due is None or self.is_done:
            return False
        return due < (today or today_ist())

    def is_due_today(self, today: Optional[dt.date] = None) -> bool:
        due = self.due_date
        if due is None or self.is_done:
            return False
        return due == (today or today_ist())

    def due_datetime(self) -> Optional[dt.datetime]:
        """The deadline as a timezone-aware datetime, if it has a clock time."""
        due = self.due_date
        if due is None or not self.due_time:
            return None
        hour, minute = (int(p) for p in self.due_time.split(":"))
        return dt.datetime(due.year, due.month, due.day, hour, minute, tzinfo=IST)

    def sort_key(self) -> tuple:
        """
        Order for display: soonest deadline first, undated last, and within a
        day, high priority first.
        """
        due = self.due_date
        rank = {"high": 0, "normal": 1, "low": 2}.get(self.priority, 1)
        return (due is None, due or dt.date.max, rank, self.created_at)

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "details": self.details,
            "due": self.due,
            "due_time": self.due_time,
            "priority": self.priority,
            "tags": list(self.tags),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "note_id": self.note_id,
            "calendar_event_id": self.calendar_event_id,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["Task"]:
        """
        Rebuild a Task from stored JSON. Returns None only if the row is so
        broken there is nothing to show (no id and no title).
        """
        if not isinstance(raw, dict):
            return None

        title = _as_str(raw.get("title"))
        task_id = _as_str(raw.get("id"))
        if not title and not task_id:
            return None

        due = date_to_str(parse_date(raw.get("due")))
        return cls(
            id=task_id or new_id("task"),
            title=title or "(untitled)",
            details=_as_str(raw.get("details")),
            due=due,
            # A time without a date is meaningless, so drop it.
            due_time=parse_time(raw.get("due_time")) if due else None,
            priority=_as_choice(raw.get("priority"), PRIORITIES, "normal"),
            tags=_as_tags(raw.get("tags")),
            status=_as_choice(raw.get("status"), STATUSES, "open"),
            created_at=_as_str(raw.get("created_at")) or iso_now(),
            updated_at=_as_str(raw.get("updated_at")) or iso_now(),
            completed_at=_as_str(raw.get("completed_at")) or None,
            note_id=_as_str(raw.get("note_id")) or None,
            calendar_event_id=_as_str(raw.get("calendar_event_id")) or None,
        )

    # -- mutation -----------------------------------------------------------
    def touch(self) -> None:
        self.updated_at = iso_now()

    def mark_done(self) -> None:
        self.status = "done"
        self.completed_at = iso_now()
        self.touch()

    def reopen(self) -> None:
        self.status = "open"
        self.completed_at = None
        self.touch()

    def snooze(self, days: int, today: Optional[dt.date] = None) -> None:
        """
        Push the deadline out. An undated task starts counting from today, so
        'snooze 1 week' on an undated task gives it a deadline a week out.
        """
        base = self.due_date or (today or today_ist())
        self.due = date_to_str(base + dt.timedelta(days=days))
        self.touch()


# ---------------------------------------------------------------------------
# Note
# ---------------------------------------------------------------------------
@dataclass
class Note:
    """
    The raw capture, kept forever. Tasks get ticked off and fade away; the note
    is the record of what you actually said, including the bits that never
    became tasks.
    """

    id: str = field(default_factory=lambda: new_id("note"))
    created_at: str = field(default_factory=iso_now)
    source: str = "typed"                # 'typed' or 'voice'
    title: str = ""
    body: str = ""                       # what you typed, or the transcript
    summary: str = ""
    task_ids: list[str] = field(default_factory=list)

    @property
    def created_date(self) -> dt.date:
        parsed = parse_date(self.created_at)
        return parsed or today_ist()

    def display_title(self) -> str:
        if self.title:
            return self.title
        first_line = (self.body or "").strip().splitlines()
        if first_line:
            text = first_line[0].strip()
            return text[:70] + ("..." if len(text) > 70 else "")
        return "(empty note)"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "source": self.source,
            "title": self.title,
            "body": self.body,
            "summary": self.summary,
            "task_ids": list(self.task_ids),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["Note"]:
        if not isinstance(raw, dict):
            return None
        return cls(
            id=_as_str(raw.get("id")) or new_id("note"),
            created_at=_as_str(raw.get("created_at")) or iso_now(),
            source=_as_choice(raw.get("source"), SOURCES, "typed"),
            title=_as_str(raw.get("title")),
            body=raw.get("body") if isinstance(raw.get("body"), str) else "",
            summary=_as_str(raw.get("summary")),
            task_ids=[_as_str(t) for t in raw.get("task_ids") or [] if _as_str(t)],
        )

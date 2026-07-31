"""
store.py - what the bytes mean.

Layout inside the data repo:

    tasks.json              every task, open and done, newest write wins
    notes/index.json        lightweight list of notes, so the Notes tab is
                            one fetch instead of one-per-note
    notes/2026-07/2026-07-31T19-30-00-note_ab12cd.md
                            the full note, with front matter

Notes are markdown rather than JSON on purpose: if you ever open the repo on a
laptop or on github.com, they read as plain text.

Every mutation goes through _mutate(), which re-reads immediately before
writing and retries on a conflict. That means two devices saving at the same
moment produce two commits, not one lost note.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Callable, Iterable, Optional

from .backends import Backend, BackendError, ConflictError, json_bytes
from .models import IST, Note, Task, iso_now, now_ist

TASKS_PATH = "tasks.json"
NOTES_DIR = "notes"
NOTES_INDEX_PATH = "notes/index.json"

_MAX_RETRIES = 4
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


class StoreError(RuntimeError):
    pass


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", text)


class TaskStore:
    """Reads and writes tasks and notes through whichever Backend it is given."""

    def __init__(self, backend: Backend):
        self.backend = backend

    # -- generic optimistic-write helper ------------------------------------
    def _mutate(self, path: str, apply: Callable[[Optional[dict]], dict], message: str) -> dict:
        """
        Re-read `path`, hand the parsed JSON to `apply`, write the result back.
        Retries if someone else wrote to the file in between.
        """
        last_error: Optional[Exception] = None
        for _ in range(_MAX_RETRIES):
            raw, version = self.backend.read(path)
            current = None
            if raw:
                try:
                    current = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise StoreError(
                        f"{path} is not valid JSON - refusing to overwrite it. "
                        f"Fix or delete the file in the repo. ({exc})"
                    ) from exc

            updated = apply(current)
            try:
                self.backend.write(path, json_bytes(updated), version, message)
                return updated
            except ConflictError as exc:
                last_error = exc
                continue

        raise StoreError(
            f"Could not write {path} after {_MAX_RETRIES} attempts: {last_error}"
        )

    # -- tasks --------------------------------------------------------------
    def load_tasks(self) -> list[Task]:
        raw, _ = self.backend.read(TASKS_PATH)
        if not raw:
            return []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StoreError(f"tasks.json is not readable: {exc}") from exc

        rows = payload.get("tasks") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []

        tasks = [Task.from_dict(row) for row in rows]
        return [t for t in tasks if t is not None]

    @staticmethod
    def _payload(tasks: Iterable[Task]) -> dict:
        return {
            "version": 1,
            "updated_at": iso_now(),
            "tasks": [t.to_dict() for t in tasks],
        }

    def replace_tasks(self, tasks: list[Task], message: str = "update tasks") -> None:
        """Blind overwrite. Only for a full-list save where the caller just read."""
        self._mutate(TASKS_PATH, lambda _current: self._payload(tasks), message)

    def add_tasks(self, new_tasks: list[Task], message: str = "") -> list[Task]:
        """Append tasks, keeping anything that arrived since we last read."""
        if not new_tasks:
            return []

        label = message or f"add {len(new_tasks)} task(s)"

        def apply(current: Optional[dict]) -> dict:
            existing = []
            if current:
                rows = current.get("tasks") if isinstance(current, dict) else current
                existing = [t for t in (Task.from_dict(r) for r in rows or []) if t]
            known = {t.id for t in existing}
            merged = existing + [t for t in new_tasks if t.id not in known]
            return self._payload(merged)

        self._mutate(TASKS_PATH, apply, label)
        return new_tasks

    def update_task(self, task_id: str, change: Callable[[Task], None], message: str = "") -> Optional[Task]:
        """
        Apply `change` to one task, in a way that is safe against a concurrent
        edit to a *different* task.
        """
        updated_holder: dict[str, Optional[Task]] = {"task": None}

        def apply(current: Optional[dict]) -> dict:
            rows = []
            if current:
                rows = current.get("tasks") if isinstance(current, dict) else current
            tasks = [t for t in (Task.from_dict(r) for r in rows or []) if t]
            for task in tasks:
                if task.id == task_id:
                    change(task)
                    task.touch()
                    updated_holder["task"] = task
                    break
            return self._payload(tasks)

        self._mutate(TASKS_PATH, apply, message or f"update task {task_id}")
        return updated_holder["task"]

    def delete_task(self, task_id: str, message: str = "") -> None:
        def apply(current: Optional[dict]) -> dict:
            rows = []
            if current:
                rows = current.get("tasks") if isinstance(current, dict) else current
            tasks = [t for t in (Task.from_dict(r) for r in rows or []) if t]
            return self._payload([t for t in tasks if t.id != task_id])

        self._mutate(TASKS_PATH, apply, message or f"delete task {task_id}")

    # -- notes --------------------------------------------------------------
    @staticmethod
    def note_path(note: Note) -> str:
        stamp = note.created_at.replace(":", "-").split("+")[0]
        month = stamp[:7] or now_ist().strftime("%Y-%m")
        return f"{NOTES_DIR}/{month}/{_safe_filename(stamp)}-{note.id}.md"

    @staticmethod
    def render_note(note: Note) -> str:
        """Markdown with a small front-matter block, readable in any editor."""
        front = {
            "id": note.id,
            "created_at": note.created_at,
            "source": note.source,
            "title": note.title,
            "summary": note.summary,
            "task_ids": note.task_ids,
        }
        return (
            "---\n"
            + json.dumps(front, indent=2, ensure_ascii=False)
            + "\n---\n\n"
            + (note.body or "")
            + "\n"
        )

    @staticmethod
    def parse_note(text: str) -> Note:
        match = _FRONT_MATTER_RE.match(text)
        if not match:
            return Note(body=text)
        try:
            front = json.loads(match.group(1))
        except json.JSONDecodeError:
            return Note(body=text)
        note = Note.from_dict(front) or Note()
        note.body = match.group(2).strip()
        return note

    def save_note(self, note: Note) -> str:
        """Write the note file, then add it to the index. Returns the path."""
        path = self.note_path(note)
        data = self.render_note(note).encode("utf-8")

        _, version = self.backend.read(path)
        self.backend.write(path, data, version, f"note: {note.display_title()[:60]}")

        def apply(current: Optional[dict]) -> dict:
            entries = []
            if isinstance(current, dict):
                entries = current.get("notes") or []
            elif isinstance(current, list):
                entries = current
            entries = [e for e in entries if isinstance(e, dict) and e.get("id") != note.id]
            entries.append(
                {
                    "id": note.id,
                    "path": path,
                    "created_at": note.created_at,
                    "source": note.source,
                    "title": note.display_title(),
                    "task_count": len(note.task_ids),
                }
            )
            entries.sort(key=lambda e: e.get("created_at") or "", reverse=True)
            return {"version": 1, "updated_at": iso_now(), "notes": entries}

        self._mutate(NOTES_INDEX_PATH, apply, "update note index")
        return path

    def list_notes(self) -> list[dict]:
        raw, _ = self.backend.read(NOTES_INDEX_PATH)
        if not raw:
            return []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
        entries = payload.get("notes") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]

    def load_note(self, path: str) -> Optional[Note]:
        raw, _ = self.backend.read(path)
        if not raw:
            return None
        return self.parse_note(raw.decode("utf-8"))

    # -- setup --------------------------------------------------------------
    def self_test(self) -> str:
        """Prove we can actually read and write. Surfaced on the Setup screen."""
        try:
            self.backend.read(TASKS_PATH)
        except BackendError as exc:
            return f"Read failed: {exc}"

        probe = f"{NOTES_DIR}/.write-check"
        try:
            _, version = self.backend.read(probe)
            self.backend.write(
                probe,
                f"last checked {iso_now()}\n".encode("utf-8"),
                version,
                "storage write check",
            )
        except BackendError as exc:
            return f"Write failed: {exc}"
        return "Read and write both OK."


# ---------------------------------------------------------------------------
# Grouping for the Tasks screen
# ---------------------------------------------------------------------------
def group_tasks(tasks: list[Task], today: Optional[dt.date] = None) -> dict[str, list[Task]]:
    """
    Bucket open tasks the way you'd actually triage them on a phone: what is
    late, what is today, what is coming, and what has no deadline at all.
    """
    today = today or now_ist().date()
    week_end = today + dt.timedelta(days=7)

    buckets: dict[str, list[Task]] = {
        "Overdue": [],
        "Today": [],
        "Next 7 days": [],
        "Later": [],
        "No deadline": [],
        "Done": [],
    }

    for task in tasks:
        if task.is_done:
            buckets["Done"].append(task)
            continue
        due = task.due_date
        if due is None:
            buckets["No deadline"].append(task)
        elif due < today:
            buckets["Overdue"].append(task)
        elif due == today:
            buckets["Today"].append(task)
        elif due <= week_end:
            buckets["Next 7 days"].append(task)
        else:
            buckets["Later"].append(task)

    for name, items in buckets.items():
        if name == "Done":
            items.sort(key=lambda t: t.completed_at or "", reverse=True)
        else:
            items.sort(key=lambda t: t.sort_key())

    return buckets

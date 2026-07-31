"""
app.py - the screen you actually touch.

Run it locally:      streamlit run assistant/app.py
On your phone:       open the deployed URL in Chrome, then
                     menu -> "Add to Home screen"

Three tabs: Capture (type or speak a note), Tasks (the list), Notes (what you
said, kept forever). A fourth, Setup, tells you which integrations are live.

The layout is deliberately vertical - single column, big touch targets, no
horizontal scrolling. A data grid is faster to write and miserable to use on a
phone.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import streamlit as st

# Lets `streamlit run assistant/app.py` find the package from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant.backends import BackendError, GitHubBackend  # noqa: E402
from assistant.calendar_sync import CalendarError  # noqa: E402
from assistant.config import (  # noqa: E402
    build_calendar_sync,
    build_store,
    load_settings,
)
from assistant.extract import ExtractionError, extract  # noqa: E402
from assistant.models import (  # noqa: E402
    Note,
    Task,
    date_to_str,
    new_id,
    today_ist,
)
from assistant.store import StoreError, group_tasks  # noqa: E402

st.set_page_config(
    page_title="Assistant",
    page_icon="\U0001F4DD",
    layout="centered",
    initial_sidebar_state="collapsed",
)

PRIORITY_LABELS = {"high": "High", "normal": "Normal", "low": "Low"}
PRIORITY_MARK = {"high": "\U0001F534", "normal": "", "low": "\U0001F535"}


# ---------------------------------------------------------------------------
# Auth - same shape as the stock dashboard's gate
# ---------------------------------------------------------------------------
def check_password(expected: str) -> None:
    if not expected:  # no password set -> no gate (local use)
        return
    if st.session_state.get("assistant_auth_ok"):
        return

    st.title("\U0001F512 Assistant")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == expected:
            st.session_state["assistant_auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
settings = load_settings()
check_password(settings.app_password)

store = build_store(settings)
calendar = build_calendar_sync(settings)


def load_tasks(force: bool = False) -> list[Task]:
    """Tasks are cached per session so every button press isn't a round trip."""
    if force or "tasks" not in st.session_state:
        try:
            st.session_state["tasks"] = store.load_tasks()
            st.session_state["tasks_error"] = ""
        except (StoreError, BackendError) as exc:
            st.session_state["tasks"] = []
            st.session_state["tasks_error"] = str(exc)
    return st.session_state["tasks"]


def sync_calendar_for(task: Task) -> str:
    """
    Push one task to Google Calendar. Returns a warning string, or "" if all
    is well. Never raises: a calendar problem must not cost you the task.
    """
    if calendar is None:
        return ""
    try:
        event_id = calendar.upsert(task)
        if event_id != task.calendar_event_id:
            task.calendar_event_id = event_id
            store.update_task(
                task.id,
                lambda t, eid=event_id: setattr(t, "calendar_event_id", eid),
                message=f"link calendar event for {task.id}",
            )
        return ""
    except CalendarError as exc:
        return f"Saved, but the calendar event failed: {exc}"


def drop_calendar_event(task: Task) -> None:
    if calendar is None or not task.calendar_event_id:
        return
    try:
        calendar.delete(task.calendar_event_id)
    except CalendarError:
        pass  # the task change matters more than a stale event


def flash(message: str, kind: str = "success") -> None:
    """Carry a message across the rerun that follows a button press."""
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    pending = st.session_state.pop("flash", None)
    if not pending:
        return
    kind, message = pending
    {"success": st.success, "warning": st.warning, "error": st.error}.get(
        kind, st.info
    )(message)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def render_capture() -> None:
    st.subheader("New note")

    if st.session_state.get("draft"):
        render_review()
        return

    typed = st.text_area(
        "Type it",
        key="capture_text",
        height=140,
        placeholder="Call the packaging vendor tomorrow. Menu costing review by Friday. "
        "Ask Ravi about the aggregator payout mismatch.",
    )

    audio = None
    if hasattr(st, "audio_input"):
        audio = st.audio_input("Or say it")
    else:
        st.caption(
            "Voice needs Streamlit 1.41+. Upgrade streamlit in requirements.txt."
        )
        uploaded = st.file_uploader("Or upload audio", type=["wav", "mp3", "m4a", "ogg", "webm"])
        audio = uploaded

    if not settings.has_gemini:
        st.info(
            "No Gemini key set. Typed notes will be parsed with simple keyword "
            "rules; voice notes need the key. See the Setup tab."
        )

    if st.button("Extract tasks", type="primary", use_container_width=True):
        audio_payload = None
        if audio is not None:
            audio_bytes = audio.getvalue()
            if audio_bytes:
                audio_payload = (audio_bytes, getattr(audio, "type", "audio/wav"))

        if not typed.strip() and audio_payload is None:
            st.warning("Type something or record something first.")
            return

        with st.spinner("Reading the note..."):
            try:
                result = extract(
                    text=typed,
                    audio=audio_payload,
                    api_key=settings.gemini_api_key,
                    today=today_ist(),
                    models=settings.models,
                )
            except ExtractionError as exc:
                st.error(str(exc))
                return

        st.session_state["draft"] = {
            "title": result.title,
            "summary": result.summary,
            "body": result.transcript or typed.strip(),
            "source": "voice" if audio_payload else "typed",
            "tasks": result.tasks,
            "warning": result.warning,
            "engine": result.source,
        }
        st.rerun()


def render_review() -> None:
    """
    The review screen. Nothing has been written yet at this point - this is the
    gate between "the model thinks" and "your to-do list says".
    """
    draft = st.session_state["draft"]

    st.caption(
        f"Read by: {'Gemini' if draft['engine'] == 'gemini' else 'simple keyword rules'}"
    )
    if draft.get("warning"):
        st.warning(draft["warning"])

    with st.expander("The note itself", expanded=draft["source"] == "voice"):
        draft["title"] = st.text_input("Title", value=draft.get("title", ""))
        draft["body"] = st.text_area("Body", value=draft.get("body", ""), height=160)
        if draft.get("summary"):
            st.caption(draft["summary"])

    tasks = draft["tasks"]
    if not tasks:
        st.info("No tasks found in this note. You can still save the note itself.")
    else:
        st.write(f"**{len(tasks)} task(s) found.** Check them before saving.")

    for index, task in enumerate(tasks):
        with st.container(border=True):
            keep = st.checkbox(
                "Add this one", value=True, key=f"keep_{index}"
            )
            task["title"] = st.text_input(
                "Task", value=task.get("title", ""), key=f"title_{index}",
                disabled=not keep,
            )

            left, right = st.columns(2)
            with left:
                current_due = task.get("due")
                due_value = dt.date.fromisoformat(current_due) if current_due else None
                new_due = st.date_input(
                    "Deadline",
                    value=due_value,
                    format="YYYY-MM-DD",
                    key=f"due_{index}",
                    disabled=not keep,
                )
                task["due"] = date_to_str(new_due) if new_due else None
            with right:
                task["priority"] = st.selectbox(
                    "Priority",
                    options=["low", "normal", "high"],
                    index=["low", "normal", "high"].index(task.get("priority", "normal")),
                    format_func=lambda p: PRIORITY_LABELS[p],
                    key=f"prio_{index}",
                    disabled=not keep,
                )

            task["tags"] = [
                t.strip().lstrip("#").lower()
                for t in st.text_input(
                    "Tags (comma separated)",
                    value=", ".join(task.get("tags", [])),
                    key=f"tags_{index}",
                    disabled=not keep,
                ).split(",")
                if t.strip()
            ]
            task["_keep"] = keep

            if task["due"] and calendar is not None:
                st.caption("Will create a Google Calendar reminder.")

    st.divider()
    save_col, discard_col = st.columns([2, 1])

    with save_col:
        if st.button("Save", type="primary", use_container_width=True):
            do_save(draft)

    with discard_col:
        if st.button("Discard", use_container_width=True):
            st.session_state.pop("draft", None)
            st.session_state.pop("capture_text", None)
            st.rerun()


def do_save(draft: dict) -> None:
    """Write the note, then the tasks, then the calendar events."""
    kept = [t for t in draft["tasks"] if t.get("_keep", True) and t.get("title", "").strip()]

    note = Note(
        id=new_id("note"),
        source=draft["source"],
        title=draft.get("title", ""),
        body=draft.get("body", ""),
        summary=draft.get("summary", ""),
    )

    new_tasks = [
        Task(
            title=row["title"].strip(),
            details=row.get("details", ""),
            due=row.get("due"),
            due_time=row.get("due_time") if row.get("due") else None,
            priority=row.get("priority", "normal"),
            tags=row.get("tags", []),
            note_id=note.id,
        )
        for row in kept
    ]
    note.task_ids = [t.id for t in new_tasks]

    warnings: list[str] = []
    try:
        with st.spinner("Saving..."):
            store.save_note(note)
            if new_tasks:
                store.add_tasks(new_tasks, message=f"add {len(new_tasks)} task(s) from note")
    except (StoreError, BackendError) as exc:
        st.error(f"Could not save: {exc}")
        return

    for task in new_tasks:
        if task.due_date:
            warning = sync_calendar_for(task)
            if warning:
                warnings.append(warning)

    st.session_state.pop("draft", None)
    st.session_state.pop("capture_text", None)
    load_tasks(force=True)

    if warnings:
        flash(" ".join(warnings), "warning")
    else:
        flash(f"Saved the note and {len(new_tasks)} task(s).")
    st.rerun()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def render_task_card(task: Task) -> None:
    with st.container(border=True):
        head, menu = st.columns([6, 1])

        with head:
            label = f"{PRIORITY_MARK.get(task.priority, '')} {task.title}".strip()
            checked = st.checkbox(label, value=task.is_done, key=f"done_{task.id}")

        if checked != task.is_done:
            if checked:
                store.update_task(task.id, lambda t: t.mark_done(), message=f"done: {task.title[:50]}")
                drop_calendar_event(task)
            else:
                store.update_task(task.id, lambda t: t.reopen(), message=f"reopen: {task.title[:50]}")
                task.status = "open"
                sync_calendar_for(task)
            load_tasks(force=True)
            st.rerun()

        bits = []
        if task.due:
            bits.append(f"due {task.due}" + (f" {task.due_time}" if task.due_time else ""))
        if task.tags:
            bits.append(" ".join(f"#{tag}" for tag in task.tags))
        if bits:
            st.caption(" · ".join(bits))

        with menu:
            with st.popover("⋯", use_container_width=True):
                render_task_editor(task)


def render_task_editor(task: Task) -> None:
    st.write("**Edit**")

    new_title = st.text_input("Title", value=task.title, key=f"edit_title_{task.id}")
    new_due = st.date_input(
        "Deadline",
        value=task.due_date,
        format="YYYY-MM-DD",
        key=f"edit_due_{task.id}",
    )
    new_priority = st.selectbox(
        "Priority",
        options=["low", "normal", "high"],
        index=["low", "normal", "high"].index(task.priority),
        format_func=lambda p: PRIORITY_LABELS[p],
        key=f"edit_prio_{task.id}",
    )

    if st.button("Save changes", key=f"save_{task.id}", use_container_width=True):
        due_str = date_to_str(new_due) if new_due else None

        def change(t: Task) -> None:
            t.title = new_title.strip() or t.title
            t.due = due_str
            if not due_str:
                t.due_time = None
            t.priority = new_priority

        updated = store.update_task(task.id, change, message=f"edit: {new_title[:50]}")
        if updated:
            if updated.due_date:
                sync_calendar_for(updated)
            elif task.calendar_event_id:
                drop_calendar_event(task)
                store.update_task(
                    task.id, lambda t: setattr(t, "calendar_event_id", None)
                )
        load_tasks(force=True)
        flash("Updated.")
        st.rerun()

    snooze_day, snooze_week = st.columns(2)
    with snooze_day:
        if st.button("+1 day", key=f"snooze1_{task.id}", use_container_width=True):
            apply_snooze(task, 1)
    with snooze_week:
        if st.button("+1 week", key=f"snooze7_{task.id}", use_container_width=True):
            apply_snooze(task, 7)

    if st.button("Delete", key=f"del_{task.id}", type="secondary", use_container_width=True):
        drop_calendar_event(task)
        store.delete_task(task.id, message=f"delete: {task.title[:50]}")
        load_tasks(force=True)
        flash("Deleted.")
        st.rerun()


def apply_snooze(task: Task, days: int) -> None:
    updated = store.update_task(
        task.id, lambda t: t.snooze(days), message=f"snooze {days}d: {task.title[:40]}"
    )
    if updated:
        sync_calendar_for(updated)
    load_tasks(force=True)
    flash(f"Pushed out {days} day(s).")
    st.rerun()


def render_quick_add() -> None:
    with st.expander("Quick add (no note)"):
        with st.form("quick_add", clear_on_submit=True):
            title = st.text_input("Task")
            due = st.date_input("Deadline (optional)", value=None, format="YYYY-MM-DD")
            submitted = st.form_submit_button("Add", use_container_width=True)

        if submitted and title.strip():
            task = Task(title=title.strip(), due=date_to_str(due) if due else None)
            try:
                store.add_tasks([task], message=f"quick add: {title[:50]}")
            except (StoreError, BackendError) as exc:
                st.error(f"Could not save: {exc}")
                return
            if task.due_date:
                sync_calendar_for(task)
            load_tasks(force=True)
            flash("Added.")
            st.rerun()


def render_tasks() -> None:
    tasks = load_tasks()

    if st.session_state.get("tasks_error"):
        st.error(st.session_state["tasks_error"])

    header, refresh = st.columns([4, 1])
    with header:
        open_count = sum(1 for t in tasks if not t.is_done)
        overdue = sum(1 for t in tasks if t.is_overdue())
        st.subheader(f"{open_count} open" + (f", {overdue} overdue" if overdue else ""))
    with refresh:
        if st.button("↻", use_container_width=True, help="Reload from storage"):
            load_tasks(force=True)
            st.rerun()

    render_quick_add()

    buckets = group_tasks(tasks)
    show_done = st.toggle("Show completed", value=False)

    any_shown = False
    for name in ("Overdue", "Today", "Next 7 days", "Later", "No deadline"):
        items = buckets[name]
        if not items:
            continue
        any_shown = True
        st.markdown(f"**{name}** ({len(items)})")
        for task in items:
            render_task_card(task)

    if not any_shown:
        st.info("Nothing open. Capture a note to add something.")

    if show_done and buckets["Done"]:
        st.markdown(f"**Done** ({len(buckets['Done'])})")
        for task in buckets["Done"][:50]:
            render_task_card(task)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
def render_notes() -> None:
    st.subheader("Notes")
    try:
        entries = store.list_notes()
    except (StoreError, BackendError) as exc:
        st.error(f"Could not list notes: {exc}")
        return

    if not entries:
        st.info("No notes yet.")
        return

    for entry in entries[:100]:
        created = (entry.get("created_at") or "")[:16].replace("T", " ")
        icon = "\U0001F3A4" if entry.get("source") == "voice" else "\U0001F4DD"
        with st.expander(f"{icon} {created} - {entry.get('title', '(untitled)')}"):
            note = store.load_note(entry.get("path", ""))
            if note is None:
                st.warning("The note file is missing from storage.")
                continue
            if note.summary:
                st.caption(note.summary)
            st.write(note.body or "(empty)")
            if entry.get("task_count"):
                st.caption(f"{entry['task_count']} task(s) came from this note.")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def render_setup() -> None:
    st.subheader("Setup")

    for problem in settings.problems:
        st.warning(problem)

    st.markdown("**Storage**")
    st.caption(store.backend.describe)
    if not settings.uses_github:
        st.warning(
            "Running on local files. On Streamlit Cloud the disk is wiped when the "
            "app restarts, so set notes_repo and github_token to keep anything."
        )
    if st.button("Test storage"):
        with st.spinner("Checking..."):
            if isinstance(store.backend, GitHubBackend):
                try:
                    st.info(store.backend.check())
                except BackendError as exc:
                    st.error(str(exc))
            st.info(store.self_test())

    st.markdown("**Voice and task extraction (Gemini)**")
    if settings.has_gemini:
        st.success(f"Key set. Models tried, in order: {', '.join(settings.models)}")
    else:
        st.error("No gemini_api_key. Typed notes fall back to keyword rules; voice won't work.")

    st.markdown("**Google Calendar**")
    if calendar is None:
        st.error("Not configured - deadlines will not create reminders.")
    else:
        st.caption(f"Service account: {calendar.service_account_email}")
        if st.button("Test calendar"):
            with st.spinner("Checking..."):
                try:
                    st.success(calendar.check())
                except CalendarError as exc:
                    st.error(str(exc))

    with st.expander("What goes in secrets"):
        st.code(
            """
app_password    = "something-only-you-know"
gemini_api_key  = "..."

# Private repo that holds your notes and tasks
notes_repo      = "your-username/your-notes-repo"
notes_branch    = "main"
github_token    = "github_pat_..."   # fine-grained, Contents: read+write

# Google Calendar
google_calendar_id = "you@gmail.com"
google_service_account = '''{"type":"service_account", ...}'''
            """.strip(),
            language="toml",
        )
        st.caption(
            "Share your calendar with the service account email, with "
            "'Make changes to events' permission, or it cannot write to it."
        )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.title("\U0001F4DD Assistant")
show_flash()

capture_tab, tasks_tab, notes_tab, setup_tab = st.tabs(
    ["Capture", "Tasks", "Notes", "Setup"]
)

with capture_tab:
    render_capture()
with tasks_tab:
    render_tasks()
with notes_tab:
    render_notes()
with setup_tab:
    render_setup()

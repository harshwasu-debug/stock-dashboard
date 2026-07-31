"""
calendar_sync.py - deadlines become Google Calendar events, so reminders
arrive through the notification system your phone already uses.

Auth is a service account, not OAuth. You create a service account in Google
Cloud, then *share your calendar with its email address* the same way you'd
share it with a colleague. That avoids an OAuth consent flow and refresh-token
storage inside a Streamlit app, which is a bad place to keep either.

How a deadline is rendered:

  no clock time  -> an all-day event on the due date, with a popup 900 minutes
                    before midnight, i.e. 9am the day before. All-day reminders
                    in Google are measured back from midnight, so "9am on the
                    day itself" is not expressible - the day before is the
                    closest useful thing, and it is what Google's own UI does.
  a clock time   -> a 30-minute timed event, popup 30 minutes before.

Tasks with no deadline never touch the calendar. Completing a task deletes its
event, so your calendar shows what is still outstanding rather than a history
of everything you ever wrote down.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from .models import IST, Task

SCOPES = ("https://www.googleapis.com/auth/calendar",)
DEFAULT_TIMEZONE = "Asia/Kolkata"

# 900 minutes before midnight = 09:00 the previous day.
ALL_DAY_REMINDER_MINUTES = 900
TIMED_REMINDER_MINUTES = 30
TIMED_EVENT_MINUTES = 30


class CalendarError(RuntimeError):
    pass


def _parse_service_account(raw: Any) -> dict:
    """Secrets may hold the service-account JSON as a dict or as a string."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalendarError(
                f"google_service_account is not valid JSON: {exc}"
            ) from exc
    raise CalendarError("No service account credentials provided.")


class CalendarSync:
    """Thin wrapper over the Calendar API - create, update, delete one event."""

    def __init__(
        self,
        service_account: Any,
        calendar_id: str,
        timezone: str = DEFAULT_TIMEZONE,
    ):
        self._info = _parse_service_account(service_account)
        self.calendar_id = calendar_id
        self.timezone = timezone
        self._service = None

        if not calendar_id:
            raise CalendarError("No calendar id configured.")

    # -- plumbing -----------------------------------------------------------
    def _client(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2 import service_account as sa
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise CalendarError(
                "google-api-python-client / google-auth are not installed. "
                "Run: pip install -r requirements.txt"
            ) from exc

        try:
            credentials = sa.Credentials.from_service_account_info(
                self._info, scopes=list(SCOPES)
            )
        except Exception as exc:
            raise CalendarError(f"Service account credentials rejected: {exc}") from exc

        self._service = build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )
        return self._service

    # -- event shape --------------------------------------------------------
    def _event_body(self, task: Task) -> dict:
        due = task.due_date
        if due is None:
            raise CalendarError("Task has no deadline, so it has no event.")

        description_parts = []
        if task.details:
            description_parts.append(task.details)
        if task.tags:
            description_parts.append("Tags: " + ", ".join(task.tags))
        description_parts.append(f"(assistant task {task.id})")

        body: dict = {
            "summary": task.title,
            "description": "\n\n".join(description_parts),
            # Lets us find our own events later, and tells anyone looking at
            # the raw event where it came from.
            "source": {"title": "assistant", "url": "https://claude.ai/code"},
        }

        start_dt = task.due_datetime()
        if start_dt is not None:
            end_dt = start_dt + dt.timedelta(minutes=TIMED_EVENT_MINUTES)
            body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": self.timezone}
            body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": self.timezone}
            reminder_minutes = TIMED_REMINDER_MINUTES
        else:
            body["start"] = {"date": due.isoformat()}
            body["end"] = {"date": (due + dt.timedelta(days=1)).isoformat()}
            reminder_minutes = ALL_DAY_REMINDER_MINUTES

        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": reminder_minutes}],
        }
        if task.priority == "high":
            body["colorId"] = "11"  # tomato red
        return body

    # -- operations ---------------------------------------------------------
    def upsert(self, task: Task) -> Optional[str]:
        """
        Make the calendar match the task. Returns the event id, or None if the
        task has no deadline (in which case any existing event is removed).
        """
        if task.due_date is None:
            if task.calendar_event_id:
                self.delete(task.calendar_event_id)
            return None

        service = self._client()
        body = self._event_body(task)

        if task.calendar_event_id:
            try:
                updated = (
                    service.events()
                    .update(
                        calendarId=self.calendar_id,
                        eventId=task.calendar_event_id,
                        body=body,
                    )
                    .execute()
                )
                return updated.get("id")
            except Exception as exc:
                # Event deleted from the phone, or otherwise gone - make a new
                # one rather than losing the reminder entirely.
                if not _is_missing(exc):
                    raise CalendarError(f"Could not update the calendar event: {exc}") from exc

        try:
            created = (
                service.events()
                .insert(calendarId=self.calendar_id, body=body)
                .execute()
            )
        except Exception as exc:
            raise CalendarError(f"Could not create the calendar event: {exc}") from exc
        return created.get("id")

    def delete(self, event_id: str) -> None:
        if not event_id:
            return
        service = self._client()
        try:
            service.events().delete(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
        except Exception as exc:
            if _is_missing(exc):
                return  # already gone, nothing to do
            raise CalendarError(f"Could not delete the calendar event: {exc}") from exc

    def check(self) -> str:
        """Confirm the calendar is reachable and actually shared with us."""
        service = self._client()
        try:
            info = service.calendars().get(calendarId=self.calendar_id).execute()
        except Exception as exc:
            if _is_missing(exc):
                raise CalendarError(
                    f"Calendar '{self.calendar_id}' not found. Did you share it with "
                    f"the service account email ({self.service_account_email})?"
                ) from exc
            raise CalendarError(f"Calendar check failed: {exc}") from exc
        return f"Connected to calendar: {info.get('summary', self.calendar_id)}"

    @property
    def service_account_email(self) -> str:
        return self._info.get("client_email", "(unknown)")


def _is_missing(exc: Exception) -> bool:
    """True for a 404/410 from the Google client, without importing its errors."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (404, 410):
        return True
    return "notFound" in str(exc) or "deleted" in str(exc).lower()


def build_calendar(
    service_account: Any, calendar_id: str, timezone: str = DEFAULT_TIMEZONE
) -> Optional[CalendarSync]:
    """Returns None (rather than raising) when calendar sync isn't configured."""
    if not service_account or not calendar_id:
        return None
    try:
        return CalendarSync(service_account, calendar_id, timezone)
    except CalendarError:
        return None

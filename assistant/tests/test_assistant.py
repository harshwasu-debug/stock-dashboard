"""
Tests for the assistant. No network, no Streamlit - every module except app.py
is plain Python, which is the point of the split.

Run:  python -m pytest assistant/tests -q
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from assistant.backends import ConflictError, LocalBackend, BackendError  # noqa: E402
from assistant.calendar_sync import CalendarSync  # noqa: E402
from assistant.extract import (  # noqa: E402
    ExtractionError,
    extract_with_rules,
    find_due_date,
    find_due_time,
    parse_model_json,
)
from assistant.models import Note, Task  # noqa: E402
from assistant.store import TaskStore, group_tasks  # noqa: E402

TODAY = dt.date(2026, 7, 31)  # a Friday


@pytest.fixture()
def store(tmp_path) -> TaskStore:
    return TaskStore(LocalBackend(tmp_path))


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class TestTaskModel:
    def test_roundtrip(self):
        task = Task(title="Call vendor", due="2026-08-04", tags=["packaging"])
        restored = Task.from_dict(task.to_dict())
        assert restored.title == "Call vendor"
        assert restored.due_date == dt.date(2026, 8, 4)
        assert restored.tags == ["packaging"]

    def test_garbage_fields_are_coerced_not_fatal(self):
        task = Task.from_dict(
            {
                "id": "task_1",
                "title": "Do the thing",
                "due": "not-a-date",
                "priority": "SUPER URGENT",
                "status": "banana",
                "tags": "one, #Two, one",
            }
        )
        assert task is not None
        assert task.due is None            # unparseable date dropped, not raised
        assert task.priority == "normal"   # unknown priority falls back
        assert task.status == "open"
        assert task.tags == ["one", "two"]  # deduped, lowercased, # stripped

    def test_row_with_nothing_useful_is_dropped(self):
        assert Task.from_dict({"details": "orphaned"}) is None
        assert Task.from_dict("not a dict") is None

    def test_datetime_due_is_accepted(self):
        task = Task.from_dict({"title": "x", "due": "2026-08-04T00:00:00"})
        assert task.due == "2026-08-04"

    def test_time_without_date_is_discarded(self):
        task = Task.from_dict({"title": "x", "due_time": "16:30"})
        assert task.due_time is None

    def test_overdue_and_today(self):
        overdue = Task(title="late", due="2026-07-30")
        today = Task(title="now", due="2026-07-31")
        assert overdue.is_overdue(TODAY)
        assert not overdue.is_due_today(TODAY)
        assert today.is_due_today(TODAY)
        assert not today.is_overdue(TODAY)

    def test_done_task_is_never_overdue(self):
        task = Task(title="late", due="2026-07-01")
        task.mark_done()
        assert not task.is_overdue(TODAY)
        assert task.completed_at

    def test_snooze_from_existing_deadline(self):
        task = Task(title="x", due="2026-08-01")
        task.snooze(7, TODAY)
        assert task.due == "2026-08-08"

    def test_snooze_undated_task_counts_from_today(self):
        task = Task(title="x")
        task.snooze(1, TODAY)
        assert task.due == "2026-08-01"

    def test_due_datetime_needs_a_time(self):
        assert Task(title="x", due="2026-08-04").due_datetime() is None
        stamped = Task(title="x", due="2026-08-04", due_time="16:30").due_datetime()
        assert stamped.hour == 16 and stamped.minute == 30

    def test_sort_puts_undated_last_and_urgent_first(self):
        rows = [
            Task(title="undated"),
            Task(title="later", due="2026-09-01"),
            Task(title="soon-normal", due="2026-08-01"),
            Task(title="soon-high", due="2026-08-01", priority="high"),
        ]
        titles = [t.title for t in sorted(rows, key=lambda t: t.sort_key())]
        assert titles == ["soon-high", "soon-normal", "later", "undated"]


class TestNoteModel:
    def test_display_title_falls_back_to_first_line(self):
        note = Note(body="Ring the supplier\nthen check stock")
        assert note.display_title() == "Ring the supplier"

    def test_empty_note(self):
        assert Note().display_title() == "(empty note)"


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------
class TestLocalBackend:
    def test_write_then_read(self, tmp_path):
        backend = LocalBackend(tmp_path)
        assert backend.read("a.json") == (None, None)

        version = backend.write("a.json", b"hello", None, "msg")
        data, current = backend.read("a.json")
        assert data == b"hello"
        assert current == version

    def test_stale_version_is_rejected(self, tmp_path):
        backend = LocalBackend(tmp_path)
        first = backend.write("a.json", b"one", None, "msg")
        backend.write("a.json", b"two", first, "msg")

        with pytest.raises(ConflictError):
            backend.write("a.json", b"three", first, "msg")

    def test_creating_a_file_that_already_exists_conflicts(self, tmp_path):
        backend = LocalBackend(tmp_path)
        backend.write("a.json", b"one", None, "msg")
        with pytest.raises(ConflictError):
            backend.write("a.json", b"two", None, "msg")

    def test_path_escape_is_refused(self, tmp_path):
        backend = LocalBackend(tmp_path / "data")
        with pytest.raises(BackendError):
            backend.write("../escaped.json", b"x", None, "msg")

    def test_list_only_returns_files_in_that_dir(self, tmp_path):
        backend = LocalBackend(tmp_path)
        backend.write("notes/one.md", b"a", None, "m")
        backend.write("notes/two.md", b"b", None, "m")
        backend.write("other.md", b"c", None, "m")
        assert backend.list("notes") == ["notes/one.md", "notes/two.md"]


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


class TestGitHubBackendDiagnostics:
    """
    check() is the button you press on a phone when something is wrong, so its
    message has to point at the real cause. These cases are taken from actual
    responses seen during setup.
    """

    def _backend(self, response, monkeypatch):
        from assistant.backends import GitHubBackend

        backend = GitHubBackend("owner", "repo", "token")
        monkeypatch.setattr(backend, "_session", lambda: FakeSession(response))
        return backend

    def test_404_explains_that_private_looks_like_missing(self, monkeypatch):
        backend = self._backend(FakeResponse(404, {"message": "Not Found"}), monkeypatch)
        with pytest.raises(BackendError) as exc:
            backend.check()
        assert "select repositories" in str(exc.value)

    def test_401_blames_the_token(self, monkeypatch):
        backend = self._backend(FakeResponse(401, {"message": "Bad credentials"}), monkeypatch)
        with pytest.raises(BackendError) as exc:
            backend.check()
        assert "401" in str(exc.value) and "new one" in str(exc.value)

    def test_403_quotes_github_and_does_not_blame_the_token(self, monkeypatch):
        """
        A real 403 seen in testing came from a network policy, not the token.
        Saying 'GitHub rejected the token' there sends you hunting for a
        problem that does not exist.
        """
        backend = self._backend(
            FakeResponse(403, {"message": "access to this repository is not enabled"}),
            monkeypatch,
        )
        with pytest.raises(BackendError) as exc:
            backend.check()
        message = str(exc.value)
        assert "access to this repository is not enabled" in message
        assert "network policy" in message
        assert "rejected the token" not in message

    def test_public_repo_is_called_out_as_a_warning(self, monkeypatch):
        backend = self._backend(
            FakeResponse(200, {"private": False, "name": "repo"}), monkeypatch
        )
        assert "WARNING" in backend.check()

    def test_private_repo_is_reported_as_fine(self, monkeypatch):
        backend = self._backend(
            FakeResponse(200, {"private": True, "name": "repo"}), monkeypatch
        )
        result = backend.check()
        assert "private" in result and "WARNING" not in result

    def test_unparseable_body_still_produces_a_message(self, monkeypatch):
        backend = self._backend(FakeResponse(500, None, text="<html>oops"), monkeypatch)
        with pytest.raises(BackendError) as exc:
            backend.check()
        assert "500" in str(exc.value)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class TestTaskStore:
    def test_empty_store_reads_as_empty_list(self, store):
        assert store.load_tasks() == []

    def test_add_and_load(self, store):
        store.add_tasks([Task(title="one"), Task(title="two")])
        assert [t.title for t in store.load_tasks()] == ["one", "two"]

    def test_add_appends_rather_than_replacing(self, store):
        store.add_tasks([Task(title="first")])
        store.add_tasks([Task(title="second")])
        assert len(store.load_tasks()) == 2

    def test_add_is_idempotent_for_the_same_id(self, store):
        task = Task(title="one")
        store.add_tasks([task])
        store.add_tasks([task])
        assert len(store.load_tasks()) == 1

    def test_update_one_task_leaves_the_others_alone(self, store):
        keep = Task(title="keep")
        change = Task(title="change")
        store.add_tasks([keep, change])

        store.update_task(change.id, lambda t: t.mark_done())
        by_id = {t.id: t for t in store.load_tasks()}
        assert by_id[change.id].is_done
        assert not by_id[keep.id].is_done

    def test_update_bumps_updated_at(self, store):
        task = Task(title="x", updated_at="2000-01-01T00:00:00+05:30")
        store.add_tasks([task])
        updated = store.update_task(task.id, lambda t: setattr(t, "title", "y"))
        assert updated.updated_at > "2000-01-01"

    def test_update_missing_task_returns_none(self, store):
        assert store.update_task("nope", lambda t: None) is None

    def test_delete(self, store):
        task = Task(title="bye")
        store.add_tasks([task, Task(title="stay")])
        store.delete_task(task.id)
        assert [t.title for t in store.load_tasks()] == ["stay"]

    def test_corrupt_tasks_file_refuses_to_overwrite(self, store):
        store.backend.write("tasks.json", b"{ not json", None, "corrupt")
        with pytest.raises(Exception):
            store.load_tasks()

    def test_unknown_rows_are_skipped_not_fatal(self, store):
        payload = {"tasks": [{"title": "good"}, "garbage", {"nothing": 1}]}
        store.backend.write(
            "tasks.json", json.dumps(payload).encode(), None, "seed"
        )
        assert [t.title for t in store.load_tasks()] == ["good"]

    def test_bare_list_format_is_still_readable(self, store):
        """An older or hand-edited tasks.json that is just an array."""
        store.backend.write(
            "tasks.json", json.dumps([{"title": "legacy"}]).encode(), None, "seed"
        )
        assert [t.title for t in store.load_tasks()] == ["legacy"]


class TestNotes:
    def test_note_roundtrip_through_markdown(self, store):
        note = Note(source="voice", title="Vendor call", body="Ring them tomorrow.")
        note.task_ids = ["task_1"]
        path = store.save_note(note)

        loaded = store.load_note(path)
        assert loaded.body == "Ring them tomorrow."
        assert loaded.source == "voice"
        assert loaded.task_ids == ["task_1"]

    def test_note_body_with_front_matter_lookalike_survives(self, store):
        note = Note(body="---\nnot really front matter\n---")
        loaded = store.load_note(store.save_note(note))
        assert "not really front matter" in loaded.body

    def test_index_lists_newest_first(self, store):
        store.save_note(Note(created_at="2026-07-01T09:00:00+05:30", title="older"))
        store.save_note(Note(created_at="2026-07-30T09:00:00+05:30", title="newer"))
        assert [e["title"] for e in store.list_notes()] == ["newer", "older"]

    def test_plain_markdown_without_front_matter_still_opens(self, store):
        store.backend.write("notes/loose.md", b"just some text", None, "m")
        assert store.load_note("notes/loose.md").body == "just some text"

    def test_missing_note_returns_none(self, store):
        assert store.load_note("notes/nope.md") is None


class TestGrouping:
    def test_buckets(self):
        tasks = [
            Task(title="late", due="2026-07-20"),
            Task(title="today", due="2026-07-31"),
            Task(title="this week", due="2026-08-03"),
            Task(title="later", due="2026-12-01"),
            Task(title="undated"),
        ]
        done = Task(title="finished", due="2026-07-01")
        done.mark_done()
        tasks.append(done)

        buckets = group_tasks(tasks, TODAY)
        assert [t.title for t in buckets["Overdue"]] == ["late"]
        assert [t.title for t in buckets["Today"]] == ["today"]
        assert [t.title for t in buckets["Next 7 days"]] == ["this week"]
        assert [t.title for t in buckets["Later"]] == ["later"]
        assert [t.title for t in buckets["No deadline"]] == ["undated"]
        assert [t.title for t in buckets["Done"]] == ["finished"]

    def test_done_tasks_never_appear_as_overdue(self):
        done = Task(title="x", due="2026-01-01")
        done.mark_done()
        assert group_tasks([done], TODAY)["Overdue"] == []


# ---------------------------------------------------------------------------
# extraction - model output parsing
# ---------------------------------------------------------------------------
class TestParseModelJson:
    def test_plain_json(self):
        payload = json.dumps(
            {
                "title": "Vendor",
                "summary": "calls to make",
                "tasks": [
                    {"title": "Call vendor", "due": "2026-08-04", "priority": "high",
                     "tags": ["packaging"]}
                ],
            }
        )
        result = parse_model_json(payload, TODAY)
        assert result.source == "gemini"
        assert result.tasks[0]["due"] == "2026-08-04"
        assert result.tasks[0]["priority"] == "high"

    def test_code_fenced_json(self):
        text = '```json\n{"tasks": [{"title": "Do it"}]}\n```'
        assert parse_model_json(text, TODAY).tasks[0]["title"] == "Do it"

    def test_chatty_reply_with_json_inside(self):
        text = 'Sure! Here you go:\n{"tasks": [{"title": "Do it"}]}\nHope that helps.'
        assert parse_model_json(text, TODAY).tasks[0]["title"] == "Do it"

    def test_null_due_is_preserved_not_invented(self):
        result = parse_model_json('{"tasks":[{"title":"Someday thing","due":null}]}', TODAY)
        assert result.tasks[0]["due"] is None

    def test_far_past_date_is_dropped_as_a_parsing_slip(self):
        result = parse_model_json('{"tasks":[{"title":"x","due":"1999-01-01"}]}', TODAY)
        assert result.tasks[0]["due"] is None

    def test_time_without_date_is_dropped(self):
        result = parse_model_json(
            '{"tasks":[{"title":"x","due":null,"due_time":"16:00"}]}', TODAY
        )
        assert result.tasks[0]["due_time"] is None

    def test_tags_are_capped_and_cleaned(self):
        result = parse_model_json(
            '{"tasks":[{"title":"x","tags":["#One","two","three","four"]}]}', TODAY
        )
        assert result.tasks[0]["tags"] == ["one", "two", "three"]

    def test_titleless_tasks_are_dropped(self):
        result = parse_model_json('{"tasks":[{"details":"no title"},{"title":"ok"}]}', TODAY)
        assert [t["title"] for t in result.tasks] == ["ok"]

    def test_empty_reply_raises(self):
        with pytest.raises(ExtractionError):
            parse_model_json("", TODAY)

    def test_non_json_raises(self):
        with pytest.raises(ExtractionError):
            parse_model_json("I could not understand the note.", TODAY)

    def test_json_that_is_not_an_object_raises(self):
        with pytest.raises(ExtractionError):
            parse_model_json("[1, 2, 3]", TODAY)


# ---------------------------------------------------------------------------
# extraction - rule-based fallback
# ---------------------------------------------------------------------------
class TestDateParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("call them tomorrow", dt.date(2026, 8, 1)),
            ("do it today", dt.date(2026, 7, 31)),
            ("day after tomorrow", dt.date(2026, 8, 2)),
            ("in 3 days", dt.date(2026, 8, 3)),
            ("in 2 weeks", dt.date(2026, 8, 14)),
            ("by monday", dt.date(2026, 8, 3)),
            ("next monday", dt.date(2026, 8, 10)),
            ("by 5 aug", dt.date(2026, 8, 5)),
            ("by 5th august", dt.date(2026, 8, 5)),
            ("on august 5", dt.date(2026, 8, 5)),
            ("before 2026-09-15", dt.date(2026, 9, 15)),
            ("end of the month", dt.date(2026, 7, 31)),
        ],
    )
    def test_recognised_forms(self, text, expected):
        assert find_due_date(text, TODAY) == expected

    def test_no_date_means_no_date(self):
        assert find_due_date("think about the new menu", TODAY) is None

    def test_maybe_is_not_the_month_of_may(self):
        assert find_due_date("maybe 5 people are coming", TODAY) is None

    def test_a_date_already_past_rolls_to_next_year(self):
        assert find_due_date("by 5 jan", TODAY) == dt.date(2027, 1, 5)

    @pytest.mark.parametrize(
        "text,expected",
        [("at 4pm", "16:00"), ("at 9 am", "09:00"), ("16:30", "16:30"), ("7:45 pm", "19:45")],
    )
    def test_times(self, text, expected):
        assert find_due_time(text) == expected

    def test_no_time(self):
        assert find_due_time("sometime next week") is None


class TestRuleExtraction:
    def test_bullets_become_tasks(self):
        note = "- Call the vendor tomorrow\n- Review menu costing\n- Pay the GST"
        result = extract_with_rules(note, TODAY)
        assert len(result.tasks) == 3
        assert result.tasks[0]["due"] == "2026-08-01"
        assert result.tasks[1]["due"] is None

    def test_numbered_lists_too(self):
        result = extract_with_rules("1. First thing\n2) Second thing", TODAY)
        assert [t["title"] for t in result.tasks] == ["First thing", "Second thing"]

    def test_sentences_are_split_when_there_are_no_bullets(self):
        result = extract_with_rules("Call the vendor. Check the stock.", TODAY)
        assert len(result.tasks) == 2

    def test_priority_keywords(self):
        result = extract_with_rules("- Fix the billing urgently\n- Reorder cups someday", TODAY)
        assert result.tasks[0]["priority"] == "high"
        assert result.tasks[1]["priority"] == "low"

    def test_questions_are_not_tasks(self):
        result = extract_with_rules("- Should we raise prices?\n- Raise prices", TODAY)
        assert [t["title"] for t in result.tasks] == ["Raise prices"]

    def test_empty_note(self):
        assert extract_with_rules("   ", TODAY).tasks == []


# ---------------------------------------------------------------------------
# calendar - event shape only, no API calls
# ---------------------------------------------------------------------------
FAKE_SERVICE_ACCOUNT = {
    "type": "service_account",
    "client_email": "assistant@example.iam.gserviceaccount.com",
}


class TestCalendarEventBody:
    def _sync(self):
        return CalendarSync(FAKE_SERVICE_ACCOUNT, "me@example.com")

    def test_all_day_event_for_a_date_only_deadline(self):
        body = self._sync()._event_body(Task(title="Pay GST", due="2026-08-04"))
        assert body["start"] == {"date": "2026-08-04"}
        # end is exclusive in the Calendar API, so a one-day event ends the 5th
        assert body["end"] == {"date": "2026-08-05"}
        assert body["reminders"]["overrides"][0]["minutes"] == 900

    def test_timed_event_when_a_clock_time_was_given(self):
        body = self._sync()._event_body(
            Task(title="Call vendor", due="2026-08-04", due_time="16:30")
        )
        assert body["start"]["dateTime"].startswith("2026-08-04T16:30")
        assert body["start"]["timeZone"] == "Asia/Kolkata"
        assert body["end"]["dateTime"].startswith("2026-08-04T17:00")
        assert body["reminders"]["overrides"][0]["minutes"] == 30

    def test_high_priority_is_coloured(self):
        body = self._sync()._event_body(
            Task(title="x", due="2026-08-04", priority="high")
        )
        assert body["colorId"] == "11"

    def test_task_id_is_recorded_on_the_event(self):
        task = Task(title="x", due="2026-08-04")
        assert task.id in self._sync()._event_body(task)["description"]

    def test_service_account_json_string_is_accepted(self):
        sync = CalendarSync(json.dumps(FAKE_SERVICE_ACCOUNT), "me@example.com")
        assert sync.service_account_email.endswith("gserviceaccount.com")

    def test_undated_task_has_no_event(self):
        with pytest.raises(Exception):
            self._sync()._event_body(Task(title="someday"))

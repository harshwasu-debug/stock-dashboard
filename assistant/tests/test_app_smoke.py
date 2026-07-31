"""
Smoke tests for the Streamlit screen itself.

These use Streamlit's own AppTest harness, which executes app.py the same way
the server does. They catch the errors unit tests can't: a widget used wrongly,
a bad import, an API that moved between Streamlit versions.

No API keys are set here, so the app runs in its "nothing configured yet"
state - local files, keyword extraction, no calendar. That is exactly the
state it will be in the first time it is opened, so it is worth testing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

APP = str(Path(__file__).resolve().parents[1] / "app.py")

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


@pytest.fixture()
def data_dir(tmp_path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def app(tmp_path, data_dir, monkeypatch):
    # Run in a scratch directory so the local-file backend writes there.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCAL_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("NOTES_REPO", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    return AppTest.from_file(APP, default_timeout=30)


def test_app_starts_clean(app):
    app.run()
    assert not app.exception, [str(e) for e in app.exception]


def test_no_password_set_means_no_gate(app):
    app.run()
    assert any("Assistant" in t.value for t in app.title)


def test_capture_to_saved_task(app, data_dir):
    """Type a note, extract, save - the whole Phase 1 path in one go."""
    app.run()

    app.text_area(key="capture_text").set_value(
        "- Call the packaging vendor tomorrow\n- Review the menu costing"
    )
    app.button[0].click().run()          # Extract tasks
    assert not app.exception, [str(e) for e in app.exception]

    # Two task cards should now be on the review screen.
    assert len(app.text_input) >= 2
    titles = [t.value for t in app.text_input]
    assert any("packaging vendor" in title for title in titles)

    # Save is the primary button on the review screen.
    save = [b for b in app.button if b.label == "Save"]
    assert save, [b.label for b in app.button]
    save[0].click().run()
    assert not app.exception, [str(e) for e in app.exception]

    assert any("Saved" in str(s.value) for s in app.success)

    # ...and it really is on disk, not just on screen.
    stored = json.loads((data_dir / "tasks.json").read_text())
    assert [t["title"] for t in stored["tasks"]] == [
        "Call the packaging vendor tomorrow",
        "Review the menu costing",
    ]
    assert stored["tasks"][0]["due"] is not None   # "tomorrow" was resolved
    assert stored["tasks"][1]["due"] is None       # nothing stated, nothing invented
    assert list((data_dir / "notes").glob("*/*.md")), "the note itself was not written"


def test_discard_throws_the_draft_away(app):
    app.run()
    app.text_area(key="capture_text").set_value("- Something to do")
    app.button[0].click().run()

    discard = [b for b in app.button if b.label == "Discard"]
    assert discard
    discard[0].click().run()
    assert not app.exception, [str(e) for e in app.exception]
    # Back to the capture screen: the text area is empty again.
    assert app.text_area(key="capture_text").value in ("", None)


def test_setup_tab_warns_about_local_storage(app):
    app.run()
    assert not app.exception
    warnings = " ".join(str(w.value) for w in app.warning)
    assert "local files" in warnings.lower()

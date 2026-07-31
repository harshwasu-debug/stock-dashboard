"""
config.py - one place that knows where settings come from.

Order of precedence, per setting: Streamlit secrets, then environment
variable, then a default. Secrets win because that is where they live on
Streamlit Cloud; env vars are there so you can run it locally without a
secrets file, and so a CI job could reuse the same modules.

Nothing here raises when a setting is missing. An unconfigured app should
still start, run on local files, and tell you what is missing on the Setup
screen - not greet you with a stack trace on your phone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .backends import Backend, GitHubBackend, LocalBackend
from .calendar_sync import DEFAULT_TIMEZONE, CalendarSync, build_calendar
from .store import TaskStore

# Where local-mode data goes when no GitHub repo is configured.
DEFAULT_LOCAL_DIR = "assistant_data"


def _from_secrets(key: str) -> Optional[Any]:
    """Read one key out of st.secrets, tolerating "no secrets file at all"."""
    try:
        import streamlit as st
    except ImportError:
        return None
    try:
        value = st.secrets.get(key)
    except Exception:
        # Streamlit raises if no secrets.toml exists anywhere; that is fine.
        return None
    return value


def setting(key: str, default: Any = "") -> Any:
    value = _from_secrets(key)
    if value not in (None, ""):
        return value
    value = os.environ.get(key.upper())
    if value not in (None, ""):
        return value
    return default


@dataclass
class Settings:
    app_password: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""

    notes_repo: str = ""          # "owner/repo"
    notes_branch: str = "main"
    github_token: str = ""
    local_data_dir: str = DEFAULT_LOCAL_DIR

    google_service_account: Any = None
    google_calendar_id: str = ""
    timezone: str = DEFAULT_TIMEZONE

    # Populated by load(), used by the Setup screen.
    problems: list[str] = field(default_factory=list)

    # -- derived ------------------------------------------------------------
    @property
    def repo_owner(self) -> str:
        return self.notes_repo.split("/")[0] if "/" in self.notes_repo else ""

    @property
    def repo_name(self) -> str:
        return self.notes_repo.split("/", 1)[1] if "/" in self.notes_repo else ""

    @property
    def uses_github(self) -> bool:
        return bool(self.repo_owner and self.repo_name and self.github_token)

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_calendar(self) -> bool:
        return bool(self.google_service_account and self.google_calendar_id)

    @property
    def models(self) -> tuple[str, ...]:
        from .extract import DEFAULT_MODELS

        if self.gemini_model:
            # Configured model first, stock ones after as a safety net.
            return (self.gemini_model,) + tuple(
                m for m in DEFAULT_MODELS if m != self.gemini_model
            )
        return DEFAULT_MODELS


def load_settings() -> Settings:
    settings = Settings(
        app_password=str(setting("app_password") or ""),
        gemini_api_key=str(setting("gemini_api_key") or ""),
        gemini_model=str(setting("gemini_model") or ""),
        notes_repo=str(setting("notes_repo") or "").strip(),
        notes_branch=str(setting("notes_branch", "main") or "main").strip(),
        github_token=str(setting("github_token") or ""),
        local_data_dir=str(setting("local_data_dir", DEFAULT_LOCAL_DIR) or DEFAULT_LOCAL_DIR),
        google_service_account=setting("google_service_account", None),
        google_calendar_id=str(setting("google_calendar_id") or "").strip(),
        timezone=str(setting("timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE),
    )

    if settings.notes_repo and "/" not in settings.notes_repo:
        settings.problems.append(
            "notes_repo should look like 'owner/repo-name'."
        )
    if settings.notes_repo and not settings.github_token:
        settings.problems.append(
            "notes_repo is set but github_token is missing, so saving would fail. "
            "Falling back to local files."
        )
    if settings.google_calendar_id and not settings.google_service_account:
        settings.problems.append(
            "google_calendar_id is set but google_service_account is missing."
        )
    return settings


def build_backend(settings: Settings) -> Backend:
    """The private repo if it is configured, otherwise a local folder."""
    if settings.uses_github:
        return GitHubBackend(
            owner=settings.repo_owner,
            repo=settings.repo_name,
            token=settings.github_token,
            branch=settings.notes_branch,
        )
    Path(settings.local_data_dir).mkdir(parents=True, exist_ok=True)
    return LocalBackend(settings.local_data_dir)


def build_store(settings: Settings) -> TaskStore:
    return TaskStore(build_backend(settings))


def build_calendar_sync(settings: Settings) -> Optional[CalendarSync]:
    return build_calendar(
        settings.google_service_account, settings.google_calendar_id, settings.timezone
    )

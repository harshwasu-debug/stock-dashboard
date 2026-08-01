"""
backends.py - where the bytes physically live.

Two implementations behind one tiny interface:

  LocalBackend   a folder on disk. Used for local development, and as an
                 automatic fallback so the app still runs before you have
                 created the private repo.
  GitHubBackend  a private GitHub repo, via the Contents API. This is the
                 real one: free, private, and every capture becomes a commit,
                 so you get full history and can recover anything.

The interface is deliberately three methods. Everything that knows what a
Task is lives in store.py, one layer up.

Concurrency: writes are optimistic. Every read hands back a `version` (a git
blob SHA, or a content hash locally) and every write must present the version
it is replacing. If someone else changed the file in between, the write raises
ConflictError instead of silently clobbering. store.py retries.
"""

from __future__ import annotations

import base64
import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

GITHUB_API = "https://api.github.com"

# GitHub's Contents API refuses to inline files above ~1MB; above that it
# returns metadata with an empty body and we have to go via the blobs API.
_CONTENTS_INLINE_LIMIT = 1_000_000


class BackendError(RuntimeError):
    """Storage failed in a way the user needs to know about."""


class ConflictError(BackendError):
    """The file changed underneath us; re-read and try again."""


class Backend(ABC):
    """Read, write, and list byte blobs addressed by a repo-relative path."""

    @abstractmethod
    def read(self, path: str) -> tuple[Optional[bytes], Optional[str]]:
        """Return (data, version). (None, None) if the path does not exist."""

    @abstractmethod
    def write(
        self, path: str, data: bytes, version: Optional[str], message: str
    ) -> str:
        """
        Write data, expecting the file to currently be at `version`
        (None meaning "I believe this file does not exist yet").
        Returns the new version. Raises ConflictError on a version mismatch.
        """

    @abstractmethod
    def list(self, prefix: str) -> list[str]:
        """Paths of files directly inside `prefix` (not recursive)."""

    @property
    def describe(self) -> str:
        """One line for the Setup screen."""
        return self.__class__.__name__


# ---------------------------------------------------------------------------
# Local folder
# ---------------------------------------------------------------------------
class LocalBackend(Backend):
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _full(self, path: str) -> Path:
        # Guard against a path escaping the data directory.
        target = (self.root / path).resolve()
        root = self.root.resolve()
        if root != target and root not in target.parents:
            raise BackendError(f"Refusing to touch a path outside the data dir: {path}")
        return target

    @staticmethod
    def _version(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:16]

    def read(self, path: str) -> tuple[Optional[bytes], Optional[str]]:
        target = self._full(path)
        if not target.is_file():
            return None, None
        data = target.read_bytes()
        return data, self._version(data)

    def write(
        self, path: str, data: bytes, version: Optional[str], message: str
    ) -> str:
        target = self._full(path)
        current, current_version = (None, None)
        if target.is_file():
            current = target.read_bytes()
            current_version = self._version(current)

        if current_version != version:
            raise ConflictError(
                f"{path} changed on disk (expected {version}, found {current_version})"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return self._version(data)

    def list(self, prefix: str) -> list[str]:
        directory = self._full(prefix)
        if not directory.is_dir():
            return []
        return sorted(
            f"{prefix}/{item.name}" for item in directory.iterdir() if item.is_file()
        )

    @property
    def describe(self) -> str:
        return f"Local folder: {self.root}"


# ---------------------------------------------------------------------------
# Private GitHub repo
# ---------------------------------------------------------------------------
class GitHubBackend(Backend):
    """
    Talks to the GitHub Contents API. Needs a fine-grained personal access
    token scoped to just the notes repo, with Contents: read and write.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        branch: str = "main",
        author_name: str = "assistant",
        author_email: str = "assistant@users.noreply.github.com",
        timeout: int = 20,
    ):
        if not (owner and repo and token):
            raise BackendError("GitHub backend needs an owner, a repo and a token.")
        self.owner = owner
        self.repo = repo
        self.token = token
        self.branch = branch
        self.author_name = author_name
        self.author_email = author_email
        self.timeout = timeout

    # -- plumbing -----------------------------------------------------------
    def _session(self):
        import requests  # imported lazily so tests can run without network libs

        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        return session

    def _url(self, path: str) -> str:
        clean = path.strip("/")
        return f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{clean}"

    # -- interface ----------------------------------------------------------
    def read(self, path: str) -> tuple[Optional[bytes], Optional[str]]:
        session = self._session()
        response = session.get(
            self._url(path), params={"ref": self.branch}, timeout=self.timeout
        )
        if response.status_code == 404:
            return None, None
        if response.status_code != 200:
            raise BackendError(
                f"GitHub read of {path} failed: {response.status_code} {response.text[:200]}"
            )

        payload = response.json()
        if isinstance(payload, list):
            raise BackendError(f"{path} is a directory, not a file.")

        sha = payload.get("sha")
        encoded = payload.get("content") or ""

        # Large file: Contents API leaves content empty, fetch the blob itself.
        if not encoded and int(payload.get("size") or 0) > _CONTENTS_INLINE_LIMIT:
            blob = session.get(
                f"{GITHUB_API}/repos/{self.owner}/{self.repo}/git/blobs/{sha}",
                timeout=self.timeout,
            )
            if blob.status_code != 200:
                raise BackendError(f"GitHub blob fetch for {path} failed.")
            encoded = blob.json().get("content") or ""

        try:
            data = base64.b64decode(encoded)
        except Exception as exc:  # pragma: no cover - malformed API response
            raise BackendError(f"Could not decode {path}: {exc}") from exc
        return data, sha

    def write(
        self, path: str, data: bytes, version: Optional[str], message: str
    ) -> str:
        session = self._session()
        body: dict = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
            "committer": {"name": self.author_name, "email": self.author_email},
        }
        if version:
            body["sha"] = version

        response = session.put(self._url(path), json=body, timeout=self.timeout)

        # 409 = branch moved under us. 422 = the sha we sent is not current
        # (which includes "file already exists" when we sent no sha at all).
        if response.status_code in (409, 422):
            raise ConflictError(
                f"{path} changed in GitHub: {response.status_code} {response.text[:200]}"
            )
        if response.status_code not in (200, 201):
            raise BackendError(
                f"GitHub write of {path} failed: {response.status_code} {response.text[:200]}"
            )

        return response.json()["content"]["sha"]

    def list(self, prefix: str) -> list[str]:
        session = self._session()
        response = session.get(
            self._url(prefix), params={"ref": self.branch}, timeout=self.timeout
        )
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise BackendError(
                f"GitHub list of {prefix} failed: {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return sorted(
            item["path"] for item in payload if item.get("type") == "file"
        )

    def check(self) -> str:
        """Cheap connectivity probe for the Setup screen."""
        import requests

        session = self._session()
        try:
            response = session.get(
                f"{GITHUB_API}/repos/{self.owner}/{self.repo}", timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise BackendError(f"Could not reach GitHub: {exc}") from exc

        if response.status_code == 404:
            raise BackendError(
                f"{self.owner}/{self.repo} not found. Either the name is wrong, or "
                "the token's 'Only select repositories' list does not include it. "
                "(A private repo the token cannot see looks exactly like a missing "
                "one - GitHub does this on purpose.)"
            )
        if response.status_code == 401:
            raise BackendError(
                "GitHub rejected the token (401). It is wrong, revoked, or expired. "
                "Generate a new one and update the app's secrets."
            )
        if response.status_code == 403:
            # 403 is NOT necessarily the token. It is also what a corporate
            # proxy, a sandbox, or an org policy returns. Quoting GitHub's own
            # message beats guessing, so don't paraphrase it away.
            raise BackendError(
                f"GitHub returned 403 (forbidden): {_message_of(response)} "
                "If the token authenticates elsewhere, suspect a network policy "
                "between this app and GitHub rather than the token itself."
            )
        if response.status_code != 200:
            raise BackendError(
                f"GitHub returned {response.status_code}: {_message_of(response)}"
            )

        info = response.json()
        if not info.get("private"):
            return (
                f"Connected to {self.owner}/{self.repo} - WARNING: this repo is "
                "PUBLIC. Your notes would be world-readable. Make it private."
            )
        return f"Connected to {self.owner}/{self.repo} (private)."

    @property
    def describe(self) -> str:
        return f"GitHub: {self.owner}/{self.repo}@{self.branch}"


def _message_of(response) -> str:
    """
    GitHub puts a human-readable reason in {"message": ...}. Surface it rather
    than a bare status code - the difference between "bad credentials" and
    "access blocked by policy" is the difference between a two-minute fix and
    an hour of looking in the wrong place.
    """
    try:
        message = response.json().get("message")
    except Exception:
        message = None
    return str(message or response.text[:200] or "no detail given").strip()


def json_bytes(payload) -> bytes:
    """One place for how we serialise JSON, so diffs in git stay readable."""
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

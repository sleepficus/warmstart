"""Thin subprocess wrapper around an installed Git.

Deliberately thin: WarmStart reads history, never writes it. Nothing here
commits, checks out, fetches, or mutates a repository in any way, so a bug in
this module cannot damage a user's work.

Git is optional. Every function degrades to a documented empty result when Git
is absent or the directory is not a repository, because the core workflow is
required to run without it.
"""

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: Commands are given a hard timeout: history in a large repository is not
#: worth blocking an inspection run indefinitely.
_TIMEOUT = 30


class GitUnavailable(Exception):
    """Raised only by callers that require Git; the detectors never do."""


@lru_cache(maxsize=1)
def git_available() -> bool:
    """Whether a usable `git` executable is on PATH."""
    try:
        completed = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _run(path: Path, *args: str) -> str | None:
    """Run a read-only git command, returning stdout or None on any failure."""
    if not git_available():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def is_repository(path: Path) -> bool:
    output = _run(path, "rev-parse", "--is-inside-work-tree")
    return output is not None and output.strip() == "true"


def current_revision(path: Path) -> str | None:
    """The current HEAD, or None when it cannot be determined."""
    output = _run(path, "rev-parse", "HEAD")
    return output.strip() if output else None


def is_dirty(path: Path) -> bool:
    """Whether the working tree has uncommitted changes.

    Evidence captured from a dirty tree cannot be reproduced from its revision,
    which is why `CommandEvidence` records this alongside the revision.
    """
    output = _run(path, "status", "--porcelain")
    return bool(output and output.strip())


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


def log(path: Path, limit: int = 500, file_path: str | None = None) -> list[Commit]:
    """Recent commits, newest first."""
    args = ["log", f"--max-count={limit}", "--format=%H%x1f%s"]
    if file_path:
        args += ["--", file_path]
    output = _run(path, *args)
    if not output:
        return []

    commits: list[Commit] = []
    for line in output.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        commits.append(Commit(sha=sha, subject=subject))
    return commits


def revert_commits(path: Path, limit: int = 500) -> list[Commit]:
    """Commits that undo earlier work.

    A revert is a recorded moment where a change turned out to be wrong, which
    is a stronger signal about a file than its churn count.
    """
    return [
        commit
        for commit in log(path, limit=limit)
        if commit.subject.lower().startswith(("revert ", 'revert "'))
    ]


def churn(path: Path, limit: int = 500) -> dict[str, int]:
    """Commit count per file over recent history."""
    output = _run(path, "log", f"--max-count={limit}", "--name-only", "--format=")
    if not output:
        return {}

    counts: dict[str, int] = {}
    for line in output.splitlines():
        name = line.strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def reverted_paths(path: Path, limit: int = 500) -> dict[str, int]:
    """Files touched by revert commits, and how often."""
    counts: dict[str, int] = {}
    for commit in revert_commits(path, limit=limit):
        output = _run(path, "show", "--name-only", "--format=", commit.sha)
        if not output:
            continue
        for line in output.splitlines():
            name = line.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts

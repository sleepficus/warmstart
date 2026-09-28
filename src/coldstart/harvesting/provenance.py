"""Access-controlled, retention-limited private provenance.

Raw session excerpts live here and nowhere else. Three properties are enforced
rather than documented:

- the store is owner-readable only (0600), and its directory 0700;
- records older than the source's retention window are dropped on every write;
- the store is never read by publication.
"""

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coldstart.domain import SessionEvent
from coldstart.storage import load_state, save_state, state_dir

PRIVATE_DIRNAME = "private"
PROVENANCE_FILENAME = "provenance.yaml"

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def provenance_path(repo_path: Path, state_override: Path | None = None) -> Path:
    return state_dir(repo_path, state_override) / PRIVATE_DIRNAME / PROVENANCE_FILENAME


def _secure(path: Path) -> None:
    """Restrict the store and its directory to the owner."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, _DIR_MODE)
    if path.exists():
        os.chmod(path, _FILE_MODE)


def is_secure(repo_path: Path, state_override: Path | None = None) -> bool:
    """Report whether the store's permissions are still owner-only."""
    path = provenance_path(repo_path, state_override)
    if not path.exists():
        return True
    file_mode = stat.S_IMODE(path.stat().st_mode)
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    return file_mode == _FILE_MODE and dir_mode == _DIR_MODE


def load_events(repo_path: Path, state_override: Path | None = None) -> list[SessionEvent]:
    path = provenance_path(repo_path, state_override)
    if not path.exists():
        return []
    return load_state(SessionEvent, path)


def apply_retention(
    events: list[SessionEvent], retention_days: int, now: datetime | None = None
) -> tuple[list[SessionEvent], list[SessionEvent]]:
    """Split events into those still within retention and those expired."""
    if retention_days <= 0:
        return events, []

    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=retention_days)

    kept: list[SessionEvent] = []
    expired: list[SessionEvent] = []
    for event in events:
        captured = event.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        (kept if captured >= cutoff else expired).append(event)
    return kept, expired


def store_events(
    repo_path: Path,
    new_events: list[SessionEvent],
    retention_days: int,
    now: datetime | None = None,
    state_override: Path | None = None,
) -> tuple[list[SessionEvent], list[SessionEvent]]:
    """Merge events into the private store, applying retention.

    Returns (added, expired). Re-harvesting the same transcript adds nothing,
    because event ids are derived from their content.
    """
    path = provenance_path(repo_path, state_override)
    _secure(path)

    existing = load_events(repo_path, state_override)
    known = {event.id for event in existing}
    added = [event for event in new_events if event.id not in known]

    merged = existing + added
    kept, expired = apply_retention(merged, retention_days, now=now)

    save_state(sorted(kept, key=lambda e: e.id), path)
    os.chmod(path, _FILE_MODE)

    return added, expired


def purge(repo_path: Path, state_override: Path | None = None) -> int:
    """Delete the private store entirely. Returns the number of records removed."""
    path = provenance_path(repo_path, state_override)
    if not path.exists():
        return 0
    count = len(load_events(repo_path, state_override))
    path.unlink()
    return count

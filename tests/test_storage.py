"""Tests for atomic storage."""

import tempfile
from pathlib import Path

from coldstart.domain import Candidate, KnowledgeState, OperationalEffect, Scope
from coldstart.storage import load_state, save_state


def test_atomic_save_and_load() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "candidates.yaml"

        candidate = Candidate(
            id="TEST-001",
            claim="Test claim",
            status=KnowledgeState.CANDIDATE,
            kind="invariant",
            confidence="high",
            derived_from=["OBS-1"],
            evidence=[{"kind": "observation", "source": "test.py"}],
            operational_effect=OperationalEffect(changes="x", avoids="y", severity="high"),
            scope=Scope(paths=["src/"], components=["core"], environments=[], authorities=[]),
            depends_on=[],
            distribution="internal",
        )

        save_state([candidate], path)
        assert path.exists()

        loaded = load_state(Candidate, path)
        assert len(loaded) == 1
        assert loaded[0].id == "TEST-001"
        assert loaded[0].status == KnowledgeState.CANDIDATE


def test_save_creates_parent_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "deep" / "nested" / "data.yaml"
        save_state([{"key": "value"}], path)
        assert path.exists()


def test_load_nonexistent_returns_empty() -> None:
    result = load_state(Candidate, Path("/nonexistent/path.yaml"))
    assert result == []

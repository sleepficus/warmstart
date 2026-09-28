"""Tests for staleness verification."""

import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from coldstart.domain import (
    Candidate,
    FileDependency,
    KnowledgeState,
    OperationalEffect,
    Scope,
    TransitionRecord,
)
from coldstart.staleness import verify_staleness
from coldstart.storage import load_state, save_state


def test_verify_staleness_file_deleted() -> None:
    """Deleting a dependency file quarantines an APPROVED candidate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        test_file = path / "test_file.txt"
        test_file.write_text("content")

        candidate = Candidate(
            id="TEST-01",
            claim="Test",
            status=KnowledgeState.APPROVED,
            kind="invariant",
            confidence="high",
            derived_from=["OBS-1"],
            evidence=[{"kind": "observation", "source": "test.py"}],
            operational_effect=OperationalEffect(changes="x", avoids="y", severity="z"),
            scope=Scope(paths=[], environments=[], authorities=[]),
            depends_on=[FileDependency(kind="file", path="test_file.txt")],
            distribution="internal",
            transition_history=[
                TransitionRecord(
                    from_state=KnowledgeState.CANDIDATE,
                    to_state=KnowledgeState.APPROVED,
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    reason="ok",
                )
            ],
        )
        candidates_path = path / ".coldstart" / "candidates.yaml"
        candidates_path.parent.mkdir()
        save_state([candidate], candidates_path)

        # File exists → no quarantine
        verify_staleness(path)
        loaded = load_state(Candidate, candidates_path)
        assert loaded[0].status == KnowledgeState.APPROVED

        # Delete file → quarantined
        test_file.unlink()
        verify_staleness(path)
        loaded = load_state(Candidate, candidates_path)
        assert loaded[0].status == KnowledgeState.QUARANTINED


def test_verify_staleness_hash_changed() -> None:
    """Changing a dependency file's content quarantines an APPROVED candidate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        test_file = path / "data.py"
        test_file.write_text("original content")
        original_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()

        candidate = Candidate(
            id="HASH-01",
            claim="Hash test",
            status=KnowledgeState.APPROVED,
            kind="invariant",
            confidence="high",
            derived_from=[],
            evidence=[],
            operational_effect=OperationalEffect(changes="x", avoids="y", severity="z"),
            scope=Scope(paths=[], environments=[], authorities=[]),
            depends_on=[FileDependency(kind="file", path="data.py", hash=original_hash)],
            distribution="internal",
            transition_history=[
                TransitionRecord(
                    from_state=KnowledgeState.CANDIDATE,
                    to_state=KnowledgeState.APPROVED,
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    reason="ok",
                )
            ],
        )
        candidates_path = path / ".coldstart" / "candidates.yaml"
        candidates_path.parent.mkdir()
        save_state([candidate], candidates_path)

        # Same content → no quarantine
        verify_staleness(path)
        loaded = load_state(Candidate, candidates_path)
        assert loaded[0].status == KnowledgeState.APPROVED

        # Change content → quarantined
        test_file.write_text("modified content")
        verify_staleness(path)
        loaded = load_state(Candidate, candidates_path)
        assert loaded[0].status == KnowledgeState.QUARANTINED


def test_candidate_goes_to_needs_verification() -> None:
    """CANDIDATE status goes to NEEDS_VERIFICATION (not QUARANTINED) on staleness."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        test_file = path / "dep.py"
        test_file.write_text("content")

        candidate = Candidate(
            id="NV-01",
            claim="Needs verification test",
            status=KnowledgeState.CANDIDATE,
            kind="invariant",
            confidence="high",
            derived_from=[],
            evidence=[],
            operational_effect=OperationalEffect(changes="x", avoids="y", severity="z"),
            scope=Scope(paths=[], environments=[], authorities=[]),
            depends_on=[FileDependency(kind="file", path="dep.py")],
            distribution="internal",
        )
        candidates_path = path / ".coldstart" / "candidates.yaml"
        candidates_path.parent.mkdir()
        save_state([candidate], candidates_path)

        # Delete → needs-verification (not quarantined)
        test_file.unlink()
        verify_staleness(path)
        loaded = load_state(Candidate, candidates_path)
        assert loaded[0].status == KnowledgeState.NEEDS_VERIFICATION

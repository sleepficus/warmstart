"""Tests for publication (basic behavior).

Comprehensive security and boundary tests are in test_publication_security.py.
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from coldstart.domain import (
    ApprovalRecord,
    Candidate,
    KnowledgeState,
    OperationalEffect,
    Scope,
    TransitionRecord,
)
from coldstart.publication import publish_candidates
from coldstart.storage import save_state


def test_publish_candidates_basic() -> None:
    """Basic publication: approved candidate appears in AGENTS.md."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        path = repo_path / "AGENTS.md"
        path.write_text("# Existing doc\n")

        candidate = Candidate(
            id="CS-001",
            claim="Test claim",
            status=KnowledgeState.APPROVED,
            kind="invariant",
            confidence="high",
            derived_from=[],
            evidence=[],
            operational_effect=OperationalEffect(changes="x", avoids="y", severity="z"),
            scope=Scope(paths=[], environments=[], authorities=[]),
            depends_on=[],
            distribution="internal",
            transition_history=[
                TransitionRecord(
                    from_state=KnowledgeState.CANDIDATE,
                    to_state=KnowledgeState.APPROVED,
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    reason="test",
                )
            ],
        )

        approval = ApprovalRecord(
            candidate_id="CS-001",
            approver="test",
            approved_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            reason="Test",
            scope_at_approval=Scope(paths=[], environments=[], authorities=[]),
            distribution_at_approval="internal",
        )

        candidates_path = repo_path / ".coldstart" / "candidates.yaml"
        candidates_path.parent.mkdir()
        save_state([candidate], candidates_path)

        approvals_path = repo_path / ".coldstart" / "approvals.yaml"
        save_state([approval], approvals_path)

        publish_candidates(repo_path)

        content = path.read_text()
        assert "<!-- warmstart:start -->" in content
        assert "Test claim" in content
        assert "# Existing doc" in content

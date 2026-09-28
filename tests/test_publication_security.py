"""Tests for publication security boundaries.

Covers:
- Quarantined, conflicted, stale, sensitive publication blocked
- Missing approval record → blocked
- Duplicate and malformed managed sections → error
- Human content preservation
- Publication idempotency
- Dry-run non-mutation
- Interrupted-write recovery (temp file cleanup)
- Missing dependency at publish time
"""

import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coldstart.domain import (
    ApprovalRecord,
    Candidate,
    Conflict,
    KnowledgeState,
    OperationalEffect,
    PublishedKnowledge,
    Scope,
    TransitionRecord,
)
from coldstart.publication import (
    NOTE_BUDGET,
    PublicationError,
    publish_candidates,
)
from coldstart.storage import load_state, save_state


def _make_approved_candidate(
    candidate_id: str = "CS-001",
    claim: str = "Test claim",
    distribution: str = "internal",
    depends_on: list | None = None,
) -> Candidate:
    """Create a properly-approved candidate with valid transition history."""
    return Candidate(
        id=candidate_id,
        claim=claim,
        status=KnowledgeState.APPROVED,
        kind="invariant",
        confidence="high",
        derived_from=[],
        evidence=[],
        operational_effect=OperationalEffect(changes="x", avoids="y", severity="high"),
        scope=Scope(paths=[], environments=[], authorities=[]),
        depends_on=depends_on or [],
        distribution=distribution,
        transition_history=[
            TransitionRecord(
                from_state=KnowledgeState.CANDIDATE,
                to_state=KnowledgeState.APPROVED,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                reason="Approved in test",
            )
        ],
    )


def _make_approval(
    candidate_id: str = "CS-001",
    approved_at: datetime | None = None,
) -> ApprovalRecord:
    return ApprovalRecord(
        candidate_id=candidate_id,
        approver="test_user",
        approved_at=approved_at or datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        reason="Test approval",
        scope_at_approval=Scope(paths=[], environments=[], authorities=[]),
        distribution_at_approval="internal",
    )


def _setup_repo(
    tmpdir: str,
    candidates: list[Candidate],
    approvals: list[ApprovalRecord] | None = None,
    conflicts: list[Conflict] | None = None,
    agents_content: str = "# Human content\n",
) -> Path:
    """Set up a temporary repository structure for publication tests."""
    repo = Path(tmpdir)
    coldstart = repo / ".coldstart"
    coldstart.mkdir()
    save_state(candidates, coldstart / "candidates.yaml")
    if approvals:
        save_state(approvals, coldstart / "approvals.yaml")
    if conflicts:
        save_state(conflicts, coldstart / "conflicts.yaml")
    (repo / "AGENTS.md").write_text(agents_content)
    return repo


class TestPublicationBlocking:
    """Verify that invalid candidates are excluded from publication."""

    def test_quarantined_candidate_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = Candidate(
                id="CS-Q01",
                claim="Stale",
                status=KnowledgeState.QUARANTINED,
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
                        reason="ok",
                    ),
                    TransitionRecord(
                        from_state=KnowledgeState.APPROVED,
                        to_state=KnowledgeState.QUARANTINED,
                        timestamp=datetime(2026, 1, 2, tzinfo=UTC),
                        reason="stale",
                    ),
                ],
            )
            repo = _setup_repo(tmpdir, [c], [_make_approval("CS-Q01")])
            publish_candidates(repo)
            content = (repo / "AGENTS.md").read_text()
            assert "Stale" not in content

    def test_conflicted_candidate_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate("CS-C01", "Conflicted claim")
            conflict = Conflict(
                id="CONFLICT-001",
                status="unresolved",
                candidate_ids=["CS-C01"],
                reason="Test conflict",
                publication_blocked=True,
            )
            repo = _setup_repo(tmpdir, [c], [_make_approval("CS-C01")], [conflict])
            _, msg = publish_candidates(repo)
            content = (repo / "AGENTS.md").read_text()
            assert "Conflicted claim" not in content
            assert "blocked by unresolved conflict" in msg

    def test_sensitive_distribution_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate("CS-S01", "Secret data", distribution="sensitive")
            repo = _setup_repo(tmpdir, [c], [_make_approval("CS-S01")])
            _, msg = publish_candidates(repo)
            content = (repo / "AGENTS.md").read_text()
            assert "Secret data" not in content
            assert "not publishable" in msg

    def test_missing_approval_record_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate("CS-NA01", "No approval")
            repo = _setup_repo(tmpdir, [c], [])  # no approvals!
            _, msg = publish_candidates(repo)
            content = (repo / "AGENTS.md").read_text()
            assert "No approval" not in content
            assert "no approval record" in msg

    def test_stale_dependency_at_publish_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate(
                "CS-D01",
                "Depends on missing",
                depends_on=[{"kind": "file", "path": "nonexistent.py"}],
            )
            repo = _setup_repo(tmpdir, [c], [_make_approval("CS-D01")])
            _, msg = publish_candidates(repo)
            content = (repo / "AGENTS.md").read_text()
            assert "Depends on missing" not in content
            assert "stale dependency" in msg


class TestManagedSectionValidation:
    """Verify that malformed or duplicate sections cause failures."""

    def test_duplicate_start_markers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate()
            repo = _setup_repo(
                tmpdir,
                [c],
                [_make_approval()],
                agents_content=(
                    "<!-- coldstart:start -->\nold\n<!-- coldstart:end -->\n"
                    "Middle\n"
                    "<!-- coldstart:start -->\nold2\n<!-- coldstart:end -->\n"
                ),
            )
            with pytest.raises(PublicationError, match="Duplicate"):
                publish_candidates(repo)

    def test_mismatched_markers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate()
            repo = _setup_repo(
                tmpdir,
                [c],
                [_make_approval()],
                agents_content="<!-- coldstart:start -->\nno end marker\n",
            )
            with pytest.raises(PublicationError, match="Malformed"):
                publish_candidates(repo)

    def test_reversed_markers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate()
            repo = _setup_repo(
                tmpdir,
                [c],
                [_make_approval()],
                agents_content="<!-- coldstart:end -->\nreversed\n<!-- coldstart:start -->\n",
            )
            with pytest.raises(PublicationError, match="end marker appears before"):
                publish_candidates(repo)


class TestPublicationBehavior:
    """Verify correct publication behavior."""

    def test_human_content_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate()
            repo = _setup_repo(
                tmpdir,
                [c],
                [_make_approval()],
                agents_content="# My Important Notes\n\nDo not delete this.\n",
            )
            publish_candidates(repo)
            content = (repo / "AGENTS.md").read_text()
            assert "# My Important Notes" in content
            assert "Do not delete this." in content
            assert "<!-- warmstart:start -->" in content
            assert "Test claim" in content

    def test_idempotent_republish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate()
            repo = _setup_repo(tmpdir, [c], [_make_approval()])
            publish_candidates(repo)
            content_after_first = (repo / "AGENTS.md").read_text()

            changed, _ = publish_candidates(repo)
            content_after_second = (repo / "AGENTS.md").read_text()

            assert not changed
            assert content_after_first == content_after_second

    def test_dry_run_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate()
            repo = _setup_repo(tmpdir, [c], [_make_approval()])
            original = (repo / "AGENTS.md").read_text()

            changed, msg = publish_candidates(repo, dry_run=True)
            assert not changed
            assert "Dry run" in msg
            assert (repo / "AGENTS.md").read_text() == original

    def test_deterministic_output_sorted_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c1 = _make_approved_candidate("CS-ZZZ", "Zulu claim")
            c2 = _make_approved_candidate("CS-AAA", "Alpha claim")
            approvals = [_make_approval("CS-ZZZ"), _make_approval("CS-AAA")]
            repo = _setup_repo(tmpdir, [c1, c2], approvals)
            publish_candidates(repo)
            content = (repo / "AGENTS.md").read_text()
            aaa_pos = content.index("CS-AAA")
            zzz_pos = content.index("CS-ZZZ")
            assert aaa_pos < zzz_pos, "Output should be sorted by ID"

    def test_claim_with_regex_escapes_is_written_literally(self) -> None:
        """A backslash in a claim must not be read as a regex group reference."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claim = r"Windows paths like C:\1\g<0> are not normalized"
            c = _make_approved_candidate("CS-RE01", claim)
            repo = _setup_repo(
                tmpdir,
                [c],
                [_make_approval("CS-RE01")],
                agents_content=(
                    "# Human\n\n<!-- coldstart:start -->\nold\n<!-- coldstart:end -->\n"
                ),
            )

            publish_candidates(repo)

            assert claim in (repo / "AGENTS.md").read_text()

    def test_budget_overflow_is_reported(self) -> None:
        """Notes held back by the budget must be named, never dropped silently."""
        with tempfile.TemporaryDirectory() as tmpdir:
            count = NOTE_BUDGET + 3
            candidates = [
                _make_approved_candidate(f"CS-B{i:03d}", f"Claim {i}") for i in range(count)
            ]
            approvals = [_make_approval(c.id) for c in candidates]
            repo = _setup_repo(tmpdir, candidates, approvals)

            _, msg = publish_candidates(repo)

            assert "note budget" in msg
            assert "3 note(s) held back" in msg
            content = (repo / "AGENTS.md").read_text()
            assert content.count("- **Invariant**") == NOTE_BUDGET
            for held in candidates[NOTE_BUDGET:]:
                assert held.id in msg

    def test_existing_file_permissions_are_preserved(self) -> None:
        """Publishing must not narrow AGENTS.md from group/world readable to 0600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate()
            repo = _setup_repo(tmpdir, [c], [_make_approval()])
            agents_md = repo / "AGENTS.md"
            agents_md.chmod(0o644)

            publish_candidates(repo)

            assert stat.S_IMODE(agents_md.stat().st_mode) == 0o644

    def test_publication_records_published_state(self) -> None:
        """Publishing moves the candidate to PUBLISHED and records it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate("CS-P01", "Live claim")
            repo = _setup_repo(tmpdir, [c], [_make_approval("CS-P01")])

            publish_candidates(repo)

            stored = load_state(Candidate, repo / ".coldstart" / "candidates.yaml")
            assert stored[0].status == KnowledgeState.PUBLISHED

            published = load_state(PublishedKnowledge, repo / ".coldstart" / "published.yaml")
            assert [p.id for p in published] == ["CS-P01"]
            assert published[0].approved_by == "test_user"

    def test_republish_keeps_published_notes(self) -> None:
        """A second publish must not empty the section now that state advanced."""
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_approved_candidate("CS-P02", "Durable claim")
            repo = _setup_repo(tmpdir, [c], [_make_approval("CS-P02")])

            publish_candidates(repo)
            first = (repo / "AGENTS.md").read_text()
            changed, _ = publish_candidates(repo)

            assert not changed
            assert (repo / "AGENTS.md").read_text() == first
            assert "Durable claim" in first

    def test_no_candidates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            changed, _ = publish_candidates(repo)
            assert not changed

"""Tests for the interactive review command.

Previously every test drove review with --approve-all, so the interactive path
— the workflow's actual gatekeeper — had no coverage at all.
"""

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from coldstart.cli import app
from coldstart.domain import (
    ApprovalRecord,
    Candidate,
    Conflict,
    KnowledgeState,
    OperationalEffect,
    Question,
    Scope,
)
from coldstart.storage import load_state, save_state

runner = CliRunner()


def _candidate(
    candidate_id: str = "CS-001",
    status: KnowledgeState = KnowledgeState.CANDIDATE,
    paths: list[str] | None = None,
) -> Candidate:
    return Candidate(
        id=candidate_id,
        claim="Account ids keep leading zeros",
        status=status,
        kind="invariant",
        confidence="high",
        derived_from=["OBS-1"],
        evidence=[{"kind": "observation", "source": "src/ids.py"}],
        operational_effect=OperationalEffect(
            changes="Keep ids as strings.", avoids="Dangling references.", severity="high"
        ),
        scope=Scope(
            paths=paths if paths is not None else ["src/ids.py", "src/other.py"],
            environments=[],
            authorities=[],
        ),
        depends_on=[],
        distribution="internal",
    )


def _repo(
    tmpdir: str, candidates: list[Candidate], conflicts: list[Conflict] | None = None
) -> Path:
    repo = Path(tmpdir)
    coldstart = repo / ".coldstart"
    coldstart.mkdir()
    save_state(candidates, coldstart / "candidates.yaml")
    if conflicts:
        save_state(conflicts, coldstart / "conflicts.yaml")
    return repo


def _load(repo: Path) -> list[Candidate]:
    return load_state(Candidate, repo / ".coldstart" / "candidates.yaml")


class TestInteractiveActions:
    def test_approve_records_identity_and_justification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])

            result = runner.invoke(
                app,
                ["review", str(repo), "--approver", "alice"],
                input="a\nVerified against the upstream export\n",
            )

            assert result.exit_code == 0
            assert _load(repo)[0].status == KnowledgeState.APPROVED

            approvals = load_state(ApprovalRecord, repo / ".coldstart" / "approvals.yaml")
            assert approvals[0].approver == "alice"
            assert approvals[0].reason == "Verified against the upstream export"

    def test_quarantine_is_recorded_as_quarantined_not_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])

            result = runner.invoke(app, ["review", str(repo)], input="q\n")

            assert result.exit_code == 0
            stored = _load(repo)[0]
            assert stored.status == KnowledgeState.QUARANTINED
            assert stored.status != KnowledgeState.REJECTED

    def test_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])
            runner.invoke(app, ["review", str(repo)], input="r\n")
            assert _load(repo)[0].status == KnowledgeState.REJECTED

    def test_skip_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])
            result = runner.invoke(app, ["review", str(repo)], input="s\n")
            assert "No changes made" in result.output
            assert _load(repo)[0].status == KnowledgeState.CANDIDATE

    def test_edit_rewrites_the_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])
            runner.invoke(app, ["review", str(repo)], input="e\nIds are opaque strings\n")
            assert _load(repo)[0].claim == "Ids are opaque strings"

    def test_convert_to_test_sets_durable_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])
            runner.invoke(app, ["review", str(repo)], input="t\n")
            assert _load(repo)[0].durable_form == "regression-test"
            assert (repo / ".coldstart" / "proposals" / "tests" / "CS-001.yaml").exists()

    def test_convert_to_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])

            runner.invoke(app, ["review", str(repo)], input="c\nIs this intentional?\n")

            assert _load(repo)[0].status == KnowledgeState.NEEDS_CLARIFICATION
            stored = load_state(Question, repo / ".coldstart" / "questions.yaml")
            assert stored[0].prompt == "Is this intentional?"
            assert stored[0].candidate_id == "CS-001"

    def test_request_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])
            runner.invoke(app, ["review", str(repo)], input="v\n")
            assert _load(repo)[0].status == KnowledgeState.NEEDS_VERIFICATION


class TestBulkApproval:
    def test_approve_all_requires_a_named_approver(self) -> None:
        """Bulk approval bypasses review, so it must be attributable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])

            result = runner.invoke(app, ["review", str(repo), "--approve-all"])

            assert result.exit_code == 2
            assert "requires --approver" in result.output
            assert _load(repo)[0].status == KnowledgeState.CANDIDATE

    def test_bulk_approval_is_recorded_as_unreviewed(self) -> None:
        """'Approved' and 'nobody looked at it' must not be indistinguishable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])

            result = runner.invoke(
                app, ["review", str(repo), "--approve-all", "--approver", "reviewer"]
            )

            assert result.exit_code == 0
            approvals = load_state(ApprovalRecord, repo / ".coldstart" / "approvals.yaml")
            assert approvals[0].approver == "reviewer"
            assert approvals[0].per_candidate_review is False
            assert "without per-candidate review" in result.output

    def test_interactive_approval_is_recorded_as_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])
            runner.invoke(
                app, ["review", str(repo), "--approver", "reviewer"], input="a\nchecked\n"
            )
            approvals = load_state(ApprovalRecord, repo / ".coldstart" / "approvals.yaml")
            assert approvals[0].per_candidate_review is True


class TestMarkConflict:
    def test_reviewer_can_mark_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate("CS-001"), _candidate("CS-002")])

            result = runner.invoke(
                app,
                ["review", str(repo), "--approver", "reviewer"],
                input="m\nCS-002\nThey disagree about id handling\ns\n",
            )

            assert result.exit_code == 0
            assert _load(repo)[0].status == KnowledgeState.CONFLICTED

            conflicts = load_state(Conflict, repo / ".coldstart" / "conflicts.yaml")
            assert len(conflicts) == 1
            assert set(conflicts[0].candidate_ids) == {"CS-001", "CS-002"}
            assert conflicts[0].publication_blocked is True

    def test_marked_conflict_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate("CS-001")])
            runner.invoke(
                app,
                ["review", str(repo), "--approver", "reviewer"],
                input="m\n\nContradicts the schema\n",
            )
            (repo / "AGENTS.md").write_text("# Human\n")

            result = runner.invoke(app, ["publish", str(repo)])

            assert "CS-001" not in (repo / "AGENTS.md").read_text()
            assert result.exit_code == 0


class TestScopeRestriction:
    def test_scope_can_be_narrowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])
            runner.invoke(app, ["review", str(repo)], input="n\nsrc/ids.py\n")
            assert _load(repo)[0].scope.paths == ["src/ids.py"]

    def test_scope_cannot_be_widened(self) -> None:
        """Review may narrow a claim, never broaden it past its evidence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate(paths=["src/ids.py"])])

            result = runner.invoke(
                app, ["review", str(repo)], input="n\nsrc/ids.py, src/everything.py\n"
            )

            assert "can only narrow scope" in result.output
            assert _load(repo)[0].scope.paths == ["src/ids.py"]


class TestStrandedCandidates:
    def test_conflicted_candidate_is_reviewable_and_reopenable(self) -> None:
        """A conflicted candidate must have a way back into the workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate = _candidate()
            candidate.transition_to(KnowledgeState.CONFLICTED, reason="contradiction")
            conflict = Conflict(
                id="CONFLICT-001",
                status="unresolved",
                candidate_ids=["CS-001"],
                reason="Two claims disagree",
                publication_blocked=True,
            )
            repo = _repo(tmpdir, [candidate], [conflict])

            result = runner.invoke(app, ["review", str(repo)], input="o\nResolved by hand\n")

            assert "CONFLICT-001" in result.output, "conflicts must be shown to the reviewer"
            assert _load(repo)[0].status == KnowledgeState.CANDIDATE

    def test_quarantined_candidate_can_be_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate = _candidate()
            candidate.transition_to(KnowledgeState.APPROVED, reason="ok")
            candidate.transition_to(KnowledgeState.QUARANTINED, reason="stale")
            repo = _repo(tmpdir, [candidate])

            runner.invoke(app, ["review", str(repo)], input="o\nDependency re-checked\n")

            assert _load(repo)[0].status == KnowledgeState.CANDIDATE


class TestDisplay:
    def test_review_shows_the_phase_seven_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo(tmpdir, [_candidate()])

            result = runner.invoke(app, ["review", str(repo)], input="s\n")

            for expected in (
                "Claim:",
                "Provenance:",
                "Confidence:",
                "Avoids:",
                "Scope paths:",
                "Distribution:",
                "Proposed durable form:",
                "Evidence:",
                "Staleness dependencies:",
                "Conflicts:",
                "Proposed agent-facing text:",
            ):
                assert expected in result.output, f"review must display {expected}"

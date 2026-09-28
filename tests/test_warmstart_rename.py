"""WarmStart product-name and Coldstart compatibility checks."""

import tempfile
from pathlib import Path

from typer.testing import CliRunner

import warmstart
from coldstart.cli import app
from coldstart.domain import (
    ApprovalRecord,
    Candidate,
    KnowledgeState,
    OperationalEffect,
    Scope,
    TransitionRecord,
)
from coldstart.publication import PublicationError, publish_candidates
from coldstart.storage import save_state


def _approved() -> tuple[Candidate, ApprovalRecord]:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    candidate = Candidate(
        id="WS-001",
        claim="Keep identifiers as strings.",
        status=KnowledgeState.APPROVED,
        kind="invariant",
        confidence="verified",
        derived_from=[],
        evidence=[],
        operational_effect=OperationalEffect(
            changes="Parsing", avoids="Lost zeroes", severity="high"
        ),
        scope=Scope(paths=[], environments=[], authorities=[]),
        depends_on=[],
        distribution="internal",
        transition_history=[
            TransitionRecord(
                from_state=KnowledgeState.CANDIDATE,
                to_state=KnowledgeState.APPROVED,
                timestamp=now,
                reason="test",
            )
        ],
    )
    approval = ApprovalRecord(
        candidate_id=candidate.id,
        approver="test",
        approved_at=now + timedelta(seconds=1),
        reason="test",
        scope_at_approval=candidate.scope,
        distribution_at_approval=candidate.distribution,
    )
    return candidate, approval


def _state(repo: Path) -> None:
    candidate, approval = _approved()
    state = repo / ".coldstart"
    state.mkdir()
    save_state([candidate], state / "candidates.yaml")
    save_state([approval], state / "approvals.yaml")


def test_warmstart_namespace_and_cli_brand() -> None:
    assert warmstart.__version__ == "0.2.0"
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "WarmStart" in result.output


def test_new_publication_uses_warmstart_markers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _state(repo)
        publish_candidates(repo)
        content = (repo / "AGENTS.md").read_text()
        assert "<!-- warmstart:start -->" in content
        assert "<!-- coldstart:start -->" not in content


def test_legacy_markers_are_updated_in_place() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _state(repo)
        (repo / "AGENTS.md").write_text(
            "# Human\n\n<!-- coldstart:start -->\nold\n<!-- coldstart:end -->\n"
        )
        publish_candidates(repo)
        content = (repo / "AGENTS.md").read_text()
        assert "<!-- coldstart:start -->" in content
        assert "<!-- warmstart:start -->" not in content
        assert "Keep identifiers as strings." in content


def test_mixed_marker_families_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _state(repo)
        (repo / "AGENTS.md").write_text(
            "<!-- coldstart:start -->\nold\n<!-- coldstart:end -->\n"
            "<!-- warmstart:start -->\nnew\n<!-- warmstart:end -->\n"
        )
        try:
            publish_candidates(repo)
        except PublicationError as exc:
            assert "Both WarmStart and legacy Coldstart" in str(exc)
        else:
            raise AssertionError("mixed marker families must be refused")

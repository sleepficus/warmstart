"""Tests for domain model transitions (basic)."""

from coldstart.domain import Candidate, KnowledgeState, OperationalEffect, Scope


def test_candidate_transition() -> None:
    candidate = Candidate(
        id="CS-001",
        claim="Test claim",
        status=KnowledgeState.CANDIDATE,
        kind="invariant",
        confidence="high",
        derived_from=[],
        evidence=[],
        operational_effect=OperationalEffect(changes="change", avoids="avoid", severity="high"),
        scope=Scope(paths=["**"], environments=["all"], authorities=["none"]),
        depends_on=[],
        distribution="repository",
    )

    candidate.transition_to(KnowledgeState.NEEDS_VERIFICATION, "needs testing")
    assert candidate.status == KnowledgeState.NEEDS_VERIFICATION
    assert len(candidate.transition_history) == 1
    assert candidate.transition_history[0].from_state == KnowledgeState.CANDIDATE
    assert candidate.transition_history[0].to_state == KnowledgeState.NEEDS_VERIFICATION
    assert candidate.transition_history[0].reason == "needs testing"

"""Tests for the domain state machine.

Covers:
- Valid transitions
- Illegal transitions (every disallowed pair that matters)
- Deserialization consistency validation
- APPROVED status without valid transition history
- Corrupted transition history detection
"""

import pytest

from coldstart.domain import (
    Candidate,
    InvalidTransitionError,
    KnowledgeState,
    OperationalEffect,
    Scope,
)


def _make_candidate(
    status: KnowledgeState = KnowledgeState.CANDIDATE,
    transition_history: list | None = None,
) -> Candidate:
    """Helper to create a minimal valid candidate."""
    if transition_history is None:
        transition_history = []
    return Candidate(
        id="TEST-001",
        claim="Test claim",
        status=status,
        kind="invariant",
        confidence="high",
        derived_from=[],
        evidence=[],
        operational_effect=OperationalEffect(changes="x", avoids="y", severity="z"),
        scope=Scope(paths=[], environments=[], authorities=[]),
        depends_on=[],
        distribution="internal",
        transition_history=transition_history,
    )


class TestValidTransitions:
    """Verify that allowed transitions succeed and record history."""

    def test_candidate_to_approved(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="Reviewed")
        assert c.status == KnowledgeState.APPROVED
        assert len(c.transition_history) == 1
        assert c.transition_history[0].from_state == KnowledgeState.CANDIDATE
        assert c.transition_history[0].to_state == KnowledgeState.APPROVED

    def test_candidate_to_needs_verification(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.NEEDS_VERIFICATION, reason="Needs check")
        assert c.status == KnowledgeState.NEEDS_VERIFICATION

    def test_approved_to_quarantined(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="OK")
        c.transition_to(KnowledgeState.QUARANTINED, reason="Stale")
        assert c.status == KnowledgeState.QUARANTINED
        assert len(c.transition_history) == 2

    def test_approved_to_published(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="OK")
        c.transition_to(KnowledgeState.PUBLISHED, reason="Published")
        assert c.status == KnowledgeState.PUBLISHED

    def test_quarantined_to_candidate(self) -> None:
        """Quarantined candidates can be re-admitted."""
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="OK")
        c.transition_to(KnowledgeState.QUARANTINED, reason="Stale")
        c.transition_to(KnowledgeState.CANDIDATE, reason="Re-admitted")
        assert c.status == KnowledgeState.CANDIDATE

    def test_transition_history_is_cumulative(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="r1")
        c.transition_to(KnowledgeState.QUARANTINED, reason="r2")
        c.transition_to(KnowledgeState.CANDIDATE, reason="r3")
        assert len(c.transition_history) == 3
        assert [t.to_state for t in c.transition_history] == [
            KnowledgeState.APPROVED,
            KnowledgeState.QUARANTINED,
            KnowledgeState.CANDIDATE,
        ]


class TestReviewerQuarantine:
    """Quarantine is a reviewer action (Phase 7), not only a staleness outcome.

    It previously had to be recorded as a rejection, which erased the
    distinction between "this is wrong" and "this needs investigation".
    """

    def test_reviewer_can_quarantine_a_candidate(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.QUARANTINED, reason="needs investigation")
        assert c.status == KnowledgeState.QUARANTINED
        assert c.transition_history[-1].to_state == KnowledgeState.QUARANTINED

    def test_quarantined_candidate_can_be_reopened(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.QUARANTINED, reason="needs investigation")
        c.transition_to(KnowledgeState.CANDIDATE, reason="investigated")
        assert c.status == KnowledgeState.CANDIDATE


class TestIllegalTransitions:
    """Verify that disallowed transitions raise InvalidTransitionError."""

    def test_candidate_to_published_skips_approval(self) -> None:
        c = _make_candidate()
        with pytest.raises(InvalidTransitionError):
            c.transition_to(KnowledgeState.PUBLISHED, reason="skip")

    def test_candidate_to_published_direct(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="ok")
        c.transition_to(KnowledgeState.QUARANTINED, reason="needs investigation")
        with pytest.raises(InvalidTransitionError):
            c.transition_to(KnowledgeState.APPROVED, reason="skip re-review")

    def test_rejected_is_terminal(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.REJECTED, reason="bad")
        with pytest.raises(InvalidTransitionError):
            c.transition_to(KnowledgeState.CANDIDATE, reason="retry")

    def test_superseded_is_terminal(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="ok")
        c.transition_to(KnowledgeState.SUPERSEDED, reason="replaced")
        with pytest.raises(InvalidTransitionError):
            c.transition_to(KnowledgeState.CANDIDATE, reason="retry")

    def test_quarantined_to_published(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="ok")
        c.transition_to(KnowledgeState.QUARANTINED, reason="stale")
        with pytest.raises(InvalidTransitionError):
            c.transition_to(KnowledgeState.PUBLISHED, reason="escape")

    def test_approved_to_candidate_not_allowed(self) -> None:
        c = _make_candidate()
        c.transition_to(KnowledgeState.APPROVED, reason="ok")
        with pytest.raises(InvalidTransitionError):
            c.transition_to(KnowledgeState.CANDIDATE, reason="downgrade")


class TestDeserializationConsistency:
    """Verify that the model_validator catches corrupted state."""

    def test_approved_with_empty_history_rejected(self) -> None:
        """Cannot deserialize an APPROVED candidate with no transition history."""
        with pytest.raises(ValueError, match="empty transition history"):
            Candidate.model_validate(
                {
                    "id": "T-01",
                    "claim": "test",
                    "status": "approved",
                    "kind": "invariant",
                    "confidence": "high",
                    "derived_from": [],
                    "evidence": [],
                    "operational_effect": {"changes": "x", "avoids": "y", "severity": "z"},
                    "scope": {"paths": [], "environments": [], "authorities": []},
                    "depends_on": [],
                    "distribution": "internal",
                    "transition_history": [],
                }
            )

    def test_approved_with_mismatched_history_rejected(self) -> None:
        """Cannot deserialize when status doesn't match terminal transition."""
        with pytest.raises(ValueError, match="last transition record shows"):
            Candidate.model_validate(
                {
                    "id": "T-01",
                    "claim": "test",
                    "status": "approved",
                    "kind": "invariant",
                    "confidence": "high",
                    "derived_from": [],
                    "evidence": [],
                    "operational_effect": {"changes": "x", "avoids": "y", "severity": "z"},
                    "scope": {"paths": [], "environments": [], "authorities": []},
                    "depends_on": [],
                    "distribution": "internal",
                    "transition_history": [
                        {
                            "from_state": "candidate",
                            "to_state": "rejected",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "reason": "bad",
                        }
                    ],
                }
            )

    def test_candidate_with_empty_history_allowed(self) -> None:
        """CANDIDATE is an initial state and may have empty history."""
        c = Candidate.model_validate(
            {
                "id": "T-01",
                "claim": "test",
                "status": "candidate",
                "kind": "invariant",
                "confidence": "high",
                "derived_from": [],
                "evidence": [],
                "operational_effect": {"changes": "x", "avoids": "y", "severity": "z"},
                "scope": {"paths": [], "environments": [], "authorities": []},
                "depends_on": [],
                "distribution": "internal",
                "transition_history": [],
            }
        )
        assert c.status == KnowledgeState.CANDIDATE

    def test_published_without_history_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty transition history"):
            Candidate.model_validate(
                {
                    "id": "T-01",
                    "claim": "test",
                    "status": "published",
                    "kind": "invariant",
                    "confidence": "high",
                    "derived_from": [],
                    "evidence": [],
                    "operational_effect": {"changes": "x", "avoids": "y", "severity": "z"},
                    "scope": {"paths": [], "environments": [], "authorities": []},
                    "depends_on": [],
                    "distribution": "internal",
                    "transition_history": [],
                }
            )

    def test_quarantined_without_history_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty transition history"):
            Candidate.model_validate(
                {
                    "id": "T-01",
                    "claim": "test",
                    "status": "quarantined",
                    "kind": "invariant",
                    "confidence": "high",
                    "derived_from": [],
                    "evidence": [],
                    "operational_effect": {"changes": "x", "avoids": "y", "severity": "z"},
                    "scope": {"paths": [], "environments": [], "authorities": []},
                    "depends_on": [],
                    "distribution": "internal",
                    "transition_history": [],
                }
            )

"""Property tests for invariants that example-based tests sample thinly.

Two invariants are worth stating as properties rather than cases, because both
are claims about *every* sequence rather than about a few:

- a candidate's recorded status must always agree with its history, however it
  got there;
- a classification can only ever move upward.
"""

from hypothesis import given
from hypothesis import strategies as st

from coldstart.domain import (
    ALLOWED_TRANSITIONS,
    INITIAL_CANDIDATE_STATES,
    Candidate,
    InvalidTransitionError,
    KnowledgeState,
    OperationalEffect,
    Scope,
    Sensitivity,
    max_sensitivity,
)

KNOWN_SENSITIVITIES = [s.value for s in Sensitivity]


def _candidate() -> Candidate:
    return Candidate(
        id="P-1",
        claim="property",
        status=KnowledgeState.CANDIDATE,
        kind="invariant",
        confidence="high",
        derived_from=[],
        evidence=[],
        operational_effect=OperationalEffect(changes="x", avoids="y", severity="high"),
        scope=Scope(paths=[], environments=[], authorities=[]),
        depends_on=[],
        distribution="internal",
    )


@given(st.lists(st.sampled_from(list(KnowledgeState)), min_size=0, max_size=12))
def test_status_always_agrees_with_history(sequence: list[KnowledgeState]) -> None:
    """However a candidate is driven, it never ends up lying about its state.

    Illegal transitions must raise and leave the candidate untouched; legal
    ones must leave the last history entry equal to the status. Re-validating
    the serialized form proves the model would accept what we just produced.
    """
    candidate = _candidate()

    for target in sequence:
        allowed = ALLOWED_TRANSITIONS.get(candidate.status, set())
        before = candidate.status
        history_before = len(candidate.transition_history)

        if target in allowed:
            candidate.transition_to(target, reason="property")
            assert candidate.status == target
            assert candidate.transition_history[-1].to_state == target
            assert len(candidate.transition_history) == history_before + 1
        else:
            try:
                candidate.transition_to(target, reason="property")
            except InvalidTransitionError:
                assert candidate.status == before
                assert len(candidate.transition_history) == history_before
            else:  # pragma: no cover - a legal edge we failed to predict
                raise AssertionError(f"{before} → {target} should have been rejected")

    if candidate.status not in INITIAL_CANDIDATE_STATES:
        assert candidate.transition_history
        assert candidate.transition_history[-1].to_state == candidate.status

    # The model validator must accept a candidate the state machine produced.
    Candidate.model_validate(candidate.model_dump(mode="json"))


@given(st.lists(st.sampled_from(KNOWN_SENSITIVITIES), min_size=1, max_size=6))
def test_max_sensitivity_is_the_highest_input(values: list[str]) -> None:
    result = max_sensitivity(*values)
    assert result in values
    for value in values:
        assert max_sensitivity(result, value) == result


@given(
    st.lists(st.sampled_from(KNOWN_SENSITIVITIES), min_size=1, max_size=4),
    st.text(min_size=1, max_size=8).filter(lambda t: t not in KNOWN_SENSITIVITIES),
)
def test_unknown_classification_always_wins(known: list[str], unknown: str) -> None:
    """An unclassifiable input is treated as the most sensitive, never the least."""
    assert max_sensitivity(*known, unknown) == unknown


@given(st.lists(st.sampled_from(KNOWN_SENSITIVITIES), min_size=1, max_size=6))
def test_max_sensitivity_is_order_independent(values: list[str]) -> None:
    assert max_sensitivity(*values) == max_sensitivity(*reversed(values))

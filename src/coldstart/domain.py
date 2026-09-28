"""WarmStart domain models and state machine.

Trust boundary: Transition-history and approval-record validation detect
accidental state corruption. They do not prevent deliberate YAML forgery
by a user with unrestricted write access to both source and state files.
Signed approvals would be required for that guarantee.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class KnowledgeState(StrEnum):
    OBSERVED = "observed"
    CANDIDATE = "candidate"
    NEEDS_VERIFICATION = "needs-verification"
    NEEDS_CLARIFICATION = "needs-clarification"
    CONFLICTED = "conflicted"
    APPROVED = "approved"
    PUBLISHED = "published"
    STALE = "stale"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


# Valid state transitions. Terminal states (SUPERSEDED, REJECTED) have no outgoing edges.
ALLOWED_TRANSITIONS: dict[KnowledgeState, set[KnowledgeState]] = {
    KnowledgeState.OBSERVED: {KnowledgeState.CANDIDATE},
    # Quarantine is a reviewer action (Phase 7) as well as a staleness outcome,
    # so every pre-approval state can reach it. Without these edges the review
    # command has to record a quarantine as a rejection, which loses the
    # distinction between "this is wrong" and "this needs investigation".
    KnowledgeState.CANDIDATE: {
        KnowledgeState.NEEDS_VERIFICATION,
        KnowledgeState.NEEDS_CLARIFICATION,
        KnowledgeState.CONFLICTED,
        KnowledgeState.APPROVED,
        KnowledgeState.QUARANTINED,
        KnowledgeState.REJECTED,
    },
    KnowledgeState.NEEDS_VERIFICATION: {
        KnowledgeState.CANDIDATE,
        KnowledgeState.NEEDS_CLARIFICATION,
        KnowledgeState.APPROVED,
        KnowledgeState.QUARANTINED,
        KnowledgeState.REJECTED,
    },
    KnowledgeState.NEEDS_CLARIFICATION: {
        KnowledgeState.CANDIDATE,
        KnowledgeState.NEEDS_VERIFICATION,
        KnowledgeState.APPROVED,
        KnowledgeState.QUARANTINED,
        KnowledgeState.REJECTED,
    },
    KnowledgeState.CONFLICTED: {
        KnowledgeState.CANDIDATE,
        KnowledgeState.QUARANTINED,
        KnowledgeState.REJECTED,
    },
    KnowledgeState.APPROVED: {
        KnowledgeState.PUBLISHED,
        KnowledgeState.STALE,
        KnowledgeState.QUARANTINED,
        KnowledgeState.SUPERSEDED,
        KnowledgeState.REJECTED,
    },
    KnowledgeState.PUBLISHED: {
        KnowledgeState.STALE,
        KnowledgeState.QUARANTINED,
        KnowledgeState.SUPERSEDED,
    },
    KnowledgeState.STALE: {
        KnowledgeState.CANDIDATE,
        KnowledgeState.QUARANTINED,
        KnowledgeState.REJECTED,
    },
    # Quarantine is recoverable: a reviewer can send a note back for
    # re-verification. Retaining history is required either way — a quarantined
    # note is never silently deleted.
    KnowledgeState.QUARANTINED: {
        KnowledgeState.CANDIDATE,
        KnowledgeState.NEEDS_VERIFICATION,
        KnowledgeState.REJECTED,
    },
    KnowledgeState.SUPERSEDED: set(),
    KnowledgeState.REJECTED: set(),
}

# States that are valid as initial candidate status (no transition history required).
INITIAL_CANDIDATE_STATES: set[KnowledgeState] = {
    KnowledgeState.OBSERVED,
    KnowledgeState.CANDIDATE,
}


class InvalidTransitionError(Exception):
    """Raised when a state transition violates the allowed-transitions map."""

    def __init__(self, from_state: KnowledgeState, to_state: KnowledgeState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        allowed = ALLOWED_TRANSITIONS.get(from_state, set())
        super().__init__(
            f"Invalid transition: {from_state.value} → {to_state.value}. "
            f"Allowed from {from_state.value}: {sorted(s.value for s in allowed)}"
        )


class SourceKind(StrEnum):
    REPOSITORY = "repository"
    SESSION = "agent-session"


class Sensitivity(StrEnum):
    """Ordered sensitivity classification.

    Session-derived material inherits the highest classification of anything it
    touched; it is never downgraded by processing.
    """

    LOW = "low"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


_SENSITIVITY_ORDER: dict[str, int] = {
    Sensitivity.LOW: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.CONFIDENTIAL: 2,
    Sensitivity.SECRET: 3,
}


def max_sensitivity(*values: str) -> str:
    """Return the highest of the given classifications.

    An unrecognised value ranks above everything known: an unclassifiable
    input is treated as the most sensitive, never the least.
    """
    best = Sensitivity.LOW.value
    best_rank = -1
    for value in values:
        rank = _SENSITIVITY_ORDER.get(value, len(_SENSITIVITY_ORDER))
        if rank > best_rank:
            best_rank = rank
            best = value
    return best


class Source(BaseModel):
    kind: SourceKind
    reference: str


class ObservationData(BaseModel):
    kind: str
    summary: str


class ObservationClass(StrEnum):
    """How much weight an observation may be given.

    Phase 2 requires inspection output to be separated this way. The separation
    is the discipline: without it, an inference about what a `.zfill` call
    implies reads exactly like the fact that a file exists, and the reader has
    no way to tell which one they are looking at.
    """

    #: Directly observed and reproducible by anyone running the same check.
    VERIFIED_FACT = "verified-fact"
    #: A reading of the evidence that is probably right and is not proven.
    PROBABLE_INTERPRETATION = "probable-interpretation"
    #: Something looks wrong. Whether it *is* wrong is not yet established.
    CANDIDATE_ANOMALY = "candidate-anomaly"
    #: The repository does not record whether this state is deliberate.
    UNRESOLVED_INTENT = "unresolved-intent"
    #: Concerns a system this repository cannot check, so it can go stale
    #: without anything here changing.
    EXTERNAL_FACT = "external-fact"


#: Presentation order, weakest claim last.
OBSERVATION_CLASS_ORDER: tuple[ObservationClass, ...] = (
    ObservationClass.VERIFIED_FACT,
    ObservationClass.PROBABLE_INTERPRETATION,
    ObservationClass.CANDIDATE_ANOMALY,
    ObservationClass.UNRESOLVED_INTENT,
    ObservationClass.EXTERNAL_FACT,
)

OBSERVATION_CLASS_HEADINGS: dict[ObservationClass, str] = {
    ObservationClass.VERIFIED_FACT: "Verified facts",
    ObservationClass.PROBABLE_INTERPRETATION: "Probable interpretations",
    ObservationClass.CANDIDATE_ANOMALY: "Candidate anomalies",
    ObservationClass.UNRESOLVED_INTENT: "Unresolved intent",
    ObservationClass.EXTERNAL_FACT: "External facts not verifiable from this repository",
}


class Observation(BaseModel):
    id: str
    source: Source
    observed: ObservationData
    captured_at: datetime
    sensitivity: str
    #: Defaults to the weakest useful reading rather than the strongest: an
    #: observation that never declared its class must not be mistaken for a
    #: verified fact.
    classification: ObservationClass = ObservationClass.PROBABLE_INTERPRETATION


class EvidenceBase(BaseModel):
    kind: str


class SchemaEvidence(EvidenceBase):
    kind: Literal["schema"] = "schema"
    path: str


class FixtureEvidence(EvidenceBase):
    kind: Literal["fixture"] = "fixture"
    path: str


class CommandEvidence(EvidenceBase):
    kind: Literal["command"] = "command"
    command: str
    revision: str
    dirty: bool


Evidence = SchemaEvidence | FixtureEvidence | CommandEvidence | dict[str, Any]


class OperationalEffect(BaseModel):
    changes: str
    avoids: str
    severity: str


class Scope(BaseModel):
    paths: list[str]
    components: list[str] = Field(default_factory=list)
    environments: list[str]
    authorities: list[str]


class DependencyBase(BaseModel):
    kind: str
    path: str


class FileDependency(DependencyBase):
    kind: Literal["file"] = "file"
    hash: str | None = None


class SymbolDependency(DependencyBase):
    kind: Literal["symbol"] = "symbol"
    symbol: str


Dependency = FileDependency | SymbolDependency | dict[str, Any]


class TransitionRecord(BaseModel):
    from_state: KnowledgeState | None
    to_state: KnowledgeState
    timestamp: datetime
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Candidate(BaseModel):
    id: str
    claim: str
    status: KnowledgeState
    kind: str
    confidence: str
    derived_from: list[str]
    evidence: list[Evidence]
    operational_effect: OperationalEffect
    scope: Scope
    depends_on: list[Dependency]
    distribution: str
    durable_form: str | None = None
    transition_history: list[TransitionRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_transition_consistency(self) -> "Candidate":
        """Validate that transition history is consistent with current status.

        This catches accidental corruption during deserialization. It does NOT
        prevent deliberate YAML forgery (see module docstring).
        """
        if self.status in INITIAL_CANDIDATE_STATES:
            # Initial states may have empty transition history.
            return self

        if not self.transition_history:
            raise ValueError(
                f"Candidate {self.id} has status '{self.status.value}' but empty "
                f"transition history. Non-initial states require at least one "
                f"recorded transition. This may indicate accidental corruption."
            )

        terminal = self.transition_history[-1]
        if terminal.to_state != self.status:
            raise ValueError(
                f"Candidate {self.id} has status '{self.status.value}' but the "
                f"last transition record shows '{terminal.to_state.value}'. "
                f"This may indicate accidental corruption."
            )

        return self

    def transition_to(
        self,
        new_state: KnowledgeState,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Explicitly transition state with validation and history recording."""
        allowed = ALLOWED_TRANSITIONS.get(self.status, set())
        if new_state not in allowed:
            raise InvalidTransitionError(self.status, new_state)

        record = TransitionRecord(
            from_state=self.status,
            to_state=new_state,
            reason=reason,
            timestamp=datetime.now(UTC),
            metadata=metadata or {},
        )
        self.transition_history.append(record)
        # Use object.__setattr__ to bypass Pydantic's frozen-model behavior
        # while keeping the model_validator as the primary guard.
        object.__setattr__(self, "status", new_state)


class ApprovalRecord(BaseModel):
    """Separate approval record, stored independently from candidate status.

    This provides a second validation layer: publication requires both
    status == APPROVED and a matching ApprovalRecord. Transition-history
    validation detects accidental corruption during deserialization.
    Deliberate forgery of both files simultaneously is outside the local
    MVP trust boundary.
    """

    candidate_id: str
    approver: str
    approved_at: datetime
    reason: str
    scope_at_approval: Scope
    distribution_at_approval: str
    #: False when the approval came from a bulk action rather than a decision
    #: about this candidate. Recorded because "approved" and "nobody looked at
    #: it individually" must not be indistinguishable in the audit trail.
    per_candidate_review: bool = True


class Conflict(BaseModel):
    id: str
    status: str
    candidate_ids: list[str]
    reason: str
    publication_blocked: bool


class Owner(BaseModel):
    kind: str
    value: str


class Question(BaseModel):
    id: str
    status: str
    prompt: str
    reason: str
    owner: Owner
    candidate_id: str
    publish_to_agents: bool
    answer: str | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None


class PublishedKnowledge(BaseModel):
    id: str
    status: KnowledgeState
    durable_form: str
    approved_by: str
    approved_at: datetime
    last_verified_at: datetime


# Publishable distribution values.
PUBLISHABLE_DISTRIBUTIONS: set[str] = {"internal", "public", "repository"}


class SafetyFinding(BaseModel):
    """A risk detected in untrusted content.

    A finding is evidence of risk. The absence of findings is not evidence of
    safety, and nothing in this system may treat it as such.
    """

    kind: str
    detail: str
    excerpt_hint: str


class SessionEvent(BaseModel):
    """Private provenance for one harvested session event.

    This record holds the raw excerpt and never leaves `.coldstart/private/`.
    The repository-safe observation derived from it carries no transcript text.
    """

    id: str
    session_id: str
    source_name: str
    source_path: str
    record_uuid: str | None
    signal: str
    speaker: str
    captured_at: datetime
    occurred_at: datetime | None
    raw_excerpt: str
    sensitivity: str
    injection_findings: list[SafetyFinding] = Field(default_factory=list)
    secret_findings: list[SafetyFinding] = Field(default_factory=list)
    secret_scan_performed: bool = True
    corroborated_paths: list[str] = Field(default_factory=list)
    quarantined: bool = False
    quarantine_reason: str | None = None

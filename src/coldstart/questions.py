"""Selective clarification: targeted questions for unresolved intent.

A question is only worth asking when repository evidence cannot settle it, the
answer would change a promotion or scope decision, and somebody can actually be
named to answer it. Generation is deterministic and deliberately narrow: the
failure mode this phase guards against is a tool that interrogates maintainers
about things a grep would have answered.

No question ever answers itself. Deferring or marking a question unknown leaves
the candidate exactly where it was — silence is not confirmation.
"""

import fnmatch
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from coldstart.domain import (
    ALLOWED_TRANSITIONS,
    Candidate,
    KnowledgeState,
    Owner,
    Question,
)

# Question lifecycle. `open` and `deferred` both mean "still unanswered"; they
# are distinguished so that a deliberate deferral is not mistaken for neglect.
QUESTION_OPEN = "open"
QUESTION_ANSWERED = "answered"
QUESTION_DEFERRED = "deferred"
QUESTION_UNKNOWN = "unknown"
QUESTION_REJECTED = "rejected"

UNRESOLVED_STATUSES = {QUESTION_OPEN, QUESTION_DEFERRED}


def _question_id(candidate_id: str, prompt: str) -> str:
    """Derive a stable id from what is being asked, not from arrival order.

    Re-running generation must not create a second copy of a question that is
    already open, and must not renumber questions when an unrelated one is
    answered.
    """
    digest = hashlib.sha256(f"{candidate_id}\n{prompt}".encode()).hexdigest()
    return f"QUESTION-{digest[:8]}"


def _codeowners_path(repo_path: Path) -> Path | None:
    for candidate in (
        repo_path / "CODEOWNERS",
        repo_path / ".github" / "CODEOWNERS",
        repo_path / "docs" / "CODEOWNERS",
    ):
        if candidate.exists():
            return candidate
    return None


def resolve_owner(repo_path: Path, scope_paths: list[str]) -> Owner | None:
    """Resolve an owner for the given scope from CODEOWNERS.

    Returns None when no owner can be identified, which is a reason not to ask
    the question rather than a reason to ask it of nobody.
    """
    codeowners = _codeowners_path(repo_path)
    if codeowners is None:
        return None

    try:
        lines = codeowners.read_text(errors="ignore").splitlines()
    except OSError:
        return None

    rules: list[tuple[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        rules.append((parts[0], " ".join(parts[1:])))

    # CODEOWNERS gives precedence to the last matching rule.
    for pattern, owners in reversed(rules):
        normalized = pattern.lstrip("/").rstrip("/")
        for scope_path in scope_paths:
            if (
                pattern == "*"
                or fnmatch.fnmatch(scope_path, normalized)
                or fnmatch.fnmatch(scope_path, f"{normalized}/*")
                or scope_path.startswith(f"{normalized}/")
            ):
                return Owner(kind="codeowners", value=owners)

    return None


def build_question(
    candidate: Candidate,
    prompt: str,
    repo_path: Path,
    fallback_owner: Owner | None = None,
    reason: str = "Repository evidence does not resolve this.",
) -> Question:
    """Build a question about `candidate`, resolving an owner for it."""
    owner = resolve_owner(repo_path, candidate.scope.paths) or fallback_owner
    if owner is None:
        owner = Owner(kind="unassigned", value="unknown")

    return Question(
        id=_question_id(candidate.id, prompt),
        status=QUESTION_OPEN,
        prompt=prompt,
        reason=reason,
        owner=owner,
        candidate_id=candidate.id,
        publish_to_agents=False,
    )


def generate_questions(
    candidates: list[Candidate],
    repo_path: Path,
    existing: list[Question] | None = None,
) -> list[Question]:
    """Generate questions for candidates whose intent the repository cannot settle.

    All four preconditions from the specification must hold:

    1. the answer would change future behavior — the candidate names a failure
       it avoids;
    2. repository evidence cannot resolve it — the candidate is awaiting
       clarification, or asserts external ownership the repository never
       declares;
    3. a plausible owner can be identified — CODEOWNERS resolves the scope;
    4. the answer would affect promotion or scope — the candidate is not
       already settled.

    A candidate that fails any of them yields no question, which is the point.
    """
    already_asked = {q.candidate_id for q in (existing or []) if q.status in UNRESOLVED_STATUSES}
    generated: list[Question] = []

    for candidate in candidates:
        if candidate.id in already_asked:
            continue
        if candidate.status not in QUESTIONABLE_STATES:
            continue
        if not candidate.operational_effect.avoids:
            continue

        owner = resolve_owner(repo_path, candidate.scope.paths)
        if owner is None:
            continue

        if candidate.status == KnowledgeState.NEEDS_CLARIFICATION:
            prompt = f"Is the behavior described by {candidate.id} intentional?"
            reason = "A reviewer marked this candidate as needing clarification."
        elif candidate.kind == "constraint":
            path = candidate.scope.paths[0] if candidate.scope.paths else "this path"
            prompt = f"Is {path} permanently owned by an external generator?"
            reason = (
                "The file carries a generated marker, but the repository does not "
                "declare who owns it or what regenerates it."
            )
        else:
            continue

        generated.append(
            build_question(candidate, prompt, repo_path, fallback_owner=owner, reason=reason)
        )

    return generated


# Candidate states for which an unresolved question could still change the outcome.
QUESTIONABLE_STATES = {
    KnowledgeState.CANDIDATE,
    KnowledgeState.NEEDS_CLARIFICATION,
    KnowledgeState.NEEDS_VERIFICATION,
}


def apply_answer(
    question: Question,
    candidate: Candidate | None,
    answer: str,
    answered_by: str,
) -> str:
    """Record an answer and return a human-readable outcome.

    An answer returns the candidate to ordinary review. It never promotes it:
    a maintainer confirming a fact is not the same act as approving its
    publication to every agent with repository access.
    """
    object.__setattr__(question, "status", QUESTION_ANSWERED)
    object.__setattr__(question, "answer", answer)
    object.__setattr__(question, "answered_by", answered_by)
    object.__setattr__(question, "answered_at", datetime.now(UTC))

    if candidate is None:
        return f"{question.id} answered (candidate {question.candidate_id} not found)"

    if KnowledgeState.CANDIDATE in ALLOWED_TRANSITIONS.get(candidate.status, set()):
        candidate.transition_to(
            KnowledgeState.CANDIDATE,
            reason=f"{question.id} answered; returned to review",
            metadata={"answered_by": answered_by},
        )
        return f"{question.id} answered; {candidate.id} returned to review"

    return f"{question.id} answered; {candidate.id} left in '{candidate.status.value}'"


def apply_defer(question: Question, note: str) -> str:
    """Defer a question without touching the candidate."""
    object.__setattr__(question, "status", QUESTION_DEFERRED)
    object.__setattr__(question, "answer", None)
    if note:
        object.__setattr__(question, "reason", note)
    return f"{question.id} deferred; candidate {question.candidate_id} unchanged"


def apply_unknown(question: Question) -> str:
    """Mark a question as unanswerable. This is a result, not a confirmation."""
    object.__setattr__(question, "status", QUESTION_UNKNOWN)
    return f"{question.id} marked unknown; candidate {question.candidate_id} remains unpromoted"


def apply_reject(question: Question, candidate: Candidate | None, rejected_by: str) -> str:
    """Reject the underlying candidate in response to a question."""
    object.__setattr__(question, "status", QUESTION_REJECTED)

    if candidate is None:
        return f"{question.id} rejected (candidate {question.candidate_id} not found)"

    if KnowledgeState.REJECTED in ALLOWED_TRANSITIONS.get(candidate.status, set()):
        candidate.transition_to(
            KnowledgeState.REJECTED,
            reason=f"Rejected in response to {question.id}",
            metadata={"rejected_by": rejected_by},
        )
        return f"{question.id} rejected; {candidate.id} rejected"

    return f"{question.id} rejected; {candidate.id} left in '{candidate.status.value}'"


def apply_route(question: Question, owner_value: str, owner_kind: str = "manual") -> str:
    """Route a question to a named owner, leaving it open."""
    object.__setattr__(question, "owner", Owner(kind=owner_kind, value=owner_value))
    object.__setattr__(question, "status", QUESTION_OPEN)
    return f"{question.id} routed to {owner_value}"

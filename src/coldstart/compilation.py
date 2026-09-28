"""Candidate compilation: observation-to-candidate pipeline.

Implements evidence sufficiency, derivability, actionability, scope
validation, and conflict detection for all MVP observation categories.
"""

from pathlib import Path
from typing import Any

from coldstart.domain import (
    INITIAL_CANDIDATE_STATES,
    Candidate,
    Conflict,
    Dependency,
    FileDependency,
    KnowledgeState,
    Observation,
    OperationalEffect,
    Scope,
)
from coldstart.staleness import compute_file_hash


def check_evidence_sufficiency(candidate: Candidate) -> dict[str, Any]:
    """Check if evidence is sufficient for the claim."""
    if not candidate.evidence:
        return {"result": "reject", "reason": "No evidence provided."}
    return {"result": "pass", "reason": "Evidence present."}


#: Documents a competent agent would read before touching a repository. If a
#: subject is already discussed here, the fact is derivable at grep cost.
_ORIENTATION_DOCS = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
    "CONTRIBUTING.md",
)


def _orientation_text(repo_path: Path) -> str:
    """Concatenate the documents a cold agent would already have read."""
    parts: list[str] = []
    for name in _ORIENTATION_DOCS:
        doc = repo_path / name
        if doc.is_file():
            try:
                parts.append(doc.read_text(errors="ignore"))
            except OSError:
                continue
    docs_dir = repo_path / "docs"
    if docs_dir.is_dir():
        for doc in sorted(docs_dir.glob("*.md"))[:20]:
            try:
                parts.append(doc.read_text(errors="ignore"))
            except OSError:
                continue
    return "\n".join(parts)


def check_derivability(candidate: Candidate, repo_path: Path | None = None) -> dict[str, Any]:
    """Reject facts a competent agent could recover by reading the obvious docs.

    The specification's test is "could a competent agent with five minutes of
    grep have found this?" — a question about the *subject*, not the wording.

    An earlier version compared the words of the claim against README text at a
    70% threshold. Measured across ten real repositories it rejected nothing at
    all, 165 candidates out of 165, because every claim is generated from a
    template whose phrasing appears in no README ever written. The filter the
    concept note calls strict was structurally incapable of firing.

    This version asks whether the thing the claim is *about* is already
    discussed in the documents a cold agent reads first. That is coarser than a
    judgment about whether the specific trap is documented, and it is
    deliberately biased toward rejection: the concept note is explicit that the
    cost of keeping a derivable note is paid on every future run, while the
    cost of dropping one is paid once.
    """
    if not repo_path:
        return {"result": "uncertain", "reason": "No repository path for derivability check."}

    orientation = _orientation_text(repo_path)
    if not orientation.strip():
        return {
            "result": "uncertain",
            "reason": "No orientation documents exist, so derivability cannot be judged.",
        }

    lowered = orientation.lower()
    subjects = [p for p in candidate.scope.paths if p]
    if not subjects:
        return {"result": "uncertain", "reason": "Candidate names no subject to look for."}

    for subject in subjects:
        relative = subject.lstrip("./")
        basename = Path(relative).name
        if relative.lower() in lowered:
            return {
                "result": "reject",
                "reason": (
                    f"{relative} is already described in the repository's orientation "
                    f"documents; an agent would find this by reading them."
                ),
            }
        # A bare filename counts only when it is distinctive enough that a
        # mention is unlikely to be coincidental.
        if len(basename) > 6 and basename.lower() in lowered:
            return {
                "result": "reject",
                "reason": (
                    f"{basename} is already mentioned in the repository's orientation "
                    f"documents; an agent would find this by reading them."
                ),
            }

    return {"result": "pass", "reason": "Subject is not covered by the orientation documents."}


# Candidate kinds that describe repository structure rather than a constraint
# on behavior. These are candidate kinds, not observation kinds: the previous
# version compared against observation kinds, which no candidate ever carries,
# so the branch never ran and every candidate passed unconditionally.
STRUCTURAL_CANDIDATE_KINDS = {"structure", "layout", "inventory"}


def check_actionability(candidate: Candidate) -> dict[str, Any]:
    """Check whether knowing this claim would change a plausible future action.

    Rejects claims that describe structure without behavioral consequence, and
    claims that name no failure they prevent — "would knowing this change what
    an agent does" is the whole admission question.
    """
    if not candidate.operational_effect.avoids.strip():
        return {
            "result": "reject",
            "reason": "No failure identified that knowing this would avoid.",
        }

    if not candidate.operational_effect.changes.strip():
        return {
            "result": "reject",
            "reason": "No change of action identified.",
        }

    if candidate.kind in STRUCTURAL_CANDIDATE_KINDS:
        return {
            "result": "reject",
            "reason": "Structural description rather than a constraint on behavior.",
        }

    if candidate.operational_effect.severity == "high":
        return {"result": "pass", "reason": "High-severity operational effect."}

    return {"result": "pass", "reason": "Identifies a specific failure to avoid."}


def check_scope(candidate: Candidate, repo_path: Path | None = None) -> dict[str, Any]:
    """Validate that scope paths exist in the repository."""
    if not repo_path:
        return {"result": "pass", "reason": "No repository path for scope validation."}

    for scope_path in candidate.scope.paths:
        if "**" in scope_path or "*" in scope_path:
            continue  # Glob patterns are valid
        full_path = repo_path / scope_path
        if not full_path.exists():
            return {
                "result": "reject",
                "reason": f"Scope path {scope_path} does not exist.",
            }

    return {"result": "pass", "reason": "All scope paths valid."}


def detect_conflicts(
    candidates: list[Candidate],
) -> list[Conflict]:
    """Detect candidates that prescribe incompatible actions for the same scope.

    A conflict is a contradiction, not a coincidence. Two candidates that state
    different true facts about one file — that it is generated *and* that its
    ids keep leading zeros — are complementary, and flagging them blocks both
    from publication over nothing.

    The detectable form of contradiction available without semantic analysis is
    incompatible prescriptions: same scope, same kind, but different required
    actions. That is narrow on purpose; conflicts a human spots are recorded
    through review rather than guessed at here.
    """
    conflicts: list[Conflict] = []
    path_claims: dict[str, list[Candidate]] = {}

    for c in candidates:
        for p in c.scope.paths:
            path_claims.setdefault(p, []).append(c)

    conflict_id = 1
    for scope_path in sorted(path_claims):
        claimants = path_claims[scope_path]
        if len(claimants) < 2:
            continue
        for i, c1 in enumerate(claimants):
            for c2 in claimants[i + 1 :]:
                if c1.kind != c2.kind:
                    continue
                if c1.operational_effect.changes == c2.operational_effect.changes:
                    # Same prescription, different wording: not a contradiction.
                    continue
                conflicts.append(
                    Conflict(
                        id=f"CONFLICT-{conflict_id:03d}",
                        status="unresolved",
                        candidate_ids=[c1.id, c2.id],
                        reason=(
                            f"Candidates {c1.id} and {c2.id} prescribe incompatible "
                            f"actions for scope {scope_path}: "
                            f"'{c1.operational_effect.changes}' versus "
                            f"'{c2.operational_effect.changes}'."
                        ),
                        publication_blocked=True,
                    )
                )
                conflict_id += 1

    return conflicts


# --- Observation-to-candidate mapping ---


def _file_dependency(reference: str, repo_path: Path | None) -> Dependency:
    """Build a file dependency, recording the file's hash when it is readable.

    The hash is what lets `warmstart verify` notice that a dependency changed
    rather than merely disappeared. Without it a note survives every edit to
    the code it describes, which is the staleness failure the product exists
    to prevent. A dependency whose file cannot be read is recorded without a
    hash, so existence checking still applies.
    """
    if repo_path is None:
        return FileDependency(path=reference)

    target = repo_path / reference
    try:
        return FileDependency(path=reference, hash=compute_file_hash(target))
    except OSError:
        return FileDependency(path=reference)


def _compile_type_inconsistency(obs: Observation, repo_path: Path | None) -> Candidate:
    """Compile a type-inconsistency observation into a candidate."""
    return Candidate(
        id=obs.id.replace("OBS-", "CAN-"),
        claim=obs.observed.summary,
        kind="invariant",
        status=KnowledgeState.CANDIDATE,
        confidence="high",
        derived_from=[obs.id],
        evidence=[{"kind": "observation", "source": obs.source.reference}],
        operational_effect=OperationalEffect(
            changes="Preserve identifier as string during processing.",
            avoids="Breaking data reconciliation by stripping leading zeros.",
            severity="high",
        ),
        scope=Scope(
            paths=[obs.source.reference],
            environments=[],
            authorities=[],
        ),
        depends_on=[_file_dependency(obs.source.reference, repo_path)],
        distribution="internal",
        durable_form="regression-test",
    )


def _compile_generated_file(obs: Observation, repo_path: Path | None) -> Candidate:
    """Compile a generated-file observation into a candidate."""
    return Candidate(
        id=obs.id.replace("OBS-", "CAN-"),
        claim=obs.observed.summary,
        kind="constraint",
        status=KnowledgeState.CANDIDATE,
        confidence="medium",
        derived_from=[obs.id],
        evidence=[{"kind": "observation", "source": obs.source.reference}],
        operational_effect=OperationalEffect(
            changes="Do not manually edit this file.",
            avoids="Manual edits being overwritten by the generator.",
            severity="medium",
        ),
        scope=Scope(
            paths=[obs.source.reference],
            environments=[],
            authorities=[],
        ),
        depends_on=[_file_dependency(obs.source.reference, repo_path)],
        distribution="internal",
        durable_form="agent-note",
    )


def _compile_codeowners(obs: Observation, repo_path: Path | None) -> Candidate:
    """Compile a CODEOWNERS observation into a candidate."""
    return Candidate(
        id=obs.id.replace("OBS-", "CAN-"),
        claim=obs.observed.summary,
        kind="ownership",
        status=KnowledgeState.CANDIDATE,
        confidence="high",
        derived_from=[obs.id],
        evidence=[{"kind": "observation", "source": obs.source.reference}],
        operational_effect=OperationalEffect(
            changes="Route changes to designated owners.",
            avoids="Changes merged without required review.",
            severity="medium",
        ),
        scope=Scope(
            paths=[obs.source.reference],
            environments=[],
            authorities=[],
        ),
        depends_on=[_file_dependency(obs.source.reference, repo_path)],
        distribution="internal",
        durable_form="agent-note",
    )


def _compile_external_integration(obs: Observation, repo_path: Path | None) -> Candidate:
    """Compile an external-integration observation into a candidate."""
    return Candidate(
        id=obs.id.replace("OBS-", "CAN-"),
        claim=obs.observed.summary,
        kind="integration",
        status=KnowledgeState.CANDIDATE,
        confidence="high",
        derived_from=[obs.id],
        evidence=[{"kind": "observation", "source": obs.source.reference}],
        operational_effect=OperationalEffect(
            changes="Respect external service contracts and authentication.",
            avoids="Breaking external integrations.",
            severity="medium",
        ),
        scope=Scope(
            paths=[obs.source.reference],
            environments=[],
            authorities=[],
        ),
        depends_on=[_file_dependency(obs.source.reference, repo_path)],
        distribution="internal",
        durable_form="agent-note",
    )


def _compile_session_event(obs: Observation, repo_path: Path | None) -> Candidate:
    """Compile a session-derived observation into an unverified candidate.

    Session material never enters the pipeline as an ordinary candidate. The
    claim is a pointer to private provenance, not an assertion, because nothing
    in an untrusted transcript may be restated as fact by automation.

    Distribution follows the retained sensitivity: anything above `internal` is
    not a publishable distribution, so the candidate cannot reach `AGENTS.md`
    even if it were approved.
    """
    distribution = "internal" if obs.sensitivity in ("low", "internal") else "private"

    return Candidate(
        id=obs.id.replace("OBS-", "CAN-"),
        claim=obs.observed.summary,
        kind="session-signal",
        status=KnowledgeState.CANDIDATE,
        confidence="low",
        derived_from=[obs.id],
        evidence=[{"kind": "private-provenance", "source": obs.source.reference}],
        operational_effect=OperationalEffect(
            changes=(
                "Read the private provenance record, then write the durable claim "
                "by hand if one is warranted."
            ),
            avoids="Repeating a mistake a previous session already paid for.",
            severity="medium",
        ),
        scope=Scope(paths=[], environments=[], authorities=[]),
        depends_on=[],
        distribution=distribution,
        durable_form="agent-note",
    )


def _repository_candidate(
    obs: Observation,
    repo_path: Path | None,
    *,
    kind: str,
    changes: str,
    avoids: str,
    severity: str,
    confidence: str,
    durable_form: str,
    distribution: str = "internal",
) -> Candidate:
    """Shared shape for candidates derived from a repository observation."""
    return Candidate(
        id=obs.id.replace("OBS-", "CAN-"),
        claim=obs.observed.summary,
        kind=kind,
        status=KnowledgeState.CANDIDATE,
        confidence=confidence,
        derived_from=[obs.id],
        evidence=[{"kind": "observation", "source": obs.source.reference}],
        operational_effect=OperationalEffect(changes=changes, avoids=avoids, severity=severity),
        scope=Scope(paths=[obs.source.reference], environments=[], authorities=[]),
        depends_on=[_file_dependency(obs.source.reference, repo_path)],
        distribution=distribution,
        durable_form=durable_form,
    )


def _compile_protected_path(obs: Observation, repo_path: Path | None) -> Candidate:
    """A write that will be refused is the highest-value thing to know early.

    An agent that does not know a hook exists will fight it, and the
    specification's own example of a hard-won fact is exactly this.
    """
    return _repository_candidate(
        obs,
        repo_path,
        kind="constraint",
        changes="Expect writes covered by this policy to be refused or rewritten.",
        avoids="Fighting a hook, or routing around a protection that is working.",
        severity="high",
        confidence="high",
        durable_form="agent-note",
    )


def _compile_source_of_truth(obs: Observation, repo_path: Path | None) -> Candidate:
    return _repository_candidate(
        obs,
        repo_path,
        kind="authority",
        changes="Treat the named authority as canonical; do not edit downstream copies.",
        avoids="Changing a derived copy and having it silently overwritten.",
        severity="high",
        confidence="medium",
        durable_form="agent-note",
    )


def _compile_doc_contradiction(obs: Observation, repo_path: Path | None) -> Candidate:
    """The data wins over the document, and somebody must be told which."""
    return _repository_candidate(
        obs,
        repo_path,
        kind="contradiction",
        changes="Trust the repository over this document until the two agree.",
        avoids="Following documentation that describes files which no longer exist.",
        severity="medium",
        confidence="medium",
        durable_form="open-question",
    )


def _compile_repeated_revert(obs: Observation, repo_path: Path | None) -> Candidate:
    """Somebody already tried the obvious change here and undid it."""
    return _repository_candidate(
        obs,
        repo_path,
        kind="landmark",
        changes="Read the revert before changing this file; the obvious fix was tried.",
        avoids="Reintroducing a change that was already made and rolled back.",
        severity="medium",
        confidence="medium",
        durable_form="agent-note",
    )


def _compile_ci_policy(obs: Observation, repo_path: Path | None) -> Candidate:
    return _repository_candidate(
        obs,
        repo_path,
        kind="policy",
        changes="Run the gating checks locally before proposing a change.",
        avoids="Submitting work that CI will reject.",
        severity="medium",
        confidence="high",
        durable_form="contract-reference",
    )


def _compile_migrations(obs: Observation, repo_path: Path | None) -> Candidate:
    return _repository_candidate(
        obs,
        repo_path,
        kind="constraint",
        changes="Add a new migration rather than editing one that has already run.",
        avoids="Editing applied history, which does not change deployed state.",
        severity="high",
        confidence="high",
        durable_form="agent-note",
    )


def _compile_unusual_exception(obs: Observation, repo_path: Path | None) -> Candidate:
    """Enforceable knowledge becomes a rule before it becomes prose.

    A swallowed exception is exactly what a linter catches, so the knowledge
    hierarchy sends it there rather than into the agent-facing file.
    """
    return _repository_candidate(
        obs,
        repo_path,
        kind="code-smell",
        changes="Narrow or surface the handler so failures are visible.",
        avoids="A broken assumption persisting silently.",
        severity="low",
        confidence="high",
        durable_form="linter-rule",
    )


# Map observation kinds to their compilers
_KIND_COMPILERS = {
    "type-inconsistency": _compile_type_inconsistency,
    "generated-file": _compile_generated_file,
    "codeowners": _compile_codeowners,
    "external-integration": _compile_external_integration,
    "session-expectation-contradicted": _compile_session_event,
    "session-human-correction": _compile_session_event,
    "protected-path": _compile_protected_path,
    "source-of-truth": _compile_source_of_truth,
    "doc-contradiction": _compile_doc_contradiction,
    "repeated-revert": _compile_repeated_revert,
    "ci-policy": _compile_ci_policy,
    "migrations": _compile_migrations,
    "unusual-exception": _compile_unusual_exception,
}

# Observation kinds whose candidates must be verified by a human before they
# can be approved. Repository patterns are self-evidencing; session material is
# not, and the specification prohibits publishing it directly.
_REQUIRES_VERIFICATION = {
    "session-expectation-contradicted",
    "session-human-correction",
}

# Observation kinds that are structural/informational and do not compile to candidates
_STRUCTURAL_KINDS = {
    "entry-point",
    "test-infrastructure",
    "test-fixture",
    "schema",
    "agent-instruction-file",
}


def compile_candidates(
    observations: list[Observation],
    repo_path: Path | None = None,
) -> tuple[list[Candidate], list[Conflict]]:
    """Compile observations into candidates through the analysis pipeline.

    Returns (candidates, conflicts).
    Structural observations (entry points, test infrastructure) are not
    compiled into candidates — they provide context but don't represent
    action-changing knowledge.
    """
    candidates: list[Candidate] = []

    for obs in observations:
        # Skip structural observations
        if obs.observed.kind in _STRUCTURAL_KINDS:
            continue

        compiler = _KIND_COMPILERS.get(obs.observed.kind)
        if compiler is None:
            continue

        candidate = compiler(obs, repo_path)

        # Run pipeline checks
        evidence_check = check_evidence_sufficiency(candidate)
        if evidence_check["result"] == "reject":
            continue

        derivability_check = check_derivability(candidate, repo_path)
        if derivability_check["result"] == "reject":
            continue

        actionability_check = check_actionability(candidate)
        if actionability_check["result"] == "reject":
            continue

        scope_check = check_scope(candidate, repo_path)
        if scope_check["result"] == "reject":
            continue

        if obs.observed.kind in _REQUIRES_VERIFICATION:
            candidate.transition_to(
                KnowledgeState.NEEDS_VERIFICATION,
                reason=(
                    "Session-derived: requires a human to read the private "
                    "provenance record before it can be approved."
                ),
            )

        candidates.append(candidate)

    # Detect conflicts
    conflicts = detect_conflicts(candidates)

    # Mark conflicted candidates
    conflicted_ids: set[str] = set()
    for conf in conflicts:
        conflicted_ids.update(conf.candidate_ids)

    for c in candidates:
        if c.id in conflicted_ids:
            c.transition_to(
                KnowledgeState.CONFLICTED,
                reason="Contradictory claims detected.",
            )

    return candidates, conflicts


def merge_candidates(existing: list[Candidate], fresh: list[Candidate]) -> list[Candidate]:
    """Merge a fresh compilation into already-persisted candidate state.

    Recompiling must not destroy review outcomes. A persisted candidate that
    has moved past an initial state carries approval, rejection, or quarantine
    history that only a human or `verify` may change, so the persisted record
    wins for that id. Candidates still in an initial state are replaced by the
    fresh compilation, which is what lets re-inspection pick up a changed claim
    or a new dependency hash.

    A persisted candidate that no longer compiles is retained when it has
    progressed — dropping it would silently discard an approval and leave a
    published note with no record behind it — and dropped otherwise.
    """
    by_id = {c.id: c for c in existing}
    merged: list[Candidate] = []
    fresh_ids: set[str] = set()

    for candidate in fresh:
        fresh_ids.add(candidate.id)
        prior = by_id.get(candidate.id)
        if prior is not None and prior.status not in INITIAL_CANDIDATE_STATES:
            merged.append(prior)
        else:
            merged.append(candidate)

    for candidate in existing:
        if candidate.id not in fresh_ids and candidate.status not in INITIAL_CANDIDATE_STATES:
            merged.append(candidate)

    return merged

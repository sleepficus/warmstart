"""WarmStart CLI: public command interface.

All commands operate through installed entry points.
Deferred commands return exit code 1 with a clear message.
"""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from coldstart.domain import (
    ALLOWED_TRANSITIONS,
    OBSERVATION_CLASS_HEADINGS,
    OBSERVATION_CLASS_ORDER,
    ApprovalRecord,
    Candidate,
    Conflict,
    KnowledgeState,
    Observation,
    ObservationClass,
    Owner,
    Question,
)
from coldstart.inspection import init_repository, inspect_repository
from coldstart.publication import published_note_ids
from coldstart.questions import build_question
from coldstart.safeguards import persist_proposal
from coldstart.storage import load_state, save_state, state_dir

app = typer.Typer(
    help="WarmStart: a local, network-free repository knowledge compiler for coding agents"
)

#: Relocates all WarmStart state out of the target repository. With it set,
#: every command except `publish` leaves the repository byte-for-byte
#: untouched, which is what makes analysing someone else's repository — or a
#: measurement run that must be provably read-only — possible.
StateDirOption = Annotated[
    Path | None,
    typer.Option(
        "--state-dir",
        help="Keep WarmStart state (the .coldstart directory) here, outside the repository.",
    ),
]

# Candidates a reviewer can still act on. Terminal and published states are
# excluded; everything else must have a way back into the workflow, or a
# conflicted or quarantined candidate is stranded forever.
REVIEWABLE_STATES = {
    KnowledgeState.CANDIDATE,
    KnowledgeState.NEEDS_VERIFICATION,
    KnowledgeState.NEEDS_CLARIFICATION,
    KnowledgeState.CONFLICTED,
    KnowledgeState.QUARANTINED,
    KnowledgeState.STALE,
}


def _conflict_id(candidate_ids: list[str], reason: str) -> str:
    """Stable id for a reviewer-declared conflict, so re-marking is idempotent."""
    payload = "\n".join([*sorted(candidate_ids), reason])
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def _render_observations(observations: list[Observation]) -> None:
    """Present observations separated by how much weight they may be given.

    Phase 2 requires exactly this separation, and it is the difference between
    a tool that reports findings and one that launders inference into fact. An
    empty section is printed rather than skipped: "no candidate anomalies" is
    itself a result.
    """
    console = Console()
    by_class: dict[ObservationClass, list[Observation]] = {
        cls: [] for cls in OBSERVATION_CLASS_ORDER
    }
    for observation in observations:
        by_class.setdefault(observation.classification, []).append(observation)

    for cls in OBSERVATION_CLASS_ORDER:
        found = by_class.get(cls, [])
        heading = OBSERVATION_CLASS_HEADINGS[cls]
        console.print(f"\n[bold]{heading}[/bold] ({len(found)})")

        if not found:
            console.print("  [dim]none[/dim]")
            continue

        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Kind", no_wrap=True)
        table.add_column("Reference", no_wrap=True, overflow="fold")
        table.add_column("Summary", overflow="fold")
        for observation in sorted(found, key=lambda o: (o.observed.kind, o.source.reference)):
            table.add_row(
                observation.observed.kind,
                observation.source.reference,
                observation.observed.summary,
            )
        console.print(table)

    console.print(
        "\n[dim]Only the first section is established. Everything below it is a "
        "reading of the evidence and must be reviewed before it is trusted.[/dim]"
    )


def _display_candidate(
    candidate: Candidate,
    conflicts: list[Conflict],
    repo_path: Path,
) -> None:
    """Show everything Phase 7 requires a reviewer to see before deciding."""
    from coldstart.staleness import verify_file_dependency

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"Candidate: {candidate.id}   [{candidate.status.value}]")
    typer.echo(f"  Claim: {candidate.claim}")
    typer.echo(f"  Kind: {candidate.kind}")
    typer.echo(f"  Confidence: {candidate.confidence}")
    typer.echo("  Provenance: repository observation (interpretation, not verified fact)")
    typer.echo(f"  Derived from: {', '.join(candidate.derived_from) or 'none recorded'}")
    typer.echo(f"  Severity: {candidate.operational_effect.severity}")
    typer.echo(f"  Changes: {candidate.operational_effect.changes}")
    typer.echo(f"  Avoids: {candidate.operational_effect.avoids}")
    typer.echo(f"  Scope paths: {', '.join(candidate.scope.paths) or 'unscoped'}")
    typer.echo(f"  Environments: {', '.join(candidate.scope.environments) or 'unspecified'}")
    typer.echo(f"  Authorities: {', '.join(candidate.scope.authorities) or 'none'}")
    typer.echo(f"  Distribution: {candidate.distribution}")
    typer.echo(f"  Proposed durable form: {candidate.durable_form or 'not set'}")

    typer.echo(f"  Evidence: {len(candidate.evidence)} item(s)")
    for e in candidate.evidence:
        if isinstance(e, dict):
            typer.echo(f"    - {e.get('kind', 'unknown')}: {e.get('source', 'N/A')}")
        else:
            typer.echo(f"    - {e.kind}")

    typer.echo(f"  Staleness dependencies: {len(candidate.depends_on)}")
    for dep in candidate.depends_on:
        dep_path = dep.get("path") if isinstance(dep, dict) else getattr(dep, "path", "?")
        is_valid, reason = verify_file_dependency(dep, repo_path)
        state = "ok" if is_valid else f"STALE — {reason}"
        typer.echo(f"    - {dep_path}: {state}")

    if conflicts:
        typer.echo(f"  Conflicts: {len(conflicts)}")
        for conf in conflicts:
            typer.echo(f"    - {conf.id} ({conf.status}): {conf.reason}")
    else:
        typer.echo("  Conflicts: none")

    typer.echo("\n  Proposed agent-facing text:")
    typer.echo(f"    - **{candidate.kind.title()}**: {candidate.claim} [{candidate.id}]")


@app.command()
def init(
    path: Annotated[str, typer.Argument()] = ".", state_dir_opt: StateDirOption = None
) -> None:
    """Initialize WarmStart in a repository."""
    repo_path = Path(path)
    config = init_repository(repo_path, state_dir_opt)
    git_status = "available" if config["repository"]["git_available"] else "not found (warning)"
    langs = ", ".join(config["repository"].get("languages", [])) or "none detected"
    typer.echo(f"Initialized WarmStart in {repo_path.absolute()}")
    typer.echo(f"  Git: {git_status}")
    typer.echo(f"  Languages: {langs}")


@app.command()
def inspect(
    path: Annotated[str, typer.Argument()] = ".", state_dir_opt: StateDirOption = None
) -> None:
    """Inspect a repository and extract observations."""
    repo_path = Path(path)
    observations = inspect_repository(repo_path)

    # Persist observations
    obs_path = state_dir(repo_path, state_dir_opt) / "observations.yaml"
    obs_path.parent.mkdir(parents=True, exist_ok=True)

    # Merge rather than overwrite. Session-derived observations come from
    # `harvest` and cannot be regenerated by re-inspecting the repository, so
    # replacing the file wholesale silently destroys them.
    existing = load_state(Observation, obs_path) if obs_path.exists() else []
    fresh_ids = {o.id for o in observations}
    preserved = [o for o in existing if o.id not in fresh_ids]
    save_state(observations + preserved, obs_path)

    typer.echo(f"Extracted {len(observations)} observations → {obs_path}")
    if preserved:
        typer.echo(f"  Preserved {len(preserved)} observation(s) from other sources.")
    _render_observations(observations)


@app.command(name="compile")
def compile_cmd(
    path: Annotated[str, typer.Argument()] = ".", state_dir_opt: StateDirOption = None
) -> None:
    """Compile candidates from observations."""
    from coldstart.compilation import compile_candidates, merge_candidates
    from coldstart.safeguards import persist_proposal

    repo_path = Path(path)
    obs_path = state_dir(repo_path, state_dir_opt) / "observations.yaml"

    # Read persisted observations, or run inspect first
    if obs_path.exists():
        observations = load_state(Observation, obs_path)
    else:
        observations = inspect_repository(repo_path)
        obs_path.parent.mkdir(parents=True, exist_ok=True)
        save_state(observations, obs_path)

    fresh, conflicts = compile_candidates(observations, repo_path)

    # Merge into persisted state so that recompiling never discards a review
    # outcome. Without this, `compile` silently reverts approved candidates to
    # `candidate` and the next `publish` empties the managed section.
    candidates_path = state_dir(repo_path, state_dir_opt) / "candidates.yaml"
    existing = load_state(Candidate, candidates_path) if candidates_path.exists() else []
    candidates = merge_candidates(existing, fresh)

    preserved = len(candidates) - len(fresh)
    save_state(candidates, candidates_path)

    for c in candidates:
        persist_proposal(c, repo_path, state_dir_opt)

    if candidates:
        typer.echo(f"Compiled {len(fresh)} candidates → .coldstart/candidates.yaml")
        if preserved or any(
            c.status not in (KnowledgeState.CANDIDATE, KnowledgeState.OBSERVED) for c in candidates
        ):
            reviewed = sum(
                1
                for c in candidates
                if c.status not in (KnowledgeState.CANDIDATE, KnowledgeState.OBSERVED)
            )
            typer.echo(f"  Preserved {reviewed} already-reviewed candidate(s).")
    else:
        typer.echo("No candidates compiled.")

    # Always rewrite conflicts, including to empty: a resolved conflict left on
    # disk would block publication forever.
    conflicts_path = state_dir(repo_path, state_dir_opt) / "conflicts.yaml"
    save_state(conflicts, conflicts_path)
    if conflicts:
        typer.echo(f"Detected {len(conflicts)} conflicts → .coldstart/conflicts.yaml")


@app.command()
def review(
    path: Annotated[str, typer.Argument()] = ".",
    state_dir_opt: StateDirOption = None,
    approve_all: bool = False,
    approver: Annotated[
        str | None,
        typer.Option(help="Identity recorded on every approval. Defaults to $USER."),
    ] = None,
) -> None:
    """Review candidates and record approval decisions.

    Presents every candidate that is not in a terminal or published state, so
    that conflicted, quarantined, and needs-verification candidates have a way
    back into the workflow instead of being stranded.
    """
    from coldstart.publication import PUBLISHABLE_STATES

    repo_path = Path(path)
    candidates_path = state_dir(repo_path, state_dir_opt) / "candidates.yaml"
    approvals_path = state_dir(repo_path, state_dir_opt) / "approvals.yaml"
    conflicts_path = state_dir(repo_path, state_dir_opt) / "conflicts.yaml"

    if not candidates_path.exists():
        typer.echo("No candidates to review.")
        return

    if approve_all and not approver:
        typer.echo(
            "Error: --approve-all requires --approver NAME.\n"
            "Bulk approval bypasses per-candidate review, so it must be "
            "attributable to a person rather than to $USER.",
            err=True,
        )
        raise typer.Exit(code=2)

    reviewer = approver or os.environ.get("USER") or "unknown"

    candidates = load_state(Candidate, candidates_path)
    existing_approvals = (
        load_state(ApprovalRecord, approvals_path) if approvals_path.exists() else []
    )
    conflicts = load_state(Conflict, conflicts_path) if conflicts_path.exists() else []
    conflicts_by_candidate: dict[str, list[Conflict]] = {}
    for conf in conflicts:
        for cid in conf.candidate_ids:
            conflicts_by_candidate.setdefault(cid, []).append(conf)

    new_approvals: list[ApprovalRecord] = []
    new_questions: list[Question] = []
    new_conflicts: list[Conflict] = []
    dirty = False

    reviewable = [c for c in candidates if c.status in REVIEWABLE_STATES]

    if not reviewable:
        typer.echo("No candidates pending review.")
        return

    for c in reviewable:
        _display_candidate(c, conflicts_by_candidate.get(c.id, []), repo_path)

        if approve_all:
            action = "approve"
        else:
            typer.echo(
                "\n  Actions: [a]pprove, [e]dit claim, [r]eject, [q]uarantine,\n"
                "           [t] convert to test, [c] convert to question,\n"
                "           [v] request verification, [n] restrict scope,\n"
                "           [m] mark conflict, [o] reopen as candidate, [s]kip"
            )
            action = typer.prompt("  Action", default="s").strip().lower()

        if action in ("a", "approve"):
            if c.status not in ALLOWED_TRANSITIONS or (
                KnowledgeState.APPROVED not in ALLOWED_TRANSITIONS[c.status]
            ):
                typer.echo(
                    f"  ! {c.id} cannot be approved from '{c.status.value}'. "
                    f"Reopen it as a candidate first.",
                    err=True,
                )
                continue
            justification = (
                "Approved without per-candidate review (--approve-all)"
                if approve_all
                else typer.prompt("  Justification", default="Approved during CLI review")
            )
            c.transition_to(
                KnowledgeState.APPROVED,
                reason="Human review approval",
                metadata={"approver": reviewer, "justification": justification},
            )
            new_approvals.append(
                ApprovalRecord(
                    candidate_id=c.id,
                    approver=reviewer,
                    approved_at=datetime.now(UTC),
                    reason=justification,
                    scope_at_approval=c.scope,
                    distribution_at_approval=c.distribution,
                    per_candidate_review=not approve_all,
                )
            )
            dirty = True
            typer.echo(f"  ✓ Approved {c.id}")

        elif action in ("e", "edit"):
            edited = typer.prompt("  Claim", default=c.claim).strip()
            if edited and edited != c.claim:
                object.__setattr__(c, "claim", edited)
                dirty = True
                typer.echo(f"  ✎ Edited claim for {c.id}")
            else:
                typer.echo(f"  — Claim unchanged for {c.id}")

        elif action in ("r", "reject"):
            c.transition_to(
                KnowledgeState.REJECTED,
                reason="Rejected during human review",
                metadata={"approver": reviewer},
            )
            dirty = True
            typer.echo(f"  ✗ Rejected {c.id}")

        elif action in ("q", "quarantine"):
            # Recorded as QUARANTINED, not REJECTED: "needs investigation" and
            # "is wrong" are different outcomes and the audit trail must say
            # which one the reviewer chose.
            c.transition_to(
                KnowledgeState.QUARANTINED,
                reason="Quarantined during human review — needs investigation",
                metadata={"approver": reviewer},
            )
            dirty = True
            typer.echo(f"  ⚠ Quarantined {c.id}")

        elif action in ("t", "test"):
            object.__setattr__(c, "durable_form", "regression-test")
            persist_proposal(c, repo_path, state_dir_opt)
            dirty = True
            typer.echo(f"  ⚙ {c.id} routed to a regression-test proposal")

        elif action in ("c", "question"):
            prompt_text = typer.prompt("  Question", default=f"Is the claim in {c.id} intentional?")
            question = build_question(
                c,
                prompt_text,
                repo_path,
                fallback_owner=Owner(kind="reviewer", value=reviewer),
            )
            new_questions.append(question)
            if KnowledgeState.NEEDS_CLARIFICATION in ALLOWED_TRANSITIONS[c.status]:
                c.transition_to(
                    KnowledgeState.NEEDS_CLARIFICATION,
                    reason=f"Converted to {question.id} during review",
                    metadata={"approver": reviewer},
                )
            dirty = True
            typer.echo(f"  ? {c.id} converted to {question.id}")

        elif action in ("v", "verify"):
            if KnowledgeState.NEEDS_VERIFICATION in ALLOWED_TRANSITIONS[c.status]:
                c.transition_to(
                    KnowledgeState.NEEDS_VERIFICATION,
                    reason="Reviewer requested verification",
                    metadata={"approver": reviewer},
                )
                dirty = True
                typer.echo(f"  ⟳ {c.id} sent for verification")
            else:
                typer.echo(
                    f"  ! {c.id} cannot move to needs-verification from '{c.status.value}'.",
                    err=True,
                )

        elif action in ("n", "scope"):
            current = ", ".join(c.scope.paths)
            narrowed = typer.prompt("  Scope paths (comma-separated)", default=current)
            paths = [p.strip() for p in narrowed.split(",") if p.strip()]
            if set(paths) - set(c.scope.paths):
                # Review may narrow a claim, never widen it: broadening scope
                # past the evidence is the failure the admission policy exists
                # to prevent.
                typer.echo(
                    "  ! Review can only narrow scope. Unchanged.",
                    err=True,
                )
            elif paths != c.scope.paths:
                object.__setattr__(c, "scope", c.scope.model_copy(update={"paths": paths}))
                dirty = True
                typer.echo(f"  ⇲ Scope of {c.id} restricted to {', '.join(paths)}")
            else:
                typer.echo(f"  — Scope unchanged for {c.id}")

        elif action in ("m", "conflict"):
            other = typer.prompt("  Conflicting candidate id", default="")
            explanation = typer.prompt(
                "  Why they conflict", default="Reviewer marked a contradiction"
            )
            candidate_ids = [c.id] + ([other.strip()] if other.strip() else [])
            new_conflicts.append(
                Conflict(
                    id=f"CONFLICT-{_conflict_id(candidate_ids, explanation)}",
                    status="unresolved",
                    candidate_ids=candidate_ids,
                    reason=explanation,
                    publication_blocked=True,
                )
            )
            if KnowledgeState.CONFLICTED in ALLOWED_TRANSITIONS[c.status]:
                c.transition_to(
                    KnowledgeState.CONFLICTED,
                    reason=explanation,
                    metadata={"approver": reviewer},
                )
            dirty = True
            typer.echo(f"  ⚑ Marked {c.id} as conflicted")

        elif action in ("o", "reopen"):
            if KnowledgeState.CANDIDATE in ALLOWED_TRANSITIONS[c.status]:
                justification = typer.prompt("  Why reopen", default="Reviewer reopened")
                c.transition_to(
                    KnowledgeState.CANDIDATE,
                    reason=justification,
                    metadata={"approver": reviewer},
                )
                dirty = True
                typer.echo(f"  ↺ Reopened {c.id}")
            else:
                typer.echo(f"  ! {c.id} cannot be reopened from '{c.status.value}'.", err=True)

        else:
            typer.echo(f"  — Skipped {c.id}")

    if not dirty:
        typer.echo("\nNo changes made.")
        return

    save_state(candidates, candidates_path)
    if new_approvals:
        save_state(existing_approvals + new_approvals, approvals_path)
    if new_conflicts:
        save_state(conflicts + new_conflicts, conflicts_path)
    if new_questions:
        questions_path = state_dir(repo_path, state_dir_opt) / "questions.yaml"
        existing_questions = load_state(Question, questions_path) if questions_path.exists() else []
        save_state(existing_questions + new_questions, questions_path)

    still_published = [
        c.id
        for c in candidates
        if c.status not in PUBLISHABLE_STATES and c.id in published_note_ids(repo_path)
    ]
    typer.echo(f"\nSaved {len(new_approvals)} approval(s), updated candidates.")
    if new_questions:
        typer.echo(f"Recorded {len(new_questions)} question(s) → .coldstart/questions.yaml")
    if new_conflicts:
        typer.echo(f"Recorded {len(new_conflicts)} conflict(s) → .coldstart/conflicts.yaml")
    bulk = [a for a in new_approvals if not a.per_candidate_review]
    if bulk:
        typer.echo(
            f"{len(bulk)} approval(s) recorded without per-candidate review.",
            err=True,
        )
    if still_published:
        typer.echo(
            f"Run 'warmstart publish' to remove {len(still_published)} note(s) "
            f"no longer approved: {', '.join(sorted(still_published))}"
        )


@app.command()
def publish(
    path: Annotated[str, typer.Argument()] = ".",
    state_dir_opt: StateDirOption = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show changes without writing."),
) -> None:
    """Publish approved candidates to AGENTS.md managed section."""
    from coldstart.publication import PublicationError, publish_candidates

    repo_path = Path(path)
    try:
        _changed, message = publish_candidates(
            repo_path, dry_run=dry_run, state_override=state_dir_opt
        )
        typer.echo(message)
    except PublicationError as e:
        typer.echo(f"Publication failed: {e}", err=True)
        raise typer.Exit(code=1) from e


@app.command()
def verify(
    path: Annotated[str, typer.Argument()] = ".", state_dir_opt: StateDirOption = None
) -> None:
    """Verify staleness dependencies.

    Exits 1 when AGENTS.md still publishes a note that is no longer approved,
    because quarantining state on disk does nothing for the cold agent that
    reads the file. Verification does not mutate AGENTS.md itself: publication
    is a separate, human-gated operation.
    """
    from coldstart.publication import PUBLISHABLE_STATES, published_note_ids
    from coldstart.staleness import verify_staleness

    repo_path = Path(path)
    messages = verify_staleness(repo_path, state_dir_opt)
    if messages:
        for msg in messages:
            typer.echo(f"  ⚠ {msg}")
        typer.echo(f"Quarantined {len(messages)} stale candidate(s).")
    else:
        typer.echo("All dependencies verified.")

    published = published_note_ids(repo_path)
    if not published:
        return

    candidates_path = state_dir(repo_path, state_dir_opt) / "candidates.yaml"
    by_id = {
        c.id: c
        for c in (load_state(Candidate, candidates_path) if candidates_path.exists() else [])
    }
    orphaned = sorted(
        note_id
        for note_id in published
        if note_id not in by_id or by_id[note_id].status not in PUBLISHABLE_STATES
    )

    if orphaned:
        typer.echo(
            f"\nAGENTS.md still publishes {len(orphaned)} note(s) that are no longer "
            f"approved: {', '.join(orphaned)}",
            err=True,
        )
        typer.echo("Run 'warmstart publish' to remove them.", err=True)
        raise typer.Exit(code=1)


@app.command()
def questions(
    path: Annotated[str, typer.Argument()] = ".",
    state_dir_opt: StateDirOption = None,
    generate: Annotated[
        bool, typer.Option("--generate", help="Generate questions from current candidates.")
    ] = False,
    answer: Annotated[str | None, typer.Option("--answer", help="Question id to answer.")] = None,
    text: Annotated[
        str | None, typer.Option("--text", help="Answer text, used with --answer.")
    ] = None,
    defer: Annotated[str | None, typer.Option("--defer", help="Question id to defer.")] = None,
    unknown: Annotated[
        str | None, typer.Option("--unknown", help="Question id nobody can answer.")
    ] = None,
    reject: Annotated[
        str | None, typer.Option("--reject", help="Question id whose candidate to reject.")
    ] = None,
    route: Annotated[
        str | None, typer.Option("--route", help="Question id to route to an owner.")
    ] = None,
    to: Annotated[
        str | None, typer.Option("--to", help="Owner to route to, used with --route.")
    ] = None,
) -> None:
    """List, generate, and resolve targeted questions about unresolved intent.

    Deferring or marking a question unknown never promotes its candidate: a
    missing answer is not a confirmation.
    """
    from coldstart.questions import (
        UNRESOLVED_STATUSES,
        apply_answer,
        apply_defer,
        apply_reject,
        apply_route,
        apply_unknown,
        generate_questions,
    )

    repo_path = Path(path)
    questions_path = state_dir(repo_path, state_dir_opt) / "questions.yaml"
    candidates_path = state_dir(repo_path, state_dir_opt) / "candidates.yaml"

    stored = load_state(Question, questions_path) if questions_path.exists() else []
    candidates = load_state(Candidate, candidates_path) if candidates_path.exists() else []
    by_id = {c.id: c for c in candidates}
    actor = os.environ.get("USER") or "unknown"

    if generate:
        fresh = generate_questions(candidates, repo_path, existing=stored)
        known = {q.id for q in stored}
        added = [q for q in fresh if q.id not in known]
        if added:
            save_state(stored + added, questions_path)
            stored = stored + added
            typer.echo(f"Generated {len(added)} question(s) → .coldstart/questions.yaml")
        else:
            typer.echo(
                "No questions generated. A question is only raised when the answer "
                "would change behavior, the repository cannot settle it, and an "
                "owner can be identified."
            )

    mutations = [
        (answer, "answer"),
        (defer, "defer"),
        (unknown, "unknown"),
        (reject, "reject"),
        (route, "route"),
    ]
    requested = [(qid, kind) for qid, kind in mutations if qid]

    if len(requested) > 1:
        typer.echo("Error: apply one action at a time.", err=True)
        raise typer.Exit(code=2)

    if requested:
        question_id, kind = requested[0]
        target = next((q for q in stored if q.id == question_id), None)
        if target is None:
            typer.echo(f"Error: no question with id '{question_id}'.", err=True)
            raise typer.Exit(code=1)

        candidate = by_id.get(target.candidate_id)

        if kind == "answer":
            if not text:
                typer.echo("Error: --answer requires --text.", err=True)
                raise typer.Exit(code=2)
            message = apply_answer(target, candidate, text, actor)
        elif kind == "defer":
            message = apply_defer(target, text or "")
        elif kind == "unknown":
            message = apply_unknown(target)
        elif kind == "reject":
            message = apply_reject(target, candidate, actor)
        else:
            if not to:
                typer.echo("Error: --route requires --to.", err=True)
                raise typer.Exit(code=2)
            message = apply_route(target, to)

        save_state(stored, questions_path)
        if candidates:
            save_state(candidates, candidates_path)
        typer.echo(message)
        return

    if not stored:
        typer.echo("No questions recorded. Run 'warmstart questions --generate' first.")
        return

    open_questions = [q for q in stored if q.status in UNRESOLVED_STATUSES]
    typer.echo(f"{len(open_questions)} unresolved of {len(stored)} question(s):\n")
    for q in sorted(stored, key=lambda q: q.id):
        typer.echo(f"{q.id} [{q.status}]  owner: {q.owner.kind}:{q.owner.value}")
        typer.echo(f"  {q.prompt}")
        typer.echo(f"  Why: {q.reason}")
        typer.echo(f"  Candidate: {q.candidate_id}")
        if q.answer:
            typer.echo(f"  Answer: {q.answer} (by {q.answered_by})")
        typer.echo("")


@app.command()
def harvest(
    source: Annotated[
        str, typer.Option("--source", help="Name of a session source declared in config.")
    ],
    path: Annotated[str, typer.Argument()] = ".",
    state_dir_opt: StateDirOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report findings without writing anything.")
    ] = False,
) -> None:
    """Harvest high-precision events from an authorized session source.

    Session content is untrusted. Raw excerpts are written only to
    access-controlled private provenance; the repository-safe observations
    carry no transcript text and cannot be published without human review.
    """
    from ruamel.yaml import YAML

    from coldstart.harvesting import (
        SourceError,
        authorization_hint,
        harvest_source,
        load_sources,
        provenance_path,
    )
    from coldstart.harvesting.events import extract_events

    repo_path = Path(path)
    config_path = state_dir(repo_path, state_dir_opt) / "config.yaml"

    if not config_path.exists():
        typer.echo("Error: run 'warmstart init' before harvesting.", err=True)
        raise typer.Exit(code=1)

    yaml = YAML(typ="safe")
    with open(config_path) as handle:
        config = yaml.load(handle) or {}

    try:
        sources = load_sources(config)
    except SourceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if source not in sources:
        typer.echo(authorization_hint(source), err=True)
        raise typer.Exit(code=1)

    session_source = sources[source]

    try:
        if dry_run:
            events = extract_events(session_source, repo_path)
            quarantined = [e for e in events if e.quarantined]
            typer.echo(
                f"Dry run: {len(events)} event(s) would be extracted, "
                f"{len(events) - len(quarantined)} observation(s) produced, "
                f"{len(quarantined)} quarantined. Nothing was written."
            )
            for event in events:
                typer.echo(f"  {event.id}  {event.signal}  [{event.sensitivity}]")
            return

        result = harvest_source(session_source, repo_path, state_dir_opt)
    except SourceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(result.summary)

    if result.observations:
        obs_path = state_dir(repo_path, state_dir_opt) / "observations.yaml"
        existing = load_state(Observation, obs_path) if obs_path.exists() else []
        known = {o.id for o in existing}
        added = [o for o in result.observations if o.id not in known]
        if added:
            save_state(existing + added, obs_path)
            typer.echo(f"Added {len(added)} observation(s) → {obs_path}")

    typer.echo(f"Private provenance: {provenance_path(repo_path, state_dir_opt)} (owner-only)")

    for event in result.quarantined:
        typer.echo(f"  ⚠ {event.id}: {event.quarantine_reason}", err=True)

    flagged = [e for e in result.events if e.secret_findings]
    for event in flagged:
        kinds = ", ".join(sorted({f.kind for f in event.secret_findings}))
        typer.echo(f"  ⚠ {event.id}: sensitive content detected ({kinds})", err=True)

    typer.echo(
        "\nA scan that found nothing is not proof that content is safe. "
        "Review private provenance before approving any session-derived candidate."
    )
    typer.echo("Next: warmstart compile, then warmstart review.")


if __name__ == "__main__":
    app()

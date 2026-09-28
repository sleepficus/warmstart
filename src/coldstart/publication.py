"""Safe publication of approved candidates to AGENTS.md.

Publication validates the full canonical approval invariant before writing.
It fails closed on malformed or duplicate managed sections.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from coldstart.domain import (
    PUBLISHABLE_DISTRIBUTIONS,
    ApprovalRecord,
    Candidate,
    Conflict,
    KnowledgeState,
    PublishedKnowledge,
)
from coldstart.staleness import verify_file_dependency
from coldstart.storage import atomic_write_text, load_state, save_state, state_dir

MANAGED_START = "<!-- warmstart:start -->"
MANAGED_END = "<!-- warmstart:end -->"
LEGACY_MANAGED_START = "<!-- coldstart:start -->"
LEGACY_MANAGED_END = "<!-- coldstart:end -->"

# Hard note budget. The specification requires a budget but does not fix a
# number; ten keeps the managed section inside a cold agent's context budget,
# which is the constraint the budget exists to serve.
NOTE_BUDGET = 10

# States whose notes belong in the managed section.
PUBLISHABLE_STATES = {KnowledgeState.APPROVED, KnowledgeState.PUBLISHED}


class PublicationError(Exception):
    """Raised when publication fails due to a validation error."""


def _managed_markers(content: str) -> tuple[str, str]:
    """Return the active marker pair, retaining a legacy block in place.

    A file may contain either the WarmStart markers or the former Coldstart
    markers.  Mixing both forms is ambiguous and is refused before writing.
    """
    current = MANAGED_START in content or MANAGED_END in content
    legacy = LEGACY_MANAGED_START in content or LEGACY_MANAGED_END in content
    if current and legacy:
        raise PublicationError(
            "Both WarmStart and legacy Coldstart managed markers are present. "
            "Keep one complete marker pair before publishing."
        )
    return (LEGACY_MANAGED_START, LEGACY_MANAGED_END) if legacy else (MANAGED_START, MANAGED_END)


def _validate_managed_section(content: str) -> tuple[str, str]:
    """Check that the active managed section is well-formed and unique."""
    start_marker, end_marker = _managed_markers(content)
    start_count = content.count(start_marker)
    end_count = content.count(end_marker)

    if start_count > 1 or end_count > 1:
        raise PublicationError(
            f"Duplicate managed section markers: {start_count} start, "
            f"{end_count} end. Refusing to publish to avoid data loss."
        )

    if start_count != end_count:
        raise PublicationError(
            f"Malformed managed section: {start_count} start markers, "
            f"{end_count} end markers. Fix AGENTS.md before publishing."
        )

    if start_count == 1:
        start_pos = content.index(start_marker)
        end_pos = content.index(end_marker)
        if end_pos < start_pos:
            raise PublicationError(
                "Malformed managed section: end marker appears before "
                "start marker. Fix AGENTS.md before publishing."
            )
    return start_marker, end_marker


def published_note_ids(repo_path: Path) -> set[str]:
    """Return the candidate ids currently published in the managed section.

    Reads the agent-facing file rather than WarmStart's own state, because the
    question this answers is "what is a cold agent being told right now?" —
    which `.coldstart/` cannot answer once the two have diverged.
    """
    agents_md = repo_path / "AGENTS.md"
    if not agents_md.exists():
        return set()

    content = agents_md.read_text()
    try:
        start_marker, end_marker = _validate_managed_section(content)
    except PublicationError:
        return set()
    if start_marker not in content or end_marker not in content:
        return set()

    start = content.index(start_marker)
    end = content.index(end_marker)
    if end < start:
        return set()

    section = content[start:end]
    return set(re.findall(r"\[([^\[\]\s]+)\]\s*$", section, flags=re.MULTILINE))


def _check_approval(
    candidate: Candidate,
    approvals: list[ApprovalRecord],
) -> str | None:
    """Return an error message if the candidate lacks a valid approval, else None."""
    matching = [a for a in approvals if a.candidate_id == candidate.id]
    if not matching:
        return f"{candidate.id}: no approval record found"

    # Find the most recent approval
    latest_approval = max(matching, key=lambda a: a.approved_at)

    # Check that the approval post-dates the candidate's transition to APPROVED
    approved_transitions = [
        t for t in candidate.transition_history if t.to_state == KnowledgeState.APPROVED
    ]
    if not approved_transitions:
        return f"{candidate.id}: no APPROVED transition in history"

    latest_transition = max(approved_transitions, key=lambda t: t.timestamp)
    if latest_approval.approved_at < latest_transition.timestamp:
        return f"{candidate.id}: approval record predates the transition to APPROVED"

    return None


def _check_staleness(
    candidate: Candidate,
    workspace_path: Path,
) -> str | None:
    """Return an error message if any dependency is stale, else None."""
    for dep in candidate.depends_on:
        is_valid, reason = verify_file_dependency(dep, workspace_path)
        if not is_valid:
            return f"{candidate.id}: stale dependency — {reason}"
    return None


def _render_managed_section(
    candidates: list[Candidate],
    start_marker: str = MANAGED_START,
    end_marker: str = MANAGED_END,
) -> str:
    """Render the managed section content deterministically."""
    # Sort by ID for deterministic output
    sorted_candidates = sorted(candidates, key=lambda c: c.id)

    lines = [start_marker]
    for c in sorted_candidates:
        form_suffix = ""
        if c.durable_form:
            form_suffix = f" (form: {c.durable_form})"
        lines.append(f"- **{c.kind.title()}**: {c.claim}{form_suffix} [{c.id}]")
    lines.append(end_marker)
    return "\n".join(lines)


def _write_published_records(
    published: list[Candidate],
    approvals: list[ApprovalRecord],
    repo_path: Path,
    state_override: Path | None = None,
) -> None:
    """Record what is currently live, separately from the candidate pipeline.

    This is the Phase 1 `Published knowledge` object: it answers "what is in
    the agent-facing file, who approved it, and when was it last confirmed"
    without having to replay transition history.
    """
    by_candidate: dict[str, ApprovalRecord] = {}
    for approval in approvals:
        prior = by_candidate.get(approval.candidate_id)
        if prior is None or approval.approved_at > prior.approved_at:
            by_candidate[approval.candidate_id] = approval

    now = datetime.now(UTC)
    records = [
        PublishedKnowledge(
            id=c.id,
            status=KnowledgeState.PUBLISHED,
            durable_form=c.durable_form or "agent-note",
            approved_by=by_candidate[c.id].approver,
            approved_at=by_candidate[c.id].approved_at,
            last_verified_at=now,
        )
        for c in sorted(published, key=lambda c: c.id)
        if c.id in by_candidate
    ]
    save_state(records, state_dir(repo_path, state_override) / "published.yaml")


def publish_candidates(
    repo_path: Path,
    *,
    dry_run: bool = False,
    state_override: Path | None = None,
) -> tuple[bool, str]:
    """Publish approved candidates to AGENTS.md managed section.

    Returns (changed: bool, message: str).
    Raises PublicationError on validation failures.
    """
    state = state_dir(repo_path, state_override)
    candidates_path = state / "candidates.yaml"
    conflicts_path = state / "conflicts.yaml"
    approvals_path = state / "approvals.yaml"
    agents_md = repo_path / "AGENTS.md"

    if not candidates_path.exists():
        return False, "No candidates file found."

    candidates = load_state(Candidate, candidates_path)
    approvals = load_state(ApprovalRecord, approvals_path) if approvals_path.exists() else []
    conflicts = load_state(Conflict, conflicts_path) if conflicts_path.exists() else []

    # Identify blocked candidate IDs from unresolved conflicts
    blocked_ids: set[str] = set()
    for conf in conflicts:
        if conf.publication_blocked:
            blocked_ids.update(conf.candidate_ids)

    # PUBLISHED candidates are re-rendered so that republishing is idempotent
    # rather than emptying the section on the second run.
    approved = [c for c in candidates if c.status in PUBLISHABLE_STATES]

    # Validate each candidate against the full canonical invariant
    publishable: list[Candidate] = []
    errors: list[str] = []

    for c in approved:
        # Check distribution
        if c.distribution not in PUBLISHABLE_DISTRIBUTIONS:
            errors.append(f"{c.id}: distribution '{c.distribution}' is not publishable")
            continue

        # Check conflicts
        if c.id in blocked_ids:
            errors.append(f"{c.id}: blocked by unresolved conflict")
            continue

        # Check approval record
        approval_error = _check_approval(c, approvals)
        if approval_error:
            errors.append(approval_error)
            continue

        # Check staleness
        staleness_error = _check_staleness(c, repo_path)
        if staleness_error:
            errors.append(staleness_error)
            continue

        publishable.append(c)

    # Budget. Truncation is reported, never silent: a dropped note reads
    # exactly like a note that was never proposed.
    if len(publishable) > NOTE_BUDGET:
        dropped = publishable[NOTE_BUDGET:]
        publishable = publishable[:NOTE_BUDGET]
        errors.append(
            f"note budget of {NOTE_BUDGET} exceeded; {len(dropped)} note(s) held back: "
            + ", ".join(c.id for c in dropped)
        )

    # Validate existing AGENTS.md
    if agents_md.exists():
        content = agents_md.read_text()
        start_marker, end_marker = _validate_managed_section(content)
    else:
        content = ""
        start_marker, end_marker = MANAGED_START, MANAGED_END

    # Render with the existing pair when updating a legacy installation. New
    # installations use WarmStart markers.
    new_section = _render_managed_section(publishable, start_marker, end_marker)

    # Compute new content. The replacement is passed as a function because a
    # claim containing a backslash escape (\1, \g<0>) would otherwise be
    # interpreted by re.sub as a group reference and either corrupt the
    # section or raise re.error.
    if start_marker in content and end_marker in content:
        new_content = re.sub(
            re.escape(start_marker) + r".*?" + re.escape(end_marker),
            lambda _match: new_section,
            content,
            flags=re.DOTALL,
        )
    elif content:
        new_content = content.rstrip() + "\n\n" + new_section + "\n"
    else:
        new_content = new_section + "\n"

    # Dry-run: report the diff without mutating anything, including state.
    if dry_run:
        msg = f"Dry run: would publish {len(publishable)} candidates."
        if errors:
            msg += f" {len(errors)} excluded: {'; '.join(errors)}"
        msg += f"\n\n{new_content}"
        return False, msg

    changed = not agents_md.exists() or agents_md.read_text() != new_content
    if changed:
        atomic_write_text(agents_md, new_content)

    # Record publication in the domain state, so that PUBLISHED is a state the
    # system actually reaches and `verify` can distinguish a live note from an
    # approved-but-unpublished one. Done regardless of whether the file needed
    # rewriting, so that a hand-identical section still settles the state.
    newly_published = [c for c in publishable if c.status == KnowledgeState.APPROVED]
    for c in newly_published:
        c.transition_to(
            KnowledgeState.PUBLISHED,
            reason="Published into the AGENTS.md managed section.",
        )
    if newly_published:
        save_state(candidates, candidates_path)
        _write_published_records(publishable, approvals, repo_path, state_override)

    if not changed:
        msg = "No changes to publish."
        if errors:
            msg += f" ({len(errors)} candidates excluded: {'; '.join(errors)})"
        return False, msg

    msg = f"Published {len(publishable)} candidates."
    if errors:
        msg += f" {len(errors)} excluded: {'; '.join(errors)}"
    return True, msg

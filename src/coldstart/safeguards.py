"""Durable-form recommendation and proposal persistence.

For every admitted candidate, determines the strongest suitable
representation from the knowledge hierarchy and persists the proposal.
"""

from pathlib import Path

from ruamel.yaml import YAML

from coldstart.domain import Candidate
from coldstart.storage import state_dir

# Knowledge hierarchy from strongest to weakest
DURABLE_FORM_HIERARCHY = [
    "regression-test",
    "assertion",
    "linter-rule",
    "contract-reference",
    "agent-note",
    "open-question",
]


def recommend_durable_form(candidate: Candidate) -> str:
    """Recommend the strongest suitable durable form for a candidate.

    Rules:
    - Type inconsistencies (invariants with high severity) → regression-test
    - Generated-file constraints → agent-note (can't be tested generically)
    - Ownership claims → agent-note
    - External integrations → agent-note
    - Anything with 'test' already in scope → assertion
    - Default → agent-note
    """
    if candidate.kind == "invariant" and candidate.operational_effect.severity == "high":
        return "regression-test"

    if candidate.kind == "constraint" and "generated" in candidate.claim.lower():
        return "agent-note"

    if candidate.kind == "ownership":
        return "agent-note"

    if candidate.kind == "integration":
        return "agent-note"

    return "agent-note"


def _assumptions(candidate: Candidate) -> list[str]:
    """State what the proposal takes for granted.

    A proposal with an empty assumptions list reads as "nothing was assumed",
    which for a deterministically-derived candidate is never true.
    """
    assumptions = [
        "The claim was derived from a deterministic repository pattern, not from "
        "verified intent. A human must confirm it describes a real constraint.",
    ]

    if candidate.confidence != "high":
        assumptions.append(
            f"Confidence is '{candidate.confidence}': the evidence supports the "
            f"pattern but not necessarily the generalization."
        )
    if not candidate.scope.environments:
        assumptions.append(
            "No environment scope was determined; the claim is assumed to apply "
            "to all environments until narrowed."
        )
    if not candidate.scope.authorities:
        assumptions.append(
            "No external authority was identified; if one owns this behavior, "
            "the claim may be superseded without warning."
        )
    return assumptions


def _verification_commands(candidate: Candidate, form: str) -> list[str]:
    """Commands a reviewer can run to check the proposal for themselves."""
    commands = [
        f"warmstart verify  # re-checks the {len(candidate.depends_on)} declared "
        f"dependency/dependencies"
    ]
    for path in candidate.scope.paths:
        commands.append(f"git log --oneline -- {path}")
    if form == "regression-test":
        commands.append("pytest  # must fail before the safeguard, pass after")
    return commands


def persist_proposal(
    candidate: Candidate, repo_path: Path, state_override: Path | None = None
) -> Path:
    """Write a proposal to .coldstart/proposals/{type}/{candidate_id}.yaml.

    Returns the path to the created proposal file.
    """
    form = candidate.durable_form or recommend_durable_form(candidate)

    # Map durable forms to proposal directories
    dir_map = {
        "regression-test": "tests",
        "assertion": "assertions",
        "linter-rule": "policies",
        "contract-reference": "contracts",
        "agent-note": "notes",
        "open-question": "questions",
    }

    subdir = dir_map.get(form, "notes")
    proposals_dir = state_dir(repo_path, state_override) / "proposals" / subdir
    proposals_dir.mkdir(parents=True, exist_ok=True)

    proposal_path = proposals_dir / f"{candidate.id}.yaml"

    proposal = {
        "candidate_id": candidate.id,
        "durable_form": form,
        "claim": candidate.claim,
        "intended_behavior": candidate.operational_effect.changes,
        "avoided_failure": candidate.operational_effect.avoids,
        "affected_files": candidate.scope.paths,
        "evidence": [e if isinstance(e, dict) else e.model_dump() for e in candidate.evidence],
        "assumptions": _assumptions(candidate),
        "verification_commands": _verification_commands(candidate, form),
    }

    if form == "regression-test":
        proposal["expected_failure_without"] = (
            f"Without this test, {candidate.operational_effect.avoids}"
        )
        proposal["expected_success_after"] = f"Test enforces: {candidate.claim}"

    yaml = YAML()
    with open(proposal_path, "w") as f:
        yaml.dump(proposal, f)

    return proposal_path

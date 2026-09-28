"""High-precision event extraction and the provenance boundary.

Only two signals are extracted, both from the specification:

1. an agent stated an expectation and a subsequent check contradicted it;
2. a human corrected a factual assertion the agent made.

Everything else in a transcript is explicitly rejected. The anti-signals are
implemented as an active filter rather than left to the detectors to miss,
because the failure mode that kills this product is a harvest full of style
preferences and task steering.

## The boundary

The raw excerpt stays in private provenance. What crosses into repository-safe
territory is deliberately almost nothing: the fact that an event of a given
kind occurred, and any file path the excerpt mentioned **that the repository
independently confirms exists**. Free text never crosses.

That is a strong constraint and it makes the automated output modest on
purpose. Extraction cannot safely author a claim from untrusted text; it can
only tell a human where to look. The human writes the claim during review.
"""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from coldstart.domain import (
    Observation,
    ObservationData,
    SessionEvent,
    Source,
    SourceKind,
    max_sensitivity,
)
from coldstart.harvesting.safety import escalated_sensitivity, scan_injection, scan_secrets
from coldstart.harvesting.sources import SessionSource, Turn, read_turns

SIGNAL_EXPECTATION_CONTRADICTED = "expectation-contradicted"
SIGNAL_HUMAN_CORRECTION = "human-correction"

# How far ahead of an expectation a contradicting check may appear.
_CONTRADICTION_WINDOW = 4

_EXPECTATION = re.compile(
    r"\b(i expect|i'd expect|we expect|this should|that should|should (?:now )?(?:be|work|pass|"
    r"succeed|return)|presumably|it will (?:be|work|pass|return)|must already)\b",
    re.IGNORECASE,
)

_CORRECTION = re.compile(
    r"(^|[.\n]\s*)(no[,.\s]|actually[,\s]|that'?s (?:not right|wrong|incorrect|false)|"
    r"that isn'?t|it'?s not|you'?re wrong|not true|incorrect[,.\s]|wrong[,.\s]|"
    r"nope[,.\s]|that'?s backwards)",
    re.IGNORECASE,
)

# Anti-signals from the specification. A turn matching any of these is rejected
# even when it also matches a correction pattern: "no, use tabs instead" is a
# preference wearing a correction's clothes.
_ANTI_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    (
        "preference",
        re.compile(
            r"\b(i prefer|i like|please use|let'?s use|i'd rather|"
            r"style|formatting|prettier|naming convention)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "steering",
        # No trailing \b: several alternatives end in punctuation, and a word
        # boundary after a comma can never match.
        re.compile(
            r"\b(next[,\s]|now (?:do|let'?s|go)\b|then do\b|after that\b|"
            r"move on to\b|start with\b|continue with\b|go ahead\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "effort-instruction",
        re.compile(
            r"\b(ultrathink|think harder|be (?:more )?thorough|take your time|"
            r"more effort|reasoning effort|be (?:more )?concise|be (?:more )?brief)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "task-sequencing",
        # As above: the comma-terminated alternatives must not be followed by \b.
        re.compile(
            r"\b(first,|second,|third,|finally,|step \d|"
            r"todo\b|task list\b|checklist\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "speculation",
        re.compile(
            r"\b(what if|maybe we|perhaps we|could we|i wonder|"
            r"brainstorm|might be worth|thinking out loud)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "copied-log",
        re.compile(
            r"(^|\n)\s*(\$ |traceback \(most recent|"
            r"\d{4}-\d{2}-\d{2}t?\d{2}:\d{2})",
            re.IGNORECASE,
        ),
    ),
]

# Paths mentioned in prose. Deliberately conservative: a token that looks like a
# path only matters if the repository confirms it, so false positives here are
# discarded by corroboration rather than published.
_PATH_TOKEN = re.compile(r"\b[\w./-]*[\w-]+\.[A-Za-z0-9]{1,6}\b")

_MAX_EXCERPT = 1200


def _event_id(session_id: str, signal: str, text: str) -> str:
    digest = hashlib.sha256(f"{session_id}\n{signal}\n{text}".encode()).hexdigest()[:12]
    return f"SESSION-{digest}"


def _matched_anti_signals(text: str) -> list[str]:
    return [name for name, pattern in _ANTI_SIGNALS if pattern.search(text)]


def corroborate_paths(text: str, repo_path: Path) -> list[str]:
    """Return path-like tokens from `text` that actually exist in the repository.

    This is the only channel by which session content reaches repository-safe
    output, and it is one the repository itself validates. A path the repo does
    not have is discarded, so a transcript cannot introduce a reference to
    something that is not there.
    """
    found: list[str] = []
    for token in dict.fromkeys(_PATH_TOKEN.findall(text)):
        candidate = token.lstrip("./")
        if not candidate or candidate.startswith("/"):
            continue
        try:
            resolved = (repo_path / candidate).resolve()
            root = repo_path.resolve()
        except (OSError, ValueError):
            continue
        # Reject traversal outside the repository even if such a path exists.
        if not resolved.is_relative_to(root):
            continue
        if resolved.exists():
            found.append(candidate)
    return sorted(found)


def _build_event(
    source: SessionSource,
    signal: str,
    turn: Turn,
    excerpt: str,
    repo_path: Path,
) -> SessionEvent:
    excerpt = excerpt[:_MAX_EXCERPT]
    injection = scan_injection(excerpt)
    secrets = scan_secrets(excerpt)
    sensitivity = escalated_sensitivity(max_sensitivity(source.sensitivity), secrets)

    quarantined = bool(injection)
    reason = None
    if injection:
        kinds = ", ".join(sorted({f.kind for f in injection}))
        reason = (
            f"Possible indirect prompt injection ({kinds}). Held for human "
            f"review; no candidate was produced."
        )

    return SessionEvent(
        id=_event_id(turn.session_id, signal, excerpt),
        session_id=turn.session_id,
        source_name=source.name,
        source_path=str(source.path),
        record_uuid=turn.uuid,
        signal=signal,
        speaker="human" if turn.is_human_authored else "agent",
        captured_at=datetime.now(UTC),
        occurred_at=turn.timestamp,
        raw_excerpt=excerpt,
        sensitivity=sensitivity,
        injection_findings=injection,
        secret_findings=secrets,
        secret_scan_performed=True,
        corroborated_paths=corroborate_paths(excerpt, repo_path),
        quarantined=quarantined,
        quarantine_reason=reason,
    )


def extract_events(source: SessionSource, repo_path: Path) -> list[SessionEvent]:
    """Extract high-precision events from an authorized session source."""
    turns = list(read_turns(source))
    events: list[SessionEvent] = []
    seen: set[str] = set()

    for position, turn in enumerate(turns):
        signal: str | None = None
        excerpt = turn.text

        if turn.role == "assistant" and not turn.is_tool_result:
            if _EXPECTATION.search(turn.text):
                # An expectation only counts once something checked it and
                # disagreed. Ordinary red-green development noise is worthless.
                window = turns[position + 1 : position + 1 + _CONTRADICTION_WINDOW]
                contradiction = next(
                    (t for t in window if t.is_tool_result and t.tool_failed), None
                )
                if contradiction is not None:
                    signal = SIGNAL_EXPECTATION_CONTRADICTED
                    excerpt = f"{turn.text}\n\n--- contradicted by ---\n{contradiction.text}"

        elif turn.is_human_authored and not turn.is_tool_result and _CORRECTION.search(turn.text):
            # A correction must be correcting something the agent said.
            prior_agent = next(
                (
                    t
                    for t in reversed(turns[:position])
                    if t.role == "assistant" and not t.is_tool_result and t.text
                ),
                None,
            )
            if prior_agent is not None:
                signal = SIGNAL_HUMAN_CORRECTION
                excerpt = f"{prior_agent.text}\n\n--- corrected by human ---\n{turn.text}"

        if signal is None:
            continue

        anti = _matched_anti_signals(turn.text)
        if anti:
            continue

        event = _build_event(source, signal, turn, excerpt, repo_path)
        if event.id in seen:
            continue
        seen.add(event.id)
        events.append(event)

    return events


def to_observation(event: SessionEvent) -> Observation:
    """Derive the repository-safe observation for an event.

    Carries no transcript text. The summary is a template plus, at most, paths
    the repository confirmed. It points a reviewer at the private provenance
    record rather than restating what the transcript said.
    """
    if event.corroborated_paths:
        subject = ", ".join(event.corroborated_paths[:3])
        subject_clause = f"concerning {subject}"
    else:
        subject_clause = "whose subject the repository could not corroborate"

    if event.signal == SIGNAL_EXPECTATION_CONTRADICTED:
        description = (
            f"During an agent session, a stated expectation {subject_clause} was "
            f"contradicted by a subsequent check."
        )
    else:
        description = (
            f"During an agent session, a human corrected an agent's factual "
            f"assertion {subject_clause}."
        )

    return Observation(
        id=f"OBS-{event.id}",
        source=Source(kind=SourceKind.SESSION, reference=event.id),
        observed=ObservationData(
            kind=f"session-{event.signal}",
            summary=(
                f"{description} The transcript text is not reproduced here; read "
                f"private provenance record {event.id} to write the claim."
            ),
        ),
        captured_at=event.captured_at,
        sensitivity=event.sensitivity,
    )

"""Session harvesting (specification Phase 6).

Session transcripts are untrusted, potentially private, and potentially
adversarial. The design consequence is that harvesting produces *pointers for a
human*, not finished knowledge: raw excerpts stay in access-controlled private
provenance, and the only session content that reaches repository-safe output is
a file path the repository itself confirms exists.

Public entry point: `harvest_source`.
"""

from pathlib import Path

from coldstart.domain import Observation, SessionEvent
from coldstart.harvesting.events import (
    SIGNAL_EXPECTATION_CONTRADICTED,
    SIGNAL_HUMAN_CORRECTION,
    extract_events,
    to_observation,
)
from coldstart.harvesting.provenance import (
    is_secure,
    load_events,
    provenance_path,
    purge,
    store_events,
)
from coldstart.harvesting.sources import (
    CLAUDE_CODE,
    SUPPORTED_FORMATS,
    SessionSource,
    SourceError,
    authorization_hint,
    load_sources,
)

__all__ = [
    "CLAUDE_CODE",
    "SIGNAL_EXPECTATION_CONTRADICTED",
    "SIGNAL_HUMAN_CORRECTION",
    "SUPPORTED_FORMATS",
    "HarvestResult",
    "SessionSource",
    "SourceError",
    "authorization_hint",
    "harvest_source",
    "is_secure",
    "load_events",
    "load_sources",
    "provenance_path",
    "purge",
]


class HarvestResult:
    """What a harvest produced, separated by trust boundary."""

    def __init__(
        self,
        events: list[SessionEvent],
        stored: list[SessionEvent],
        expired: list[SessionEvent],
        observations: list[Observation],
        quarantined: list[SessionEvent],
    ) -> None:
        self.events = events
        self.stored = stored
        self.expired = expired
        self.observations = observations
        self.quarantined = quarantined

    @property
    def summary(self) -> str:
        parts = [
            f"{len(self.events)} event(s) extracted",
            f"{len(self.stored)} new private provenance record(s)",
            f"{len(self.observations)} repository-safe observation(s)",
        ]
        if self.quarantined:
            parts.append(f"{len(self.quarantined)} quarantined for possible injection")
        if self.expired:
            parts.append(f"{len(self.expired)} expired by retention")
        return "; ".join(parts)


def harvest_source(
    source: SessionSource, repo_path: Path, state_override: Path | None = None
) -> HarvestResult:
    """Harvest one authorized source into private provenance plus observations.

    A quarantined event is stored — losing evidence of an attempted injection
    would be its own failure — but produces no observation, so it cannot reach
    a candidate without a human reading the private record first.
    """
    events = extract_events(source, repo_path)
    stored, expired = store_events(
        repo_path, events, source.retention_days, state_override=state_override
    )

    quarantined = [event for event in events if event.quarantined]
    observations = [to_observation(event) for event in events if not event.quarantined]

    return HarvestResult(
        events=events,
        stored=stored,
        expired=expired,
        observations=observations,
        quarantined=quarantined,
    )

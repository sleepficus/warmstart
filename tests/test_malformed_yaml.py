"""Tests for malformed YAML handling.

Covers:
- Truncated YAML files
- Invalid status values
- Missing required fields
- Empty files
- Corrupted transition history
"""

import tempfile
from pathlib import Path

import pytest

from coldstart.domain import Candidate, KnowledgeState, OperationalEffect, Scope
from coldstart.storage import load_state, save_state


class TestMalformedYaml:
    """Verify graceful handling of corrupted state files."""

    def test_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidates.yaml"
            path.write_text("")
            result = load_state(Candidate, path)
            assert result == []

    def test_truncated_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidates.yaml"
            path.write_text("- id: TEST-001\n  claim: 'incomplete\n")
            with pytest.raises(ValueError, match="Failed to load"):
                load_state(Candidate, path)

    def test_invalid_status_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidates.yaml"
            path.write_text(
                "- id: TEST-001\n"
                "  claim: test\n"
                "  status: INVALID_STATUS\n"
                "  kind: invariant\n"
                "  confidence: high\n"
                "  derived_from: []\n"
                "  evidence: []\n"
                "  operational_effect: {changes: x, avoids: y, severity: z}\n"
                "  scope: {paths: [], environments: [], authorities: []}\n"
                "  depends_on: []\n"
                "  distribution: internal\n"
                "  transition_history: []\n"
            )
            with pytest.raises(ValueError, match="Failed to load"):
                load_state(Candidate, path)

    def test_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidates.yaml"
            path.write_text("- id: TEST-001\n  claim: test\n")
            with pytest.raises(ValueError, match="Failed to load"):
                load_state(Candidate, path)

    def test_corrupted_transition_history(self) -> None:
        """Status says APPROVED but history says REJECTED → caught by validator."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidates.yaml"
            path.write_text(
                "- id: TEST-001\n"
                "  claim: test\n"
                "  status: approved\n"
                "  kind: invariant\n"
                "  confidence: high\n"
                "  derived_from: []\n"
                "  evidence: []\n"
                "  operational_effect: {changes: x, avoids: y, severity: z}\n"
                "  scope: {paths: [], environments: [], authorities: []}\n"
                "  depends_on: []\n"
                "  distribution: internal\n"
                "  transition_history:\n"
                "    - from_state: candidate\n"
                "      to_state: rejected\n"
                "      timestamp: '2026-01-01T00:00:00Z'\n"
                "      reason: bad\n"
            )
            with pytest.raises(ValueError, match="Failed to load"):
                load_state(Candidate, path)

    def test_nonexistent_file(self) -> None:
        path = Path("/nonexistent/path/candidates.yaml")
        result = load_state(Candidate, path)
        assert result == []

    def test_valid_roundtrip(self) -> None:
        """Valid data survives save/load roundtrip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidates.yaml"
            c = Candidate(
                id="RT-001",
                claim="Roundtrip test",
                status=KnowledgeState.CANDIDATE,
                kind="invariant",
                confidence="high",
                derived_from=["OBS-1"],
                evidence=[{"kind": "observation", "source": "test.py"}],
                operational_effect=OperationalEffect(changes="x", avoids="y", severity="z"),
                scope=Scope(paths=["src/"], environments=[], authorities=[]),
                depends_on=[],
                distribution="internal",
            )
            save_state([c], path)
            loaded = load_state(Candidate, path)
            assert len(loaded) == 1
            assert loaded[0].id == "RT-001"
            assert loaded[0].status == KnowledgeState.CANDIDATE

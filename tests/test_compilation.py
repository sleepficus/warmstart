"""Tests for the compilation pipeline."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from coldstart.compilation import (
    check_actionability,
    check_derivability,
    check_evidence_sufficiency,
    compile_candidates,
    detect_conflicts,
    merge_candidates,
)
from coldstart.domain import (
    Candidate,
    KnowledgeState,
    Observation,
    ObservationData,
    OperationalEffect,
    Scope,
    Source,
    SourceKind,
)


def _make_obs(kind: str = "type-inconsistency", ref: str = "test.py") -> Observation:
    return Observation(
        id=f"OBS-{kind}-{ref}-1",
        source=Source(kind=SourceKind.REPOSITORY, reference=ref),
        observed=ObservationData(kind=kind, summary=f"Test {kind} observation."),
        captured_at=datetime.now(UTC),
        sensitivity="low",
    )


def _make_candidate(
    *,
    id: str = "CS-001",
    claim: str = "Test",
    status: KnowledgeState = KnowledgeState.CANDIDATE,
    kind: str = "invariant",
    evidence: list | None = None,
    operational_effect: OperationalEffect | None = None,
    scope: Scope | None = None,
    depends_on: list | None = None,
    distribution: str = "internal",
) -> Candidate:
    return Candidate(
        id=id,
        claim=claim,
        status=status,
        kind=kind,
        confidence="high",
        derived_from=[],
        evidence=evidence
        if evidence is not None
        else [{"kind": "observation", "source": "test.py"}],
        operational_effect=operational_effect
        or OperationalEffect(changes="x", avoids="y", severity="high"),
        scope=scope or Scope(paths=[], environments=[], authorities=[]),
        depends_on=depends_on or [],
        distribution=distribution,
    )


class TestEvidenceSufficiency:
    def test_pass_with_evidence(self) -> None:
        c = _make_candidate(evidence=[{"kind": "test"}])
        assert check_evidence_sufficiency(c)["result"] == "pass"

    def test_reject_without_evidence(self) -> None:
        c = _make_candidate(evidence=[])
        assert check_evidence_sufficiency(c)["result"] == "reject"


class TestDerivability:
    def test_pass_without_repo(self) -> None:
        c = _make_candidate()
        result = check_derivability(c, None)
        assert result["result"] == "uncertain"

    def test_rejects_a_subject_the_readme_already_covers(self) -> None:
        """The specification's test is about the subject, not the wording.

        The previous version compared claim words against README text at a 70%
        threshold. Measured across ten real repositories it rejected 0 of 165
        candidates, because every claim is generated from a template whose
        phrasing appears in no README ever written.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "README.md").write_text(
                "Identifiers are handled in `src/importer.py`; see that module.\n"
            )
            c = _make_candidate(
                claim="File src/importer.py zero-pads values using format spec :0Nd.",
                scope=Scope(paths=["src/importer.py"], environments=[], authorities=[]),
            )

            result = check_derivability(c, repo)

            assert result["result"] == "reject"
            assert "orientation documents" in result["reason"]

    def test_admits_a_subject_no_document_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "README.md").write_text("A project that does some things.\n")
            c = _make_candidate(
                scope=Scope(paths=["src/undocumented.py"], environments=[], authorities=[])
            )

            assert check_derivability(c, repo)["result"] == "pass"

    def test_agent_instruction_files_count_as_orientation(self) -> None:
        """A fact already in AGENTS.md is one the agent will already have read."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "AGENTS.md").write_text("Never edit `generated/rates.py` by hand.\n")
            c = _make_candidate(
                scope=Scope(paths=["generated/rates.py"], environments=[], authorities=[])
            )

            assert check_derivability(c, repo)["result"] == "reject"

    def test_uncertain_when_there_are_no_orientation_documents(self) -> None:
        """Absence of documentation is not evidence that a fact is novel."""
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _make_candidate(
                scope=Scope(paths=["src/thing.py"], environments=[], authorities=[])
            )
            assert check_derivability(c, Path(tmpdir))["result"] == "uncertain"

    def test_short_basenames_do_not_match_coincidentally(self) -> None:
        """`api.py` appearing as a word fragment must not reject the candidate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "README.md").write_text("We expose a public api for consumers.\n")
            c = _make_candidate(
                scope=Scope(paths=["deep/nested/api.py"], environments=[], authorities=[])
            )

            assert check_derivability(c, repo)["result"] == "pass"


class TestActionability:
    def test_high_severity_is_actionable(self) -> None:
        c = _make_candidate()
        assert check_actionability(c)["result"] == "pass"

    def test_rejects_when_no_failure_is_avoided(self) -> None:
        c = _make_candidate(
            operational_effect=OperationalEffect(changes="x", avoids="   ", severity="low")
        )
        result = check_actionability(c)
        assert result["result"] == "reject"
        assert "avoid" in result["reason"].lower()

    def test_rejects_when_no_action_changes(self) -> None:
        c = _make_candidate(
            operational_effect=OperationalEffect(changes="", avoids="y", severity="low")
        )
        assert check_actionability(c)["result"] == "reject"

    def test_rejects_structural_candidate_kinds(self) -> None:
        c = _make_candidate(kind="inventory")
        assert check_actionability(c)["result"] == "reject"


class TestConflictDetection:
    def test_no_conflict_for_single_candidate(self) -> None:
        c = _make_candidate()
        assert detect_conflicts([c]) == []

    def test_conflict_for_incompatible_prescriptions(self) -> None:
        c1 = _make_candidate(
            id="CS-001",
            claim="Ids are opaque strings",
            operational_effect=OperationalEffect(
                changes="Preserve the id as a string.", avoids="Dangling refs", severity="high"
            ),
            scope=Scope(paths=["shared/path"], environments=[], authorities=[]),
        )
        c2 = _make_candidate(
            id="CS-002",
            claim="Ids are numeric",
            operational_effect=OperationalEffect(
                changes="Normalize the id to an integer.", avoids="Type errors", severity="high"
            ),
            scope=Scope(paths=["shared/path"], environments=[], authorities=[]),
        )
        conflicts = detect_conflicts([c1, c2])
        assert len(conflicts) == 1
        assert conflicts[0].publication_blocked
        assert "incompatible actions" in conflicts[0].reason

    def test_complementary_facts_are_not_a_conflict(self) -> None:
        """Two different true facts about one file must not block each other."""
        effect = OperationalEffect(
            changes="Do not edit manually.", avoids="Overwritten edits", severity="medium"
        )
        c1 = _make_candidate(
            id="CS-001",
            claim="This file is generated",
            operational_effect=effect,
            scope=Scope(paths=["shared/path"], environments=[], authorities=[]),
        )
        c2 = _make_candidate(
            id="CS-002",
            claim="This file is regenerated nightly",
            operational_effect=effect,
            scope=Scope(paths=["shared/path"], environments=[], authorities=[]),
        )
        assert detect_conflicts([c1, c2]) == []


class TestMergeCandidates:
    def test_reviewed_candidate_survives_recompilation(self) -> None:
        prior = _make_candidate(id="CS-001", claim="Old", status=KnowledgeState.CANDIDATE)
        prior.transition_to(KnowledgeState.APPROVED, reason="approved")
        fresh = _make_candidate(id="CS-001", claim="Recompiled")

        merged = merge_candidates([prior], [fresh])

        assert len(merged) == 1
        assert merged[0].status == KnowledgeState.APPROVED
        assert merged[0].claim == "Old"
        assert merged[0].transition_history

    def test_unreviewed_candidate_is_refreshed(self) -> None:
        prior = _make_candidate(id="CS-001", claim="Old")
        fresh = _make_candidate(id="CS-001", claim="Recompiled")

        merged = merge_candidates([prior], [fresh])

        assert len(merged) == 1
        assert merged[0].claim == "Recompiled"

    def test_reviewed_candidate_retained_when_it_no_longer_compiles(self) -> None:
        prior = _make_candidate(id="CS-GONE")
        prior.transition_to(KnowledgeState.APPROVED, reason="approved")

        merged = merge_candidates([prior], [])

        assert [c.id for c in merged] == ["CS-GONE"]

    def test_unreviewed_candidate_dropped_when_it_no_longer_compiles(self) -> None:
        prior = _make_candidate(id="CS-GONE")

        assert merge_candidates([prior], []) == []

    def test_new_candidate_is_added(self) -> None:
        prior = _make_candidate(id="CS-001")
        fresh = _make_candidate(id="CS-002")

        merged = merge_candidates([prior], [fresh])

        assert [c.id for c in merged] == ["CS-002"]


class TestDependencyHashRecording:
    def test_compiled_dependency_records_a_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "ids.py").write_text("x = '1'.zfill(3)\n")
            obs = _make_obs("type-inconsistency", ref="ids.py")

            candidates, _ = compile_candidates([obs], repo)

            assert candidates
            dep = candidates[0].depends_on[0]
            recorded = dep.hash if not isinstance(dep, dict) else dep.get("hash")
            assert recorded, "dependency compiled without a hash — content changes invisible"

    def test_missing_file_records_no_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            obs = _make_obs("generated-file", ref="absent.py")

            candidates, _ = compile_candidates([obs], repo)

            if candidates:
                dep = candidates[0].depends_on[0]
                recorded = dep.hash if not isinstance(dep, dict) else dep.get("hash")
                assert recorded is None


class TestCompileCandidates:
    def test_type_inconsistency_compiled(self) -> None:
        obs = _make_obs("type-inconsistency")
        candidates, _conflicts = compile_candidates([obs])
        assert len(candidates) >= 1
        assert candidates[0].durable_form == "regression-test"

    def test_structural_observations_not_compiled(self) -> None:
        obs = _make_obs("entry-point")
        candidates, _conflicts = compile_candidates([obs])
        assert len(candidates) == 0

    def test_generated_file_compiled(self) -> None:
        obs = _make_obs("generated-file")
        candidates, _conflicts = compile_candidates([obs])
        assert len(candidates) >= 1
        assert candidates[0].durable_form == "agent-note"

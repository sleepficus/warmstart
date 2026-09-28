"""Tests for the Phase 2 detectors added after the first pass.

Each detector gets a positive control and a negative one. The negatives matter
more: two of these detectors were shipped in a first draft that fired on
essentially every document in this repository, and a detector that floods the
reviewer is worse than no detector at all.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from coldstart.domain import ObservationClass
from coldstart.inspection import (
    _detect_ci_policies,
    _detect_contracts,
    _detect_doc_contradictions,
    _detect_generated_files,
    _detect_leading_zero_patterns,
    _detect_migrations,
    _detect_protected_paths,
    _detect_repeated_reverts,
    _detect_schema_files,
    _detect_unusual_exceptions,
    inspect_repository,
)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestMigrations:
    def test_detects_a_migrations_directory(self, repo: Path) -> None:
        (repo / "migrations").mkdir()
        (repo / "migrations" / "001.sql").write_text("ALTER TABLE x;\n")
        found = _detect_migrations(repo)
        assert len(found) == 1
        assert found[0].classification is ObservationClass.VERIFIED_FACT

    def test_silent_without_migrations(self, repo: Path) -> None:
        assert _detect_migrations(repo) == []


class TestProtectedPaths:
    def test_detects_an_active_hook(self, repo: Path) -> None:
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        found = _detect_protected_paths(repo)
        assert any("pre-commit" in o.source.reference for o in found)

    def test_ignores_sample_hooks(self, repo: Path) -> None:
        """Every fresh clone has sample hooks; none of them run."""
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit.sample").write_text("#!/bin/sh\n")
        assert _detect_protected_paths(repo) == []


class TestCiPolicies:
    def test_extracts_gating_commands(self, repo: Path) -> None:
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("jobs:\n  t:\n    steps:\n      - run: pytest\n")
        found = _detect_ci_policies(repo)
        assert len(found) == 1
        assert "pytest" in found[0].observed.summary

    def test_silent_without_ci(self, repo: Path) -> None:
        assert _detect_ci_policies(repo) == []


class TestDocContradictions:
    def test_detects_a_reference_left_behind_by_a_move(self, repo: Path) -> None:
        (repo / "src").mkdir()
        (repo / "src" / "here.py").write_text("x = 1\n")
        (repo / "README.md").write_text("See `src/here.py` and `src/moved_away.py`.\n")

        found = _detect_doc_contradictions(repo)

        assert len(found) == 1
        assert "src/moved_away.py" in found[0].observed.summary
        assert found[0].classification is ObservationClass.CANDIDATE_ANOMALY

    def test_ignores_module_names(self, repo: Path) -> None:
        """`coldstart.domain` is a module path, not a file claim."""
        (repo / "README.md").write_text("See `coldstart.domain` and `re.sub`.\n")
        assert _detect_doc_contradictions(repo) == []

    def test_ignores_files_the_tool_creates_at_runtime(self, repo: Path) -> None:
        """The parent directory must exist, or the doc is describing intent.

        Documentation for a file the tool writes later (`.coldstart/config.yaml`)
        names a directory that does not exist yet, and contradicts nothing.
        """
        (repo / "README.md").write_text("Writes `.coldstart/config.yaml` on init.\n")
        assert _detect_doc_contradictions(repo) == []

    def test_ignores_bare_filenames_in_prose(self, repo: Path) -> None:
        (repo / "README.md").write_text("Edit `config.yaml` to change settings.\n")
        assert _detect_doc_contradictions(repo) == []


class TestUnusualExceptions:
    def test_detects_a_bare_except(self, repo: Path) -> None:
        (repo / "m.py").write_text("try:\n    f()\nexcept:\n    pass\n")
        assert len(_detect_unusual_exceptions(repo)) == 1

    def test_detects_a_broad_silent_handler(self, repo: Path) -> None:
        (repo / "m.py").write_text("try:\n    f()\nexcept Exception:\n    pass\n")
        assert len(_detect_unusual_exceptions(repo)) == 1

    def test_ignores_a_deliberate_typed_skip(self, repo: Path) -> None:
        """`except OSError: continue` while walking files is intent, not a bug."""
        (repo / "m.py").write_text(
            "for f in files:\n    try:\n        g()\n    except OSError:\n        continue\n"
        )
        assert _detect_unusual_exceptions(repo) == []

    def test_does_not_match_the_word_inside_a_string(self, repo: Path) -> None:
        """The first draft matched its own report text and flagged itself."""
        (repo / "m.py").write_text('msg = "bare `except:` is bad"\n')
        assert _detect_unusual_exceptions(repo) == []


class TestContracts:
    def test_detects_a_source_of_truth_declaration(self, repo: Path) -> None:
        (repo / "docs.md").write_text("The upstream feed is the source of truth.\n")
        found = _detect_contracts(repo)
        assert len(found) == 1
        assert found[0].classification is ObservationClass.EXTERNAL_FACT

    def test_silent_without_a_declaration(self, repo: Path) -> None:
        (repo / "docs.md").write_text("Some ordinary documentation.\n")
        assert _detect_contracts(repo) == []


class TestRepeatedReverts:
    def test_detects_a_revert(self, repo: Path) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)

        git("init", "-q")
        git("config", "user.email", "test.invalid")
        git("config", "user.name", "T")
        (repo / "a.py").write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "add a")
        (repo / "a.py").write_text("x = 2\n")
        git("add", "-A")
        git("commit", "-qm", "change a")
        git("revert", "--no-edit", "HEAD")

        found = _detect_repeated_reverts(repo)

        assert any(o.source.reference == "a.py" for o in found)
        assert all(o.classification is ObservationClass.CANDIDATE_ANOMALY for o in found)

    def test_silent_outside_a_repository(self, repo: Path) -> None:
        assert _detect_repeated_reverts(repo) == []


class TestClassification:
    def test_every_observation_declares_a_class(self, repo: Path) -> None:
        (repo / "src").mkdir()
        (repo / "src" / "ids.py").write_text("def pad(x):\n    return x.zfill(8)\n")
        (repo / "gen.py").write_text("# Generated\n# DO NOT EDIT\nx = 1\n")
        (repo / "api.py").write_text("import requests\n")

        observations = inspect_repository(repo)

        assert observations
        assert all(isinstance(o.classification, ObservationClass) for o in observations)

    def test_inference_is_not_labelled_a_verified_fact(self, repo: Path) -> None:
        """A `.zfill` call is a fact; what it implies about ids is a reading."""
        (repo / "ids.py").write_text("def pad(x):\n    return x.zfill(8)\n")
        observations = inspect_repository(repo)
        inferred = [o for o in observations if o.observed.kind == "type-inconsistency"]
        assert inferred
        assert all(o.classification is ObservationClass.PROBABLE_INTERPRETATION for o in inferred)

    def test_external_integration_is_flagged_unverifiable(self, repo: Path) -> None:
        (repo / "api.py").write_text("import requests\n")
        observations = inspect_repository(repo)
        external = [o for o in observations if o.observed.kind == "external-integration"]
        assert external
        assert all(o.classification is ObservationClass.EXTERNAL_FACT for o in external)


class TestClaimIdentifiesFileUniquely:
    def test_same_basename_in_two_directories_yields_distinct_claims(self, repo: Path) -> None:
        """A claim must say which file it is about.

        Found on a real repository: two `pipeline-state.yaml` files in
        different directories produced word-for-word identical claims. Published
        as notes, an agent could not tell which file either line referred to.
        """
        for sub in ("course-a", "course-b"):
            (repo / sub).mkdir()
            (repo / sub / "pipeline-state.yaml").write_text("# Generated\n# DO NOT EDIT\nx: 1\n")

        found = _detect_generated_files(repo)

        assert len(found) == 2
        claims = {o.observed.summary for o in found}
        assert len(claims) == 2, f"claims are indistinguishable: {claims}"
        assert any("course-a/pipeline-state.yaml" in c for c in claims)
        assert any("course-b/pipeline-state.yaml" in c for c in claims)


class TestZeroPadDetection:
    """The canonical trap from the concept note, which the first draft missed.

    Measured against the repository the concept note was written about, the
    original detector matched only `.zfill(` and so found nothing, despite
    three files zero-padding with `:02d`.
    """

    def test_detects_format_spec_padding(self, repo: Path) -> None:
        (repo / "wiki.py").write_text('def slug(lecture_id):\n    return f"L{lecture_id:02d}"\n')
        found = _detect_leading_zero_patterns(repo)
        assert len(found) == 1
        assert "format spec" in found[0].observed.summary

    def test_detects_percent_padding(self, repo: Path) -> None:
        (repo / "old.py").write_text('def code(n):\n    return "%03d" % n\n')
        assert len(_detect_leading_zero_patterns(repo)) == 1

    def test_detects_zfill(self, repo: Path) -> None:
        (repo / "a.py").write_text("def pad(account_id):\n    return account_id.zfill(8)\n")
        assert len(_detect_leading_zero_patterns(repo)) == 1

    def test_detects_rjust_with_zero(self, repo: Path) -> None:
        (repo / "a.py").write_text("def pad(key):\n    return key.rjust(8, '0')\n")
        assert len(_detect_leading_zero_patterns(repo)) == 1

    def test_identifier_context_sharpens_the_claim(self, repo: Path) -> None:
        (repo / "ids.py").write_text('def f(account_id):\n    return f"{account_id:02d}"\n')
        (repo / "clock.py").write_text('def t(h, m):\n    return f"{h:02d}{m:02d}"\n')
        by_ref = {o.source.reference: o for o in _detect_leading_zero_patterns(repo)}
        assert "likely opaque strings" in by_ref["ids.py"].observed.summary
        assert "may be display formatting" in by_ref["clock.py"].observed.summary

    def test_silent_on_code_without_padding(self, repo: Path) -> None:
        (repo / "a.py").write_text("def add(x, y):\n    return x + y\n")
        assert _detect_leading_zero_patterns(repo) == []


class TestSchemaDetection:
    def test_finds_versioned_markdown_contracts(self, repo: Path) -> None:
        """A schema is a contract regardless of the format it is written in.

        Restricting this to yaml and json missed a repository holding twenty
        versioned schema documents in Markdown.
        """
        d = repo / "schemas"
        d.mkdir()
        (d / "claim-v2.1.md").write_text("# Claim schema\n")
        (d / "audit-v1.md").write_text("# Audit schema\n")

        found = _detect_schema_files(repo)

        assert len(found) == 1
        assert "2 schema or contract file(s)" in found[0].observed.summary
        assert "explicit version" in found[0].observed.summary

    def test_finds_a_nested_schema_directory(self, repo: Path) -> None:
        d = repo / "services" / "billing" / "schemas"
        d.mkdir(parents=True)
        (d / "invoice.json").write_text("{}")
        assert len(_detect_schema_files(repo)) == 1

    def test_ignores_agent_worktree_copies(self, repo: Path) -> None:
        """A live worktree is a copy; reporting it doubles every finding."""
        real = repo / "schemas"
        real.mkdir()
        (real / "a.json").write_text("{}")
        copy = repo / ".claude" / "worktrees" / "abc123" / "schemas"
        copy.mkdir(parents=True)
        (copy / "a.json").write_text("{}")

        found = _detect_schema_files(repo)

        assert [o.source.reference for o in found] == ["schemas"]

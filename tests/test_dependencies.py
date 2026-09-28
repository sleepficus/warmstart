"""Tests for the full set of staleness dependency kinds.

The external-contract case carries the most weight: it must always report
stale, because "we could not check" and "it is still true" are different
statements and only one of them is honest.
"""

import tempfile
from pathlib import Path

import pytest

from coldstart.staleness import (
    verify_config_dependency,
    verify_dependency,
    verify_external_dependency,
    verify_revision_dependency,
    verify_schema_dependency,
    verify_symbol_dependency,
    verify_test_dependency,
)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestSymbolDependency:
    def test_existing_function_is_valid(self, repo: Path) -> None:
        (repo / "mod.py").write_text("def parse_id(x):\n    return x\n")
        assert verify_symbol_dependency(
            {"kind": "symbol", "path": "mod.py", "symbol": "parse_id"}, repo
        ) == (True, "")

    def test_deleted_symbol_is_stale(self, repo: Path) -> None:
        (repo / "mod.py").write_text("def other(x):\n    return x\n")
        valid, reason = verify_symbol_dependency(
            {"kind": "symbol", "path": "mod.py", "symbol": "parse_id"}, repo
        )
        assert not valid
        assert "no longer defined" in reason

    def test_symbol_only_in_a_comment_is_stale(self, repo: Path) -> None:
        """Python is parsed, not searched.

        A text search would find the name in a comment and report a symbol that
        has actually been deleted — the exact failure staleness checking exists
        to prevent.
        """
        (repo / "mod.py").write_text("# parse_id used to live here\ndef other(x):\n    return x\n")
        valid, _ = verify_symbol_dependency(
            {"kind": "symbol", "path": "mod.py", "symbol": "parse_id"}, repo
        )
        assert not valid

    def test_class_and_assignment_count_as_definitions(self, repo: Path) -> None:
        (repo / "mod.py").write_text("class Loader:\n    pass\n\nDEFAULT = 3\n")
        assert verify_symbol_dependency(
            {"kind": "symbol", "path": "mod.py", "symbol": "Loader"}, repo
        )[0]
        assert verify_symbol_dependency(
            {"kind": "symbol", "path": "mod.py", "symbol": "DEFAULT"}, repo
        )[0]

    def test_unparseable_python_is_stale(self, repo: Path) -> None:
        (repo / "mod.py").write_text("def broken(:\n")
        valid, reason = verify_symbol_dependency(
            {"kind": "symbol", "path": "mod.py", "symbol": "broken"}, repo
        )
        assert not valid
        assert "Cannot parse" in reason

    def test_missing_file_is_stale(self, repo: Path) -> None:
        valid, _ = verify_symbol_dependency(
            {"kind": "symbol", "path": "gone.py", "symbol": "x"}, repo
        )
        assert not valid


class TestTestDependency:
    def test_present_test_is_valid(self, repo: Path) -> None:
        (repo / "test_ids.py").write_text("def test_keeps_zeros():\n    assert True\n")
        assert verify_test_dependency(
            {"kind": "test", "path": "test_ids.py", "test": "test_keeps_zeros"}, repo
        ) == (True, "")

    def test_deleted_test_is_stale(self, repo: Path) -> None:
        """A note claiming enforcement whose test is gone is the worst case."""
        (repo / "test_ids.py").write_text("def test_something_else():\n    assert True\n")
        valid, reason = verify_test_dependency(
            {"kind": "test", "path": "test_ids.py", "test": "test_keeps_zeros"}, repo
        )
        assert not valid
        assert "no longer defined" in reason


class TestConfigDependency:
    def test_present_key_is_valid(self, repo: Path) -> None:
        (repo / "cfg.yaml").write_text("service:\n  retries: 3\n")
        assert verify_config_dependency(
            {"kind": "config", "path": "cfg.yaml", "key": "service.retries"}, repo
        ) == (True, "")

    def test_removed_key_is_stale(self, repo: Path) -> None:
        (repo / "cfg.yaml").write_text("service:\n  timeout: 5\n")
        valid, reason = verify_config_dependency(
            {"kind": "config", "path": "cfg.yaml", "key": "service.retries"}, repo
        )
        assert not valid
        assert "no longer present" in reason

    def test_changed_value_is_stale(self, repo: Path) -> None:
        (repo / "cfg.yaml").write_text("service:\n  retries: 5\n")
        valid, reason = verify_config_dependency(
            {"kind": "config", "path": "cfg.yaml", "key": "service.retries", "value": 3}, repo
        )
        assert not valid
        assert "changed" in reason

    def test_json_is_supported(self, repo: Path) -> None:
        (repo / "cfg.json").write_text('{"a": {"b": 1}}')
        assert verify_config_dependency({"kind": "config", "path": "cfg.json", "key": "a.b"}, repo)[
            0
        ]


class TestSchemaDependency:
    def test_matching_version_is_valid(self, repo: Path) -> None:
        (repo / "schema.yaml").write_text("version: 2\nfields: []\n")
        assert verify_schema_dependency(
            {"kind": "schema", "path": "schema.yaml", "version": 2}, repo
        ) == (True, "")

    def test_bumped_version_is_stale(self, repo: Path) -> None:
        (repo / "schema.yaml").write_text("version: 3\n")
        valid, reason = verify_schema_dependency(
            {"kind": "schema", "path": "schema.yaml", "version": 2}, repo
        )
        assert not valid
        assert "version changed" in reason

    def test_schema_without_a_version_cannot_be_confirmed(self, repo: Path) -> None:
        (repo / "schema.yaml").write_text("fields: []\n")
        valid, reason = verify_schema_dependency(
            {"kind": "schema", "path": "schema.yaml", "version": 2}, repo
        )
        assert not valid
        assert "no version" in reason


class TestRevisionDependency:
    def test_non_repository_is_stale(self, repo: Path) -> None:
        valid, reason = verify_revision_dependency(
            {"kind": "revision", "revision": "abc1234"}, repo
        )
        assert not valid
        assert "not a git repository" in reason


class TestExternalDependency:
    def test_external_contract_always_requires_manual_reverification(self, repo: Path) -> None:
        """The load-bearing negative.

        Nothing inside the repository can confirm a claim about a system
        outside it, so the only honest automated answer is that nobody has
        checked. Returning valid here would let silence read as confirmation.
        """
        valid, reason = verify_external_dependency(
            {"kind": "external", "authority": "upstream-customer-export", "version": "v3"}, repo
        )
        assert not valid
        assert "Manual reverification required" in reason
        assert "upstream-customer-export" in reason

    def test_it_is_stale_even_with_no_detail(self, repo: Path) -> None:
        assert verify_external_dependency({"kind": "external"}, repo)[0] is False


class TestDispatch:
    def test_unknown_kind_is_stale_not_ignored(self, repo: Path) -> None:
        """A dependency nobody knows how to check has not been checked."""
        valid, reason = verify_dependency({"kind": "telepathy", "path": "x"}, repo)
        assert not valid
        assert "Unknown dependency kind" in reason

    def test_file_kind_still_dispatches(self, repo: Path) -> None:
        (repo / "a.txt").write_text("hello")
        assert verify_dependency({"kind": "file", "path": "a.txt"}, repo) == (True, "")

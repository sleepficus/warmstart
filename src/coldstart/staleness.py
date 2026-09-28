"""Staleness verification for candidate dependencies.

Covers every dependency kind the specification lists: file existence, file
hash, symbol existence, test existence, schema version, configuration key, git
revision, and external contract.

The external-contract case is the important one. It always reports stale,
because the only honest automated statement about a system this repository
cannot see is that nobody has checked it. Treating silence as confirmation is
exactly how a note becomes confidently wrong.
"""

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from coldstart import gitinfo
from coldstart.domain import (
    Candidate,
    Dependency,
    FileDependency,
    KnowledgeState,
)
from coldstart.storage import load_state, save_state, state_dir


def _dep_field(dep: Dependency, name: str) -> Any:
    """Read a field from a dependency, whether typed or a raw mapping."""
    if isinstance(dep, dict):
        return dep.get(name)
    return getattr(dep, name, None)


def dependency_kind(dep: Dependency) -> str:
    return str(_dep_field(dep, "kind") or "file")


def verify_file_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """Verify if a file dependency is still valid.

    Returns (is_valid, reason).
    """
    path_str: str | None = None
    expected_hash: str | None = None

    if isinstance(dep, FileDependency):
        path_str = dep.path
        expected_hash = dep.hash
    elif isinstance(dep, dict) and dep.get("kind") == "file":
        path_str = str(dep.get("path", ""))
        expected_hash = dep.get("hash")

    if not path_str:
        return True, ""

    file_path = workspace_path / path_str
    if not file_path.exists():
        return False, f"Missing file: {path_str}"

    if expected_hash:
        actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            return False, f"File hash changed for {path_str}"

    return True, ""


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def verify_symbol_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """Verify that a named symbol still exists in its file.

    Python files are parsed rather than searched: a regex would match the
    symbol's name inside a comment or a string and report a symbol that has
    actually been deleted, which is the failure mode staleness checking exists
    to prevent. Other languages fall back to a word-boundary search, which is
    weaker and says so.
    """
    path_str, symbol = _dep_field(dep, "path"), _dep_field(dep, "symbol")
    if not path_str or not symbol:
        return True, ""

    file_path = workspace_path / path_str
    if not file_path.exists():
        return False, f"Missing file for symbol {symbol}: {path_str}"

    try:
        content = file_path.read_text(errors="ignore")
    except OSError as exc:
        return False, f"Cannot read {path_str}: {exc}"

    if file_path.suffix == ".py":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return False, f"Cannot parse {path_str} to confirm symbol {symbol}"
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name == symbol
            ):
                return True, ""
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == symbol:
                return True, ""
        return False, f"Symbol {symbol} no longer defined in {path_str}"

    if re.search(rf"\b{re.escape(symbol)}\b", content):
        return True, ""
    return False, f"Symbol {symbol} not found in {path_str}"


def verify_test_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """Verify that a test enforcing a claim still exists.

    A note whose enforcing test was deleted is the most dangerous kind: it
    still reads as "enforced by ...", and nothing enforces it any more.
    """
    path_str, test_name = _dep_field(dep, "path"), _dep_field(dep, "test")
    if not path_str:
        return True, ""

    file_path = workspace_path / path_str
    if not file_path.exists():
        return False, f"Missing test file: {path_str}"
    if not test_name:
        return True, ""

    try:
        content = file_path.read_text(errors="ignore")
    except OSError as exc:
        return False, f"Cannot read {path_str}: {exc}"

    if re.search(rf"def\s+{re.escape(test_name)}\s*\(", content):
        return True, ""
    return False, f"Test {test_name} no longer defined in {path_str}"


def _lookup_key(data: Any, dotted: str) -> tuple[bool, Any]:
    """Resolve a dotted key path through nested mappings."""
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _load_structured(file_path: Path) -> Any:
    """Parse YAML or JSON, returning None when the file cannot be read.

    An unparseable dependency target is reported stale by the caller: we cannot
    confirm a key in a file we cannot read.
    """
    try:
        if file_path.suffix == ".json":
            return json.loads(file_path.read_text(errors="ignore"))
        yaml = YAML(typ="safe")
        return yaml.load(file_path)
    except Exception:
        return None


def verify_config_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """Verify that a configuration key still exists, and optionally its value."""
    path_str, key = _dep_field(dep, "path"), _dep_field(dep, "key")
    expected = _dep_field(dep, "value")
    if not path_str or not key:
        return True, ""

    file_path = workspace_path / path_str
    if not file_path.exists():
        return False, f"Missing config file: {path_str}"

    data = _load_structured(file_path)
    if data is None:
        return False, f"Cannot parse {path_str} to confirm key {key}"

    present, actual = _lookup_key(data, key)
    if not present:
        return False, f"Config key {key} no longer present in {path_str}"
    if expected is not None and str(actual) != str(expected):
        return False, f"Config key {key} changed in {path_str} (expected {expected})"
    return True, ""


def verify_schema_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """Verify a schema's declared version."""
    path_str = _dep_field(dep, "path")
    expected = _dep_field(dep, "version")
    if not path_str:
        return True, ""

    file_path = workspace_path / path_str
    if not file_path.exists():
        return False, f"Missing schema file: {path_str}"
    if expected is None:
        return True, ""

    data = _load_structured(file_path)
    if data is None:
        return False, f"Cannot parse schema {path_str}"

    for key in ("version", "schema_version", "$schema"):
        present, actual = _lookup_key(data, key)
        if present:
            if str(actual) == str(expected):
                return True, ""
            return False, f"Schema version changed in {path_str}: {actual} != {expected}"
    return False, f"Schema {path_str} declares no version to compare"


def verify_revision_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """Verify the repository revision a claim was established at.

    A moved revision does not by itself make a claim wrong, so this reports
    staleness for reverification rather than asserting the claim is false.
    """
    expected = _dep_field(dep, "revision")
    if not expected:
        return True, ""
    if not gitinfo.is_repository(workspace_path):
        return False, "Recorded a git revision, but this is not a git repository"

    actual = gitinfo.current_revision(workspace_path)
    if actual is None:
        return False, "Cannot determine the current revision"
    if actual == expected:
        return True, ""
    return False, f"Repository moved from {expected[:8]} to {actual[:8]}; reverify"


def verify_external_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """An external contract can never be confirmed from inside the repository.

    This deliberately always reports stale. The specification calls for
    "external contract version requiring manual reverification", and the only
    honest automated answer about a system we cannot see is that nobody has
    checked. Treating silence as confirmation is how a note becomes confidently
    wrong.
    """
    authority = _dep_field(dep, "authority") or "an external authority"
    version = _dep_field(dep, "version")
    detail = f" (recorded version {version})" if version else ""
    return False, (
        f"Depends on {authority}{detail}, which cannot be verified from this "
        f"repository. Manual reverification required."
    )


VERIFIERS = {
    "file": verify_file_dependency,
    "symbol": verify_symbol_dependency,
    "test": verify_test_dependency,
    "config": verify_config_dependency,
    "schema": verify_schema_dependency,
    "revision": verify_revision_dependency,
    "external": verify_external_dependency,
}


def verify_dependency(dep: Dependency, workspace_path: Path) -> tuple[bool, str]:
    """Verify any dependency kind.

    An unrecognised kind is reported stale rather than passed. A dependency
    nobody knows how to check is not a dependency that has been checked.
    """
    kind = dependency_kind(dep)
    verifier = VERIFIERS.get(kind)
    if verifier is None:
        return False, f"Unknown dependency kind '{kind}'; cannot verify"
    return verifier(dep, workspace_path)


def verify_staleness(workspace_path: Path, state_override: Path | None = None) -> list[str]:
    """Verify all dependencies and quarantine stale candidates.

    Returns list of quarantine messages.
    """
    candidates_path = state_dir(workspace_path, state_override) / "candidates.yaml"
    if not candidates_path.exists():
        return []

    candidates = load_state(Candidate, candidates_path)
    dirty = False
    messages: list[str] = []

    for c in candidates:
        # A published note goes through STALE on its way to QUARANTINED, so the
        # history records that it was live when its dependency moved rather than
        # jumping straight to a quarantine with no explanation of what changed.
        if c.status in (KnowledgeState.APPROVED, KnowledgeState.PUBLISHED):
            target_state = KnowledgeState.QUARANTINED
        elif c.status == KnowledgeState.CANDIDATE:
            target_state = KnowledgeState.NEEDS_VERIFICATION
        else:
            continue

        stale = False
        reason = ""
        for dep in c.depends_on:
            is_valid, dep_reason = verify_dependency(dep, workspace_path)
            if not is_valid:
                stale = True
                reason = dep_reason
                break

        if stale:
            if c.status == KnowledgeState.PUBLISHED:
                c.transition_to(
                    KnowledgeState.STALE,
                    reason=f"Stale dependency: {reason}",
                )
            c.transition_to(
                target_state,
                reason=f"Stale dependency: {reason}",
                metadata={},
            )
            dirty = True
            messages.append(f"{c.id}: {reason} → {target_state.value}")

    if dirty:
        save_state(candidates, candidates_path)

    return messages

"""Repository inspection: deterministic extraction of observations.

Implements the MVP-required observation categories from README Phase 2.
All detection is deterministic (no model inference, no network).
"""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from coldstart import gitinfo
from coldstart.domain import (
    Observation,
    ObservationClass,
    ObservationData,
    Source,
    SourceKind,
)
from coldstart.storage import state_dir


def init_repository(path: Path, state_override: Path | None = None) -> dict[str, Any]:
    """Initialize .coldstart config in a repository directory.

    Detects git availability, agent instruction files, languages, and test
    infrastructure. Warns (does not fail) on non-git directories.
    """
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Path {path} is not a valid directory.")

    has_git = (path / ".git").exists()
    has_agents_md = (path / "AGENTS.md").exists()
    has_claude_md = (path / "CLAUDE.md").exists()

    # Detect languages
    languages: list[str] = []
    if list(path.rglob("*.py")):
        languages.append("python")
    if list(path.rglob("*.js")) or list(path.rglob("*.ts")):
        languages.append("javascript/typescript")
    if list(path.rglob("*.go")):
        languages.append("go")
    if list(path.rglob("*.java")):
        languages.append("java")

    # Detect test infrastructure
    test_commands: list[str] = []
    if (path / "pyproject.toml").exists() or list(path.rglob("conftest.py")):
        test_commands.append("pytest")
    if (path / "package.json").exists():
        test_commands.append("npm test")

    coldstart_dir = state_dir(path, state_override)
    coldstart_dir.mkdir(parents=True, exist_ok=True)

    config_path = coldstart_dir / "config.yaml"
    config: dict[str, Any] = {
        "version": 1,
        "repository": {
            "git_available": has_git,
            "has_agents_md": has_agents_md,
            "has_claude_md": has_claude_md,
            "languages": languages,
            "test_commands": test_commands,
        },
        "sources": {"repository": True, "sessions": []},
    }

    if not config_path.exists():
        yaml = YAML()
        with open(config_path, "w") as f:
            yaml.dump(config, f)

    return config


# --- Observation category detectors ---

_SKIP_DIRS = {
    ".venv",
    ".coldstart",
    "__pycache__",
    "node_modules",
    ".git",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    # Transient agent worktrees are copies of the repository, not content.
    # Without this every finding is reported once per live worktree.
    "worktrees",
    "dist",
    "build",
    ".next",
}


def _should_skip(file_path: Path) -> bool:
    """Check if a file path should be skipped during inspection."""
    return bool(_SKIP_DIRS.intersection(file_path.parts))


def _walk(path: Path, pattern: str) -> list[Path]:
    """Sorted, filtered traversal.

    rglob order is filesystem-dependent, so anything derived from traversal
    order — including ids — would differ between machines for an identical
    repository. Sorting makes inspection reproducible.
    """
    return sorted(p for p in path.rglob(pattern) if not _should_skip(p))


def _observation_id(tag: str, reference: str) -> str:
    """Build an id from what was observed, not from the order it was found in.

    Positional ids renumber whenever an unrelated file is added, which silently
    rebinds approval records and published `[CS-…]` references to the wrong
    candidate. Deriving the id from the path keeps it stable for the life of
    the file.
    """
    digest = hashlib.sha256(reference.encode()).hexdigest()[:8]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", Path(reference).name).strip("-").lower()
    return f"OBS-{tag}-{slug}-{digest}"


#: Ways Python actually zero-pads. The original detector matched only
#: `.zfill(`, which missed the two commonest idioms outright — `:02d` format
#: specs and `%02d` interpolation. Measured against the repository the concept
#: note was written about, that gap hid the note's own canonical example.
_ZERO_PAD_IDIOMS: list[tuple[str, re.Pattern[str]]] = [
    (".zfill()", re.compile(r"\.zfill\s*\(")),
    ("format spec :0Nd", re.compile(r":0\d+[dns]")),
    ("percent format %0Nd", re.compile(r"%0\d+d")),
    ("rjust with '0'", re.compile(r"\.rjust\s*\([^)]*['\"]0['\"]")),
]

#: Zero-padding a clock is not the same signal as zero-padding an identifier.
#: A nearby identifier word raises confidence; its absence is reported rather
#: than suppressed, so the reviewer decides.
#: `\b` cannot be used here: an underscore is a word character, so `\bid\b`
#: never matches inside `account_id` — which is precisely where the word
#: appears in real code.
_ID_CONTEXT = re.compile(
    r"(?:^|[^0-9a-z])(id|ids|code|codes|key|keys|number|ref|sku|account)(?:[^0-9a-z]|$)",
    re.I,
)


def _detect_leading_zero_patterns(path: Path) -> list[Observation]:
    """Detect zero-padded formatting, which implies identifiers are strings.

    The trap this exists to catch is an identifier whose leading zeros are
    significant being coerced to an integer somewhere else in the system. The
    padding is the visible half; the coercion is what breaks.
    """
    observations: list[Observation] = []
    for file_path in _walk(path, "*.py"):
        try:
            content = file_path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue

        found = [name for name, pattern in _ZERO_PAD_IDIOMS if pattern.search(content)]
        if not found:
            continue

        identifier_context = bool(_ID_CONTEXT.search(content))
        qualifier = (
            "an identifier is referenced nearby, so these are likely opaque "
            "strings whose leading zeros are significant"
            if identifier_context
            else "no identifier word appears nearby, so this may be display "
            "formatting rather than an identifier"
        )

        observations.append(
            Observation(
                id=_observation_id("leading-zero", str(file_path.relative_to(path))),
                source=Source(
                    kind=SourceKind.REPOSITORY,
                    reference=str(file_path.relative_to(path)),
                ),
                observed=ObservationData(
                    kind="type-inconsistency",
                    summary=(
                        f"File {file_path.relative_to(path)} zero-pads values using "
                        f"{', '.join(found)}; {qualifier}. Coercing such a value to "
                        f"an integer anywhere else would silently drop the padding."
                    ),
                ),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.PROBABLE_INTERPRETATION,
            )
        )
    return observations


def _detect_entry_points(path: Path) -> list[Observation]:
    """Detect modules and entry points."""
    observations: list[Observation] = []
    for file_path in _walk(path, "*.py"):
        try:
            content = file_path.read_text(errors="ignore")
            if 'if __name__ == "__main__"' in content or "if __name__ == '__main__'" in content:
                observations.append(
                    Observation(
                        id=_observation_id("entrypoint", str(file_path.relative_to(path))),
                        source=Source(
                            kind=SourceKind.REPOSITORY,
                            reference=str(file_path.relative_to(path)),
                        ),
                        observed=ObservationData(
                            kind="entry-point",
                            summary=(
                                f"File {file_path.relative_to(path)} contains a __main__ guard, "
                                f"indicating it is an executable entry point."
                            ),
                        ),
                        captured_at=datetime.now(UTC),
                        sensitivity="low",
                        classification=ObservationClass.VERIFIED_FACT,
                    )
                )
        except (OSError, UnicodeDecodeError):
            continue

    # Check pyproject.toml for console_scripts
    pyproject = path / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            if "[project.scripts]" in content:
                observations.append(
                    Observation(
                        id=_observation_id("entrypoint", "pyproject.toml"),
                        source=Source(
                            kind=SourceKind.REPOSITORY,
                            reference="pyproject.toml",
                        ),
                        observed=ObservationData(
                            kind="entry-point",
                            summary="pyproject.toml declares console_scripts entry points.",
                        ),
                        captured_at=datetime.now(UTC),
                        sensitivity="low",
                        classification=ObservationClass.VERIFIED_FACT,
                    )
                )
        except (OSError, UnicodeDecodeError):
            pass

    return observations


def _detect_test_infrastructure(path: Path) -> list[Observation]:
    """Detect test files and fixtures."""
    observations: list[Observation] = []
    test_files = _walk(path, "test_*.py") + _walk(path, "*_test.py")

    if test_files:
        observations.append(
            Observation(
                id="OBS-tests-1",
                source=Source(kind=SourceKind.REPOSITORY, reference="tests/"),
                observed=ObservationData(
                    kind="test-infrastructure",
                    summary=f"Repository contains {len(test_files)} test file(s).",
                ),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.VERIFIED_FACT,
            )
        )

    # Check for fixture directories
    for fixture_dir in ["fixtures", "testdata", "test_data"]:
        for found in _walk(path, fixture_dir):
            if found.is_dir():
                observations.append(
                    Observation(
                        id=_observation_id("fixture", str(found.relative_to(path))),
                        source=Source(
                            kind=SourceKind.REPOSITORY,
                            reference=str(found.relative_to(path)),
                        ),
                        observed=ObservationData(
                            kind="test-fixture",
                            summary=(
                                f"Directory {found.relative_to(path)} appears to "
                                f"contain test fixtures."
                            ),
                        ),
                        captured_at=datetime.now(UTC),
                        sensitivity="low",
                        classification=ObservationClass.PROBABLE_INTERPRETATION,
                    )
                )

    return observations


#: A schema is a contract regardless of the format it is written in. Limiting
#: this to yaml and json missed a repository holding thirty versioned schema
#: documents in Markdown, which is exactly the kind of frozen contract the
#: concept note says accumulates non-derivable facts.
_SCHEMA_SUFFIXES = {".yaml", ".yml", ".json", ".md", ".sql", ".proto", ".graphql", ".xsd"}

#: Versioned filenames — `claim-v2.1.md`, `audit-v1.md` — are the strongest
#: signal that a directory holds contracts rather than notes.
_VERSIONED = re.compile(r"[-_]v\d+(\.\d+)*\b", re.IGNORECASE)


def _detect_schema_files(path: Path) -> list[Observation]:
    """Detect directories holding schema or contract definitions.

    Searched anywhere in the tree rather than only at the root, because a
    schema directory belonging to one service in a monorepo is still a
    contract.
    """
    observations: list[Observation] = []
    seen: set[Path] = set()

    for name in ("schemas", "schema", "contracts", "proto"):
        for schema_dir in _walk(path, name):
            if not schema_dir.is_dir() or schema_dir in seen:
                continue
            seen.add(schema_dir)

            files = sorted(
                f
                for f in schema_dir.rglob("*")
                if f.is_file() and f.suffix.lower() in _SCHEMA_SUFFIXES
            )
            if not files:
                continue

            versioned = [f for f in files if _VERSIONED.search(f.name)]
            reference = str(schema_dir.relative_to(path))
            detail = (
                f" {len(versioned)} of them carry an explicit version, so changing "
                f"one changes an agreement something else depends on."
                if versioned
                else ""
            )
            observations.append(
                Observation(
                    id=_observation_id("schema", reference),
                    source=Source(kind=SourceKind.REPOSITORY, reference=reference),
                    observed=ObservationData(
                        kind="schema",
                        summary=(
                            f"Directory {reference} contains {len(files)} schema or "
                            f"contract file(s).{detail}"
                        ),
                    ),
                    captured_at=datetime.now(UTC),
                    sensitivity="low",
                    classification=ObservationClass.VERIFIED_FACT,
                )
            )
    return observations


def _detect_generated_files(path: Path) -> list[Observation]:
    """Detect generated-file markers."""
    observations: list[Observation] = []
    markers = [
        re.compile(r"#\s*Generated", re.IGNORECASE),
        re.compile(r"#\s*DO NOT EDIT", re.IGNORECASE),
        re.compile(r"@generated"),
        re.compile(r"//\s*Code generated.*DO NOT EDIT"),
    ]

    for file_path in _walk(path, "*"):
        if not file_path.is_file():
            continue
        if file_path.suffix not in (".py", ".go", ".java", ".js", ".ts", ".yaml", ".yml"):
            continue
        try:
            # Only read first 5 lines to check for markers
            with open(file_path, errors="ignore") as f:
                head = "".join(f.readline() for _ in range(5))
            for marker in markers:
                if marker.search(head):
                    observations.append(
                        Observation(
                            id=_observation_id("generated", str(file_path.relative_to(path))),
                            source=Source(
                                kind=SourceKind.REPOSITORY,
                                reference=str(file_path.relative_to(path)),
                            ),
                            observed=ObservationData(
                                kind="generated-file",
                                summary=(
                                    f"File {file_path.relative_to(path)} contains a generated-file "
                                    f"marker. It may be owned by an external tool."
                                ),
                            ),
                            captured_at=datetime.now(UTC),
                            sensitivity="low",
                            classification=ObservationClass.UNRESOLVED_INTENT,
                        )
                    )
                    break
        except (OSError, UnicodeDecodeError):
            continue
    return observations


def _detect_agent_files(path: Path) -> list[Observation]:
    """Detect agent instruction files."""
    observations: list[Observation] = []
    agent_filenames = ["AGENTS.md", "CLAUDE.md", ".cursorrules", ".github/copilot-instructions.md"]

    for name in agent_filenames:
        agent_file = path / name
        if agent_file.exists():
            observations.append(
                Observation(
                    id=_observation_id("agent-file", name),
                    source=Source(
                        kind=SourceKind.REPOSITORY,
                        reference=name,
                    ),
                    observed=ObservationData(
                        kind="agent-instruction-file",
                        summary=f"Repository contains agent instruction file: {name}",
                    ),
                    captured_at=datetime.now(UTC),
                    sensitivity="low",
                    classification=ObservationClass.VERIFIED_FACT,
                )
            )
    return observations


def _detect_codeowners(path: Path) -> list[Observation]:
    """Detect CODEOWNERS boundaries."""
    observations: list[Observation] = []
    codeowners_paths = [
        path / "CODEOWNERS",
        path / ".github" / "CODEOWNERS",
        path / "docs" / "CODEOWNERS",
    ]

    for co_path in codeowners_paths:
        if co_path.exists():
            try:
                content = co_path.read_text()
                entries = [
                    line.strip()
                    for line in content.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                observations.append(
                    Observation(
                        id=_observation_id("codeowners", str(co_path.relative_to(path))),
                        source=Source(
                            kind=SourceKind.REPOSITORY,
                            reference=str(co_path.relative_to(path)),
                        ),
                        observed=ObservationData(
                            kind="codeowners",
                            summary=f"CODEOWNERS file defines {len(entries)} ownership rule(s).",
                        ),
                        captured_at=datetime.now(UTC),
                        sensitivity="low",
                        classification=ObservationClass.VERIFIED_FACT,
                    )
                )
            except (OSError, UnicodeDecodeError):
                continue
    return observations


def _detect_external_sdks(path: Path) -> list[Observation]:
    """Detect external SDK and API integration patterns."""
    observations: list[Observation] = []
    sdk_patterns: dict[str, str] = {
        "boto3": "AWS SDK (boto3)",
        "google.cloud": "Google Cloud SDK",
        "azure": "Azure SDK",
        "stripe": "Stripe API",
        "requests": "HTTP requests library",
        "httpx": "HTTPX HTTP client",
    }

    seen_sdks: set[str] = set()
    for file_path in _walk(path, "*.py"):
        try:
            content = file_path.read_text(errors="ignore")
            for sdk_import, sdk_name in sdk_patterns.items():
                if sdk_import not in seen_sdks and f"import {sdk_import}" in content:
                    seen_sdks.add(sdk_import)
                    observations.append(
                        Observation(
                            id=_observation_id(
                                f"sdk-{sdk_import}", str(file_path.relative_to(path))
                            ),
                            source=Source(
                                kind=SourceKind.REPOSITORY,
                                reference=str(file_path.relative_to(path)),
                            ),
                            observed=ObservationData(
                                kind="external-integration",
                                summary=(
                                    f"Repository integrates with {sdk_name} (import {sdk_import})."
                                ),
                            ),
                            captured_at=datetime.now(UTC),
                            sensitivity="low",
                            classification=ObservationClass.EXTERNAL_FACT,
                        )
                    )
        except (OSError, UnicodeDecodeError):
            continue
    return observations


def inspect_repository(path: Path) -> list[Observation]:
    """Extract repository-derived observations across all MVP categories.

    Categories implemented:
    - Type inconsistencies (leading-zero patterns)
    - Modules and entry points
    - Test infrastructure and fixtures
    - Schema files
    - Generated-file markers
    - Agent instruction files
    - CODEOWNERS boundaries
    - External SDK/API integrations
    - Migrations, protected paths, CI policies
    - Documentation/code contradictions, unusual exceptions
    - Contracts and source-of-truth declarations
    - Repeated reverts, from Git history when available
    """
    observations: list[Observation] = []

    observations.extend(_detect_leading_zero_patterns(path))
    observations.extend(_detect_entry_points(path))
    observations.extend(_detect_test_infrastructure(path))
    observations.extend(_detect_schema_files(path))
    observations.extend(_detect_generated_files(path))
    observations.extend(_detect_agent_files(path))
    observations.extend(_detect_codeowners(path))
    observations.extend(_detect_external_sdks(path))
    observations.extend(_detect_migrations(path))
    observations.extend(_detect_protected_paths(path))
    observations.extend(_detect_ci_policies(path))
    observations.extend(_detect_doc_contradictions(path))
    observations.extend(_detect_unusual_exceptions(path))
    observations.extend(_detect_contracts(path))
    observations.extend(_detect_repeated_reverts(path))

    return observations


def _detect_migrations(path: Path) -> list[Observation]:
    """Detect migration directories.

    Migrations are ordered, applied history: a repository that has them has a
    schema whose past matters, which changes how a file in that directory may
    be edited.
    """
    observations: list[Observation] = []
    for name in ("migrations", "migrate", "alembic"):
        for found in _walk(path, name):
            if not found.is_dir():
                continue
            files = sorted(f for f in found.iterdir() if f.is_file())
            observations.append(
                Observation(
                    id=_observation_id("migrations", str(found.relative_to(path))),
                    source=Source(
                        kind=SourceKind.REPOSITORY, reference=str(found.relative_to(path))
                    ),
                    observed=ObservationData(
                        kind="migrations",
                        summary=(
                            f"Directory {found.relative_to(path)} contains {len(files)} migration "
                            f"file(s). Applied migrations are history: editing one that "
                            f"has already run does not change deployed state."
                        ),
                    ),
                    captured_at=datetime.now(UTC),
                    sensitivity="low",
                    classification=ObservationClass.VERIFIED_FACT,
                )
            )
    return observations


def _detect_protected_paths(path: Path) -> list[Observation]:
    """Detect mechanisms that refuse writes.

    A protected path is the highest-value thing inspection can find: an agent
    that does not know about a hook will fight it, and the specification's own
    example of a hard-won fact is exactly this.
    """
    observations: list[Observation] = []

    hooks_dir = path / ".git" / "hooks"
    if hooks_dir.is_dir():
        active = sorted(
            hook
            for hook in hooks_dir.iterdir()
            if hook.is_file() and not hook.name.endswith(".sample")
        )
        for hook in active:
            observations.append(
                Observation(
                    id=_observation_id("protected", f".git/hooks/{hook.name}"),
                    source=Source(kind=SourceKind.REPOSITORY, reference=f".git/hooks/{hook.name}"),
                    observed=ObservationData(
                        kind="protected-path",
                        summary=(
                            f"An active {hook.name} hook is installed. It may deny "
                            f"writes or commits that otherwise look valid; a denial "
                            f"is the system working, not an obstacle to route around."
                        ),
                    ),
                    captured_at=datetime.now(UTC),
                    sensitivity="low",
                    classification=ObservationClass.VERIFIED_FACT,
                )
            )

    for config_name in (".pre-commit-config.yaml", ".gitattributes"):
        config = path / config_name
        if not config.exists():
            continue
        try:
            content = config.read_text(errors="ignore")
        except OSError:
            continue
        observations.append(
            Observation(
                id=_observation_id("protected", config_name),
                source=Source(kind=SourceKind.REPOSITORY, reference=config_name),
                observed=ObservationData(
                    kind="protected-path",
                    summary=(
                        f"{config_name} declares {len(content.splitlines())} line(s) of "
                        f"write-time policy. Changes to the paths it covers are checked "
                        f"or transformed before they land."
                    ),
                ),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.VERIFIED_FACT,
            )
        )
    return observations


def _detect_ci_policies(path: Path) -> list[Observation]:
    """Detect CI configuration that gates merges."""
    observations: list[Observation] = []
    candidates: list[Path] = []
    workflows = path / ".github" / "workflows"
    if workflows.is_dir():
        candidates.extend(sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")))
    for name in (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml", "azure-pipelines.yml"):
        candidate = path / name
        if candidate.is_file():
            candidates.append(candidate)

    for config in candidates:
        try:
            content = config.read_text(errors="ignore")
        except OSError:
            continue
        commands = sorted(
            {
                line.strip().lstrip("-").strip()
                for line in content.splitlines()
                if re.search(r"\b(pytest|ruff|mypy|npm test|go test|cargo test)\b", line)
            }
        )
        reference = str(config.relative_to(path))
        summary = f"CI configuration {reference} gates changes."
        if commands:
            summary += " Checks that must pass: " + "; ".join(commands[:4]) + "."
        observations.append(
            Observation(
                id=_observation_id("ci", reference),
                source=Source(kind=SourceKind.REPOSITORY, reference=reference),
                observed=ObservationData(kind="ci-policy", summary=summary),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.VERIFIED_FACT,
            )
        )
    return observations


#: Path-like tokens in prose. A directory component is required, which is the
#: filter that makes this detector usable: `coldstart.domain` is a module name
#: and `config.yaml` in prose is usually illustrative, but `src/thing.py` is a
#: claim about the repository.
_DOC_PATH = re.compile(r"`([\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]{1,6})`")

#: Extensions worth checking. Restricting to these keeps the detector away from
#: prose that merely looks path-shaped.
_CHECKABLE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".go",
    ".java",
    ".rb",
    ".rs",
    ".sh",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".cfg",
    ".ini",
    ".sql",
}


def _is_broken_reference(repo_path: Path, token: str) -> bool:
    """Whether `token` is a repository path claim the repository contradicts.

    The load-bearing rule is that the *parent directory* must exist. A document
    describing a file the tool creates at runtime (`.coldstart/config.yaml`)
    names a directory that is absent, and is describing intended behavior
    rather than contradicting anything. A document pointing into a directory
    that does exist, at a file that does not, has been left behind by a move.
    """
    if token.startswith(("http", "/")) or "*" in token:
        return False

    relative = token.lstrip("./")
    if not relative:
        return False

    target = repo_path / relative
    if target.suffix not in _CHECKABLE_SUFFIXES:
        return False
    if target.exists():
        return False
    parent = target.parent
    return parent.is_dir() and parent != repo_path


def _detect_doc_contradictions(path: Path) -> list[Observation]:
    """Detect documentation that references files the repository does not have.

    When a document and the repository disagree, the repository wins and
    somebody should be told — so this is reported as an anomaly to investigate,
    never as an established defect.
    """
    observations: list[Observation] = []
    for doc in _walk(path, "*.md"):
        try:
            content = doc.read_text(errors="ignore")
        except OSError:
            continue

        missing = sorted(
            {token for token in _DOC_PATH.findall(content) if _is_broken_reference(path, token)}
        )
        if not missing:
            continue

        reference = str(doc.relative_to(path))
        observations.append(
            Observation(
                id=_observation_id("doc-contradiction", reference),
                source=Source(kind=SourceKind.REPOSITORY, reference=reference),
                observed=ObservationData(
                    kind="doc-contradiction",
                    summary=(
                        f"{reference} references {len(missing)} path(s) that do not "
                        f"exist: {', '.join(missing[:4])}. Either the document is "
                        f"stale or the files moved; the repository is the authority."
                    ),
                ),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.CANDIDATE_ANOMALY,
            )
        )
    return observations


#: Only a *broad* catch that is also silent. A typed `except OSError: continue`
#: while walking files is a deliberate skip, not a hidden failure, and flagging
#: it would bury the real cases.
_SWALLOWED = re.compile(
    r"^[ \t]*except\s+(?:Exception|BaseException)[^\n:]*:\s*\n\s*(?:pass|continue)\b",
    re.MULTILINE,
)

#: Anchored to statement position. Without the anchor this matches the word
#: "except:" inside the detector's own report string, and the detector flags
#: itself — which it did.
_BARE_EXCEPT = re.compile(r"^[ \t]*except\s*:", re.MULTILINE)


def _detect_unusual_exceptions(path: Path) -> list[Observation]:
    """Detect exception handling that hides failure.

    A swallowed exception is where an expectation breaks silently, which is
    precisely the moment this product exists to notice.
    """
    observations: list[Observation] = []
    for file_path in _walk(path, "*.py"):
        try:
            content = file_path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue

        bare = len(_BARE_EXCEPT.findall(content))
        swallowed = len(_SWALLOWED.findall(content))
        if not bare and not swallowed:
            continue

        reference = str(file_path.relative_to(path))
        parts = []
        if bare:
            parts.append(f"{bare} bare `except:`")
        if swallowed:
            parts.append(f"{swallowed} silently swallowed exception(s)")
        observations.append(
            Observation(
                id=_observation_id("exceptions", reference),
                source=Source(kind=SourceKind.REPOSITORY, reference=reference),
                observed=ObservationData(
                    kind="unusual-exception",
                    summary=(
                        f"{reference} contains {' and '.join(parts)}. Failures here "
                        f"do not surface, so a broken assumption can persist unseen."
                    ),
                ),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.CANDIDATE_ANOMALY,
            )
        )
    return observations


_AUTHORITY = re.compile(
    r"\b(source of truth|authoritative|system of record|canonical source|"
    r"do not edit directly|owned by)\b",
    re.IGNORECASE,
)


def _detect_contracts(path: Path) -> list[Observation]:
    """Detect declared contracts and source-of-truth statements.

    A repository that names an external authority accumulates facts it cannot
    verify, which the concept note argues is where this product pays for
    itself.
    """
    observations: list[Observation] = []
    for doc in _walk(path, "*.md"):
        try:
            content = doc.read_text(errors="ignore")
        except OSError:
            continue
        matches = [line.strip() for line in content.splitlines() if _AUTHORITY.search(line)]
        if not matches:
            continue
        reference = str(doc.relative_to(path))
        observations.append(
            Observation(
                id=_observation_id("contract", reference),
                source=Source(kind=SourceKind.REPOSITORY, reference=reference),
                observed=ObservationData(
                    kind="source-of-truth",
                    summary=(
                        f"{reference} declares an authority or ownership boundary in "
                        f"{len(matches)} place(s). Whether that authority still holds "
                        f"cannot be checked from this repository."
                    ),
                ),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.EXTERNAL_FACT,
            )
        )

    for name in ("contracts", "schemas/contracts", "api"):
        for found in _walk(path, name):
            if not found.is_dir():
                continue
            reference = str(found.relative_to(path))
            observations.append(
                Observation(
                    id=_observation_id("contract-dir", reference),
                    source=Source(kind=SourceKind.REPOSITORY, reference=reference),
                    observed=ObservationData(
                        kind="source-of-truth",
                        summary=(
                            f"Directory {reference} holds interface contracts. Changing "
                            f"one changes an agreement with a consumer this repository "
                            f"may not contain."
                        ),
                    ),
                    captured_at=datetime.now(UTC),
                    sensitivity="low",
                    classification=ObservationClass.EXTERNAL_FACT,
                )
            )
    return observations


def _detect_repeated_reverts(path: Path) -> list[Observation]:
    """Detect files that history shows were changed and then changed back.

    A revert is a recorded instance of somebody being wrong about a file, which
    is a far better signal than churn: it marks a place where the obvious
    change turned out not to work.
    """
    if not gitinfo.is_repository(path):
        return []

    observations: list[Observation] = []
    for file_name, count in sorted(gitinfo.reverted_paths(path).items()):
        if count < 1:
            continue
        observations.append(
            Observation(
                id=_observation_id("revert", file_name),
                source=Source(kind=SourceKind.REPOSITORY, reference=file_name),
                observed=ObservationData(
                    kind="repeated-revert",
                    summary=(
                        f"{file_name} was touched by {count} revert commit(s). A change "
                        f"here was made and then undone, so the obvious edit may have "
                        f"been tried already."
                    ),
                ),
                captured_at=datetime.now(UTC),
                sensitivity="low",
                classification=ObservationClass.CANDIDATE_ANOMALY,
            )
        )
    return observations

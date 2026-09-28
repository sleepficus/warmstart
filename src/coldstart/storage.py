import io
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML


def _get_yaml() -> YAML:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    return yaml


STATE_DIRNAME = ".coldstart"


def state_dir(repo_path: Path, override: Path | None = None) -> Path:
    """Where WarmStart keeps its state for `repo_path`.

    Normally `<repo>/.coldstart`. An override relocates every state file
    elsewhere, which is what makes it possible to analyse a repository without
    writing anything into it — useful for a repository you do not own, one with
    a dirty working tree you must not disturb, or a measurement run that has to
    be provably read-only.

    Publication is the exception and remains a write to `AGENTS.md`, because
    changing that file is the entire point of publishing.
    """
    return override if override is not None else repo_path / STATE_DIRNAME


def _target_mode(path: Path) -> int:
    """Return the file mode a replacement for `path` should carry.

    NamedTemporaryFile creates 0600. Renaming it over an existing file would
    silently narrow that file's permissions — publishing to a world-readable
    AGENTS.md would make it owner-only. Preserve the existing mode, and fall
    back to the process umask for a new file.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically, preserving its permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = _target_mode(path)

    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, suffix=path.suffix
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        # A failed dump must not leave a stray temp file beside the real one.
        if tmp_name is not None:
            with suppress(OSError):
                os.unlink(tmp_name)


def save_state(obj: Any, path: Path) -> None:
    """Atomically save state to a YAML file."""
    yaml = _get_yaml()

    data: Any
    if isinstance(obj, list):
        data = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in obj
        ]
    elif isinstance(obj, BaseModel):
        data = obj.model_dump(mode="json")
    else:
        data = obj

    buffer = io.StringIO()
    yaml.dump(data, buffer)
    atomic_write_text(path, buffer.getvalue())


def load_state[T: BaseModel](cls: type[T], path: Path) -> list[T]:
    """Load a list of state objects from a YAML file."""
    if not path.exists():
        return []

    yaml = _get_yaml()
    try:
        data = yaml.load(path)
        if not data:
            return []

        if not isinstance(data, list):
            data = [data]

        return [cls.model_validate(item) for item in data]
    except Exception as e:
        # Includes ruamel parse errors and pydantic ValidationError: any
        # unreadable state file is reported as one actionable error rather
        # than surfacing a library-specific exception to the CLI.
        raise ValueError(f"Failed to load state from {path}: {e}") from e

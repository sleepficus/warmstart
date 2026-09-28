"""Authorized session sources and transcript reading.

Only one format is supported: Claude Code JSONL transcripts. Supporting one
format well is the specification's instruction, and each additional format is
another parser applied to untrusted input.

A source must be declared in `.coldstart/config.yaml` before it can be read.
Authorization is a deliberate human act, not a path argument.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from coldstart.domain import Sensitivity

CLAUDE_CODE = "claude-code"
SUPPORTED_FORMATS = {CLAUDE_CODE}

DEFAULT_RETENTION_DAYS = 30


class SourceError(Exception):
    """Raised when a session source is unusable or unauthorized."""


@dataclass(frozen=True)
class SessionSource:
    """A session source the user has explicitly authorized."""

    name: str
    format: str
    path: Path
    sensitivity: str = Sensitivity.CONFIDENTIAL.value
    retention_days: int = DEFAULT_RETENTION_DAYS


@dataclass
class Turn:
    """One conversational turn, normalized away from the on-disk schema."""

    index: int
    role: str
    uuid: str | None
    timestamp: datetime | None
    text: str
    #: True only for text a human actually typed. Tool results arrive with
    #: role "user" and must never be mistaken for human speech.
    is_human_authored: bool = False
    is_tool_result: bool = False
    tool_failed: bool = False
    session_id: str = ""
    meta_blocks: list[str] = field(default_factory=list)


def load_sources(config: dict[str, Any]) -> dict[str, SessionSource]:
    """Read authorized session sources from a loaded config mapping."""
    raw = (config.get("sources") or {}).get("sessions") or []
    sources: dict[str, SessionSource] = {}

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        fmt = str(entry.get("format", "")).strip()
        path = str(entry.get("path", "")).strip()
        if not name or not path:
            continue
        if fmt not in SUPPORTED_FORMATS:
            raise SourceError(
                f"Session source '{name}' declares unsupported format '{fmt}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_FORMATS))}."
            )
        sources[name] = SessionSource(
            name=name,
            format=fmt,
            path=Path(path).expanduser(),
            # An undeclared classification is treated as confidential, not as
            # public. Silence is not permission.
            sensitivity=str(entry.get("sensitivity") or Sensitivity.CONFIDENTIAL.value),
            retention_days=int(entry.get("retention_days") or DEFAULT_RETENTION_DAYS),
        )

    return sources


def authorization_hint(name: str) -> str:
    """Explain exactly how to authorize a source, without doing it for them."""
    return (
        f"Session source '{name}' is not authorized.\n"
        f"\n"
        f"Reading agent transcripts is opt-in. Add the source to\n"
        f".coldstart/config.yaml yourself:\n"
        f"\n"
        f"  sources:\n"
        f"    repository: true\n"
        f"    sessions:\n"
        f"      - name: {name}\n"
        f"        format: claude-code\n"
        f"        path: ~/.claude/projects/<project-slug>\n"
        f"        sensitivity: confidential\n"
        f"        retention_days: 30\n"
        f"\n"
        f"Only add a source whose contents you are willing to have reviewed."
    )


def _iter_transcript_files(source: SessionSource) -> list[Path]:
    if source.path.is_file():
        return [source.path]
    if source.path.is_dir():
        return sorted(source.path.glob("*.jsonl"))
    raise SourceError(f"Session source '{source.name}' path does not exist: {source.path}")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# Wrappers the harness injects into user turns. Their contents are machinery or
# quoted material, never something the human said, so they are stripped before
# a turn is considered human-authored.
_META_WRAPPERS = (
    ("<system-reminder>", "</system-reminder>"),
    ("<command-name>", "</command-name>"),
    ("<command-message>", "</command-message>"),
    ("<command-args>", "</command-args>"),
    ("<local-command-stdout>", "</local-command-stdout>"),
    ("<user-prompt-submit-hook>", "</user-prompt-submit-hook>"),
)


def _strip_meta(text: str) -> tuple[str, list[str]]:
    """Remove harness-injected wrappers, returning the remainder and what was cut."""
    removed: list[str] = []
    cleaned = text
    for open_tag, close_tag in _META_WRAPPERS:
        while open_tag in cleaned:
            start = cleaned.index(open_tag)
            end = cleaned.find(close_tag, start)
            if end == -1:
                removed.append(open_tag)
                cleaned = cleaned[:start]
                break
            removed.append(open_tag)
            cleaned = cleaned[:start] + cleaned[end + len(close_tag) :]
    return cleaned.strip(), removed


_TOOL_FAILURE_MARKERS = (
    "traceback (most recent call last)",
    "assertionerror",
    "no such file or directory",
    "command not found",
    "syntaxerror",
    "failed",
    "error:",
    "exit code 1",
    "exception",
)


def _block_text(block: dict[str, Any]) -> str:
    content = block.get("content", block.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def read_turns(source: SessionSource) -> Iterator[Turn]:
    """Yield normalized turns from an authorized source.

    Malformed lines are skipped rather than raising: a transcript is untrusted
    input, and one unparseable record must not abort a harvest.
    """
    if source.format != CLAUDE_CODE:
        raise SourceError(f"Unsupported session format: {source.format}")

    index = 0
    for transcript in _iter_transcript_files(source):
        try:
            handle = transcript.open(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SourceError(f"Cannot read transcript {transcript}: {exc}") from exc

        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue

                record_type = record.get("type")
                if record_type not in ("user", "assistant"):
                    continue

                message = record.get("message")
                if not isinstance(message, dict):
                    continue

                role = str(message.get("role") or record_type)
                content = message.get("content")
                session_id = str(record.get("sessionId") or "")
                uuid = record.get("uuid")
                timestamp = _parse_timestamp(record.get("timestamp"))

                if isinstance(content, str):
                    text, removed = _strip_meta(content)
                    index += 1
                    yield Turn(
                        index=index,
                        role=role,
                        uuid=str(uuid) if uuid else None,
                        timestamp=timestamp,
                        text=text,
                        is_human_authored=role == "user" and bool(text),
                        session_id=session_id,
                        meta_blocks=removed,
                    )
                    continue

                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")

                    if block_type == "text":
                        text, removed = _strip_meta(_block_text(block))
                        if not text:
                            continue
                        index += 1
                        yield Turn(
                            index=index,
                            role=role,
                            uuid=str(uuid) if uuid else None,
                            timestamp=timestamp,
                            text=text,
                            # A `text` block under role "user" is genuine human
                            # input; a `tool_result` block is not, even though
                            # both arrive with role "user".
                            is_human_authored=role == "user",
                            session_id=session_id,
                            meta_blocks=removed,
                        )

                    elif block_type == "tool_result":
                        text = _block_text(block)
                        lowered = text.lower()
                        failed = bool(block.get("is_error")) or any(
                            marker in lowered for marker in _TOOL_FAILURE_MARKERS
                        )
                        index += 1
                        yield Turn(
                            index=index,
                            role=role,
                            uuid=str(uuid) if uuid else None,
                            timestamp=timestamp,
                            text=text,
                            is_human_authored=False,
                            is_tool_result=True,
                            tool_failed=failed,
                            session_id=session_id,
                        )

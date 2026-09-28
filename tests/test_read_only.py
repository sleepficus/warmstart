"""Prove that `--state-dir` leaves the target repository untouched.

This is the guarantee that makes it safe to point WarmStart at a repository you
do not own, or at one whose working tree must not be disturbed during a
measurement run. It is asserted by fingerprinting every file in the repository
before and after, rather than by inspecting the code and trusting it.

`publish` is deliberately excluded: writing to AGENTS.md is what publishing is.
"""

import hashlib
import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from coldstart.cli import app

runner = CliRunner()


def _fingerprint(root: Path) -> dict[str, str]:
    """Content hash of every file in the tree, keyed by relative path."""
    prints: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            prints[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return prints


def _make_repo(root: Path) -> None:
    src = root / "src"
    src.mkdir()
    (src / "processor.py").write_text("def pad_id(uid):\n    return uid.zfill(8)\n")
    (root / "AGENTS.md").write_text("# Team notes\n\nDo not lose this.\n")
    (root / "README.md").write_text("# Project\n")
    (root / "CODEOWNERS").write_text("src/ @team\n")


def _session(root: Path) -> Path:
    transcript = root / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s1",
                "uuid": "a1",
                "timestamp": "2026-08-04T10:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "src/processor.py coerces ids to ints."}],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "h1",
                "timestamp": "2026-08-04T10:01:00.000Z",
                "message": {
                    "role": "user",
                    "content": "No, that's wrong. src/processor.py keeps them as strings.",
                },
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n")
    return transcript


def test_whole_workflow_writes_nothing_into_the_repository() -> None:
    """Every command except publish must leave the repository byte-identical."""
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
        repo = Path(repo_dir)
        state = Path(state_dir) / "cs-state"
        _make_repo(repo)
        transcript = _session(repo)

        before = _fingerprint(repo)

        assert runner.invoke(app, ["init", str(repo), "--state-dir", str(state)]).exit_code == 0

        # Authorize the session source in the relocated config.
        (state / "config.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  repository: true\n"
            "  sessions:\n"
            "    - name: hist\n"
            "      format: claude-code\n"
            f"      path: {transcript}\n"
            "      sensitivity: confidential\n"
        )

        for args in (
            ["inspect", str(repo), "--state-dir", str(state)],
            ["harvest", "--source", "hist", str(repo), "--state-dir", str(state)],
            ["compile", str(repo), "--state-dir", str(state)],
            ["questions", str(repo), "--state-dir", str(state), "--generate"],
            ["review", str(repo), "--state-dir", str(state), "--approve-all", "--approver", "t"],
            ["verify", str(repo), "--state-dir", str(state)],
        ):
            result = runner.invoke(app, args)
            assert result.exit_code in (0, 1), f"{args[0]} crashed: {result.output}"

        after = _fingerprint(repo)

        assert after == before, (
            "WarmStart modified the repository despite --state-dir. "
            f"Changed: {sorted(set(before) ^ set(after))}"
        )
        assert not (repo / ".coldstart").exists(), "state leaked into the repository"
        assert (state / "observations.yaml").exists(), "state was not written to --state-dir"


def test_private_provenance_relocates_and_stays_owner_only() -> None:
    """Relocating state must not weaken the private store's permissions."""
    import stat

    from coldstart.harvesting import provenance_path

    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
        repo = Path(repo_dir)
        state = Path(state_dir) / "cs-state"
        _make_repo(repo)
        transcript = _session(repo)

        runner.invoke(app, ["init", str(repo), "--state-dir", str(state)])
        (state / "config.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  sessions:\n"
            "    - name: hist\n"
            "      format: claude-code\n"
            f"      path: {transcript}\n"
        )
        runner.invoke(app, ["harvest", "--source", "hist", str(repo), "--state-dir", str(state)])

        stored = provenance_path(repo, state)
        assert stored.exists()
        assert not (repo / ".coldstart").exists()
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700


def test_default_still_writes_inside_the_repository() -> None:
    """Without --state-dir the ordinary behaviour is unchanged."""
    with tempfile.TemporaryDirectory() as repo_dir:
        repo = Path(repo_dir)
        _make_repo(repo)

        runner.invoke(app, ["init", str(repo)])

        assert (repo / ".coldstart" / "config.yaml").exists()


def test_inspect_preserves_harvested_observations() -> None:
    """Re-inspecting must not destroy session-derived observations.

    Found by running against a real repository: `inspect` overwrote
    observations.yaml wholesale, so every harvested session observation
    vanished the next time the repository was inspected. Session observations
    cannot be regenerated by re-inspection — the evidence is in a transcript,
    not in the repository.
    """
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
        repo = Path(repo_dir)
        state = Path(state_dir) / "cs-state"
        _make_repo(repo)
        transcript = _session(repo)

        runner.invoke(app, ["init", str(repo), "--state-dir", str(state)])
        (state / "config.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  sessions:\n"
            "    - name: hist\n"
            "      format: claude-code\n"
            f"      path: {transcript}\n"
        )
        runner.invoke(app, ["harvest", "--source", "hist", str(repo), "--state-dir", str(state)])

        from coldstart.domain import Observation
        from coldstart.storage import load_state

        obs_path = state / "observations.yaml"
        before = {o.id for o in load_state(Observation, obs_path)}
        session_before = {o.id for o in load_state(Observation, obs_path) if "SESSION" in o.id}
        assert session_before, "fixture produced no session observations"

        result = runner.invoke(app, ["inspect", str(repo), "--state-dir", str(state)])
        assert result.exit_code == 0

        after = {o.id for o in load_state(Observation, obs_path)}
        assert session_before <= after, "inspect destroyed harvested observations"
        assert before <= after

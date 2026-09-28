"""Tests for repository inspection."""

import tempfile
from pathlib import Path

from ruamel.yaml import YAML

from coldstart.inspection import init_repository, inspect_repository


class TestInitRepository:
    def test_creates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            (path / ".git").mkdir()
            (path / "AGENTS.md").touch()

            config = init_repository(path)
            assert config["repository"]["git_available"] is True
            assert config["repository"]["has_agents_md"] is True

            config_file = path / ".coldstart" / "config.yaml"
            assert config_file.exists()

            yaml = YAML(typ="safe")
            with open(config_file) as f:
                saved = yaml.load(f)
            assert saved["version"] == 1

    def test_detects_python_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            (path / "main.py").write_text("print('hello')")
            config = init_repository(path)
            assert "python" in config["repository"]["languages"]

    def test_warns_on_no_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            config = init_repository(path)
            assert config["repository"]["git_available"] is False

    def test_does_not_overwrite_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            coldstart = path / ".coldstart"
            coldstart.mkdir()
            config_file = coldstart / "config.yaml"
            config_file.write_text("version: 99\n")

            init_repository(path)
            content = config_file.read_text()
            assert "99" in content  # original preserved


class TestInspectRepository:
    def test_detects_zfill_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            src = path / "src"
            src.mkdir()
            (src / "ids.py").write_text("def pad(x): return x.zfill(8)\n")

            obs = inspect_repository(path)
            type_obs = [o for o in obs if o.observed.kind == "type-inconsistency"]
            assert len(type_obs) >= 1

    def test_detects_entry_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            (path / "main.py").write_text('if __name__ == "__main__":\n    pass\n')
            obs = inspect_repository(path)
            ep_obs = [o for o in obs if o.observed.kind == "entry-point"]
            assert len(ep_obs) >= 1

    def test_detects_generated_file_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            (path / "gen.py").write_text("# DO NOT EDIT - Generated\ncode = 1\n")
            obs = inspect_repository(path)
            gen_obs = [o for o in obs if o.observed.kind == "generated-file"]
            assert len(gen_obs) >= 1

    def test_detects_agent_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            (path / "AGENTS.md").write_text("# Agent rules\n")
            obs = inspect_repository(path)
            agent_obs = [o for o in obs if o.observed.kind == "agent-instruction-file"]
            assert len(agent_obs) >= 1

    def test_detects_test_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            tests = path / "tests"
            tests.mkdir()
            (tests / "test_foo.py").write_text("def test_x(): pass\n")
            obs = inspect_repository(path)
            test_obs = [o for o in obs if o.observed.kind == "test-infrastructure"]
            assert len(test_obs) >= 1

    def test_skips_venv_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            venv = path / ".venv" / "lib"
            venv.mkdir(parents=True)
            (venv / "module.py").write_text('x = "1".zfill(5)\n')
            obs = inspect_repository(path)
            assert len(obs) == 0

    def test_observation_ids_are_stable_when_other_files_appear(self) -> None:
        """Ids must not renumber, or approvals rebind to the wrong candidate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            src = path / "src"
            src.mkdir()
            (src / "zzz_ids.py").write_text("def pad(x): return x.zfill(8)\n")

            before = {o.source.reference: o.id for o in inspect_repository(path)}

            # A new file that sorts ahead of the existing one.
            (src / "aaa_other.py").write_text("def pad(x): return x.zfill(4)\n")
            after = {o.source.reference: o.id for o in inspect_repository(path)}

            assert before["src/zzz_ids.py"] == after["src/zzz_ids.py"]

    def test_inspection_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            src = path / "src"
            src.mkdir()
            for name in ("b.py", "a.py", "c.py"):
                (src / name).write_text("def pad(x): return x.zfill(8)\n")

            first = [o.id for o in inspect_repository(path)]
            second = [o.id for o in inspect_repository(path)]

            assert first == second
            assert first == sorted(first) or len(first) == len(set(first))

    def test_empty_repo_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            obs = inspect_repository(path)
            assert obs == []

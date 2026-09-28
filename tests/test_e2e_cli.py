"""End-to-end CLI integration test.

Exercises the complete public-CLI vertical slice:
1. Init
2. Create fixture with leading-zero pattern
3. Inspect → observations persisted
4. Compile → candidates created with proposals
5. Review → approval records written
6. Publish → managed section in AGENTS.md with human content preserved
7. Idempotent re-publish
8. Modify dependency
9. Verify → candidate quarantined
10. Re-publish → stale note removed
11. Deferred commands return exit 1
"""

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from coldstart.cli import app
from coldstart.domain import ApprovalRecord, Candidate, KnowledgeState
from coldstart.storage import load_state

runner = CliRunner()


def test_e2e_vertical_slice() -> None:
    """Full deterministic core vertical slice via public CLI commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)

        # === 1. Init ===
        result = runner.invoke(app, ["init", str(repo)])
        assert result.exit_code == 0, f"init failed: {result.output}"
        assert (repo / ".coldstart" / "config.yaml").exists()

        # === 2. Create fixture ===
        src = repo / "src"
        src.mkdir()
        fixture = src / "processor.py"
        fixture.write_text("def pad_id(uid): return uid.zfill(8)\n")

        # === 3. Inspect ===
        result = runner.invoke(app, ["inspect", str(repo)])
        assert result.exit_code == 0, f"inspect failed: {result.output}"
        assert "observations" in result.output.lower()
        obs_path = repo / ".coldstart" / "observations.yaml"
        assert obs_path.exists(), "Observations not persisted"

        # === 4. Compile ===
        result = runner.invoke(app, ["compile", str(repo)])
        assert result.exit_code == 0, f"compile failed: {result.output}"
        candidates_path = repo / ".coldstart" / "candidates.yaml"
        assert candidates_path.exists()
        cands = load_state(Candidate, candidates_path)
        assert len(cands) >= 1
        # Verify durable_form is set
        assert any(c.durable_form is not None for c in cands)

        # === 5. Review with --approve-all ===
        result = runner.invoke(
            app, ["review", str(repo), "--approve-all", "--approver", "test-runner"]
        )
        assert result.exit_code == 0, f"review failed: {result.output}"
        assert "Approved" in result.output or "approval" in result.output.lower()

        # Verify approval records written
        approvals_path = repo / ".coldstart" / "approvals.yaml"
        assert approvals_path.exists(), "Approval records not persisted"
        approvals = load_state(ApprovalRecord, approvals_path)
        assert len(approvals) >= 1

        # Verify candidates are APPROVED with valid transition history
        cands = load_state(Candidate, candidates_path)
        approved_cands = [c for c in cands if c.status == KnowledgeState.APPROVED]
        assert len(approved_cands) >= 1
        for c in approved_cands:
            assert len(c.transition_history) >= 1
            assert c.transition_history[-1].to_state == KnowledgeState.APPROVED

        # === 6. Publish ===
        agents_md = repo / "AGENTS.md"
        agents_md.write_text("# My Human Content\n\nDo not delete this.\n")
        result = runner.invoke(app, ["publish", str(repo)])
        assert result.exit_code == 0, f"publish failed: {result.output}"
        content = agents_md.read_text()
        assert "<!-- warmstart:start -->" in content
        assert "# My Human Content" in content
        assert "Do not delete this." in content
        # At least one candidate ID should appear
        assert any(c.id in content for c in approved_cands)

        # === 7. Idempotent re-publish ===
        content_before = agents_md.read_text()
        result = runner.invoke(app, ["publish", str(repo)])
        assert result.exit_code == 0
        assert agents_md.read_text() == content_before

        published_ids = [c.id for c in approved_cands]

        # === 8. Modify (not delete) a declared dependency ===
        # Every compiled dependency must carry a hash, or a content change is
        # invisible to verification and the note outlives the code it describes.
        for c in approved_cands:
            assert c.depends_on, f"{c.id} declares no dependency"
            for dep in c.depends_on:
                recorded = dep.hash if not isinstance(dep, dict) else dep.get("hash")
                assert recorded, f"{c.id} dependency {dep} recorded without a hash"

        fixture.write_text("def pad_id(uid): return str(int(uid))  # strips zeros\n")

        # === 9. Verify → quarantine, and exit 1 while the note is still live ===
        result = runner.invoke(app, ["verify", str(repo)])
        assert result.exit_code == 1, "verify must fail while AGENTS.md serves a stale note"

        cands = load_state(Candidate, candidates_path)
        quarantined = [c for c in cands if c.status == KnowledgeState.QUARANTINED]
        assert quarantined, "modified dependency did not quarantine any candidate"
        assert {c.id for c in quarantined} == set(published_ids)
        assert not [c for c in cands if c.status == KnowledgeState.APPROVED]

        # The stale note is still in AGENTS.md at this point — that is exactly
        # why verify exits non-zero rather than reporting success.
        assert any(cid in agents_md.read_text() for cid in published_ids)

        # === 10. Re-publish → stale note removed, human content preserved ===
        result = runner.invoke(app, ["publish", str(repo)])
        assert result.exit_code == 0
        content = agents_md.read_text()
        for cid in published_ids:
            assert cid not in content, f"quarantined {cid} still published"
        assert "# My Human Content" in content
        assert "Do not delete this." in content

        # === 11. Verify is now clean ===
        result = runner.invoke(app, ["verify", str(repo)])
        assert result.exit_code == 0


def test_recompile_preserves_review_outcome() -> None:
    """Re-running compile must not revert an approved candidate to candidate.

    Recompiling used to overwrite candidates.yaml wholesale, which silently
    discarded approvals and made the next publish empty the managed section
    while approvals.yaml still claimed the notes were approved.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        runner.invoke(app, ["init", str(repo)])
        src = repo / "src"
        src.mkdir()
        (src / "processor.py").write_text("def pad_id(uid): return uid.zfill(8)\n")

        runner.invoke(app, ["compile", str(repo)])
        runner.invoke(app, ["review", str(repo), "--approve-all", "--approver", "test-runner"])

        candidates_path = repo / ".coldstart" / "candidates.yaml"
        approved_before = [
            c.id
            for c in load_state(Candidate, candidates_path)
            if c.status == KnowledgeState.APPROVED
        ]
        assert approved_before

        result = runner.invoke(app, ["compile", str(repo)])
        assert result.exit_code == 0

        after = load_state(Candidate, candidates_path)
        approved_after = [c.id for c in after if c.status == KnowledgeState.APPROVED]
        assert approved_after == approved_before, "recompile discarded the approval"
        for c in after:
            if c.id in approved_before:
                assert c.transition_history, "recompile discarded transition history"

        agents_md = repo / "AGENTS.md"
        agents_md.write_text("# Human\n")
        result = runner.invoke(app, ["publish", str(repo)])
        assert result.exit_code == 0
        content = agents_md.read_text()
        assert all(cid in content for cid in approved_before)


def test_harvest_requires_an_authorized_source() -> None:
    """Reading transcripts is opt-in; an undeclared source must be refused."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        runner.invoke(app, ["init", str(repo)])

        result = runner.invoke(app, ["harvest", "--source", "nowhere", str(repo)])

        assert result.exit_code == 1
        assert "not authorized" in result.output
        # The refusal must explain how to authorize, without doing it.
        assert "sources:" in result.output
        config = (repo / ".coldstart" / "config.yaml").read_text()
        assert "nowhere" not in config, "refusal must not authorize the source"


def test_dry_run_publish() -> None:
    """Publish --dry-run should show output without mutating."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        runner.invoke(app, ["init", str(repo)])

        src = repo / "src"
        src.mkdir()
        (src / "processor.py").write_text("def pad_id(uid): return uid.zfill(8)\n")

        runner.invoke(app, ["compile", str(repo)])
        runner.invoke(app, ["review", str(repo), "--approve-all", "--approver", "test-runner"])

        agents_md = repo / "AGENTS.md"
        agents_md.write_text("# Original\n")
        original = agents_md.read_text()

        result = runner.invoke(app, ["publish", str(repo), "--dry-run"])
        assert result.exit_code == 0
        assert agents_md.read_text() == original  # not mutated


def test_publish_fails_on_duplicate_sections() -> None:
    """Publication must fail on duplicate managed sections."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        runner.invoke(app, ["init", str(repo)])
        src = repo / "src"
        src.mkdir()
        (src / "processor.py").write_text("def pad_id(uid): return uid.zfill(8)\n")
        runner.invoke(app, ["compile", str(repo)])
        runner.invoke(app, ["review", str(repo), "--approve-all", "--approver", "test-runner"])

        agents_md = repo / "AGENTS.md"
        agents_md.write_text(
            "<!-- coldstart:start -->\nold\n<!-- coldstart:end -->\n"
            "Middle\n"
            "<!-- coldstart:start -->\nold2\n<!-- coldstart:end -->\n"
        )

        result = runner.invoke(app, ["publish", str(repo)])
        assert result.exit_code == 1

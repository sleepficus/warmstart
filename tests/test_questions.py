"""Tests for Phase 5 selective clarification.

The load-bearing property is negative: a question is only raised when all four
preconditions hold, and no unanswered question ever promotes its candidate.
"""

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from coldstart.cli import app
from coldstart.domain import (
    Candidate,
    KnowledgeState,
    OperationalEffect,
    Question,
    Scope,
)
from coldstart.questions import (
    QUESTION_ANSWERED,
    QUESTION_DEFERRED,
    QUESTION_UNKNOWN,
    apply_answer,
    apply_defer,
    apply_reject,
    apply_route,
    apply_unknown,
    build_question,
    generate_questions,
    resolve_owner,
)
from coldstart.storage import load_state, save_state

runner = CliRunner()


def _candidate(
    candidate_id: str = "CS-001",
    kind: str = "constraint",
    status: KnowledgeState = KnowledgeState.CANDIDATE,
    avoids: str = "Manual edits being overwritten by the generator.",
    paths: list[str] | None = None,
) -> Candidate:
    return Candidate(
        id=candidate_id,
        claim="Generated file is owned elsewhere",
        status=status,
        kind=kind,
        confidence="medium",
        derived_from=[],
        evidence=[{"kind": "observation", "source": "generated/rates.py"}],
        operational_effect=OperationalEffect(
            changes="Do not edit manually.", avoids=avoids, severity="medium"
        ),
        scope=Scope(
            paths=paths if paths is not None else ["generated/rates.py"],
            environments=[],
            authorities=[],
        ),
        depends_on=[],
        distribution="internal",
    )


def _repo_with_codeowners(tmpdir: str, content: str = "generated/ @data-platform\n") -> Path:
    repo = Path(tmpdir)
    (repo / "CODEOWNERS").write_text(content)
    return repo


class TestOwnerResolution:
    def test_resolves_owner_from_codeowners(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            owner = resolve_owner(repo, ["generated/rates.py"])
            assert owner is not None
            assert owner.kind == "codeowners"
            assert owner.value == "@data-platform"

    def test_no_codeowners_file_means_no_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assert resolve_owner(Path(tmpdir), ["generated/rates.py"]) is None

    def test_last_matching_rule_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir, "* @everyone\ngenerated/ @data-platform\n")
            owner = resolve_owner(repo, ["generated/rates.py"])
            assert owner is not None
            assert owner.value == "@data-platform"


class TestGenerationPreconditions:
    def test_generates_for_unowned_generated_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            generated = generate_questions([_candidate()], repo)
            assert len(generated) == 1
            assert "permanently owned" in generated[0].prompt
            assert generated[0].publish_to_agents is False

    def test_no_question_without_an_identifiable_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assert generate_questions([_candidate()], Path(tmpdir)) == []

    def test_no_question_when_nothing_would_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            assert generate_questions([_candidate(avoids="")], repo) == []

    def test_no_question_for_settled_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            settled = _candidate()
            settled.transition_to(KnowledgeState.APPROVED, reason="approved")
            assert generate_questions([settled], repo) == []

    def test_generation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            candidate = _candidate()
            first = generate_questions([candidate], repo)
            second = generate_questions([candidate], repo, existing=first)
            assert second == []

    def test_question_id_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            a = build_question(_candidate(), "Same question?", repo)
            b = build_question(_candidate(), "Same question?", repo)
            assert a.id == b.id


class TestResolution:
    def test_answer_returns_candidate_to_review_without_promoting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            fresh = _candidate()
            fresh.transition_to(KnowledgeState.NEEDS_CLARIFICATION, reason="unclear")
            question = build_question(fresh, "Intentional?", repo)

            apply_answer(question, fresh, "Yes, vendor-owned.", "maintainer")

            assert question.status == QUESTION_ANSWERED
            assert question.answer == "Yes, vendor-owned."
            assert fresh.status == KnowledgeState.CANDIDATE, "answering must not promote"

    def test_defer_leaves_candidate_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            candidate = _candidate()
            question = build_question(candidate, "Intentional?", repo)

            apply_defer(question, "Waiting on the data team")

            assert question.status == QUESTION_DEFERRED
            assert candidate.status == KnowledgeState.CANDIDATE
            assert not candidate.transition_history

    def test_unknown_is_not_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            candidate = _candidate()
            question = build_question(candidate, "Intentional?", repo)

            apply_unknown(question)

            assert question.status == QUESTION_UNKNOWN
            assert candidate.status == KnowledgeState.CANDIDATE

    def test_reject_rejects_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            candidate = _candidate()
            question = build_question(candidate, "Intentional?", repo)

            apply_reject(question, candidate, "maintainer")

            assert candidate.status == KnowledgeState.REJECTED

    def test_route_names_an_owner_and_keeps_it_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            question = build_question(_candidate(), "Intentional?", repo)

            apply_route(question, "@platform-team")

            assert question.owner.value == "@platform-team"
            assert question.status == "open"


class TestQuestionsCli:
    def test_generate_list_and_defer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            coldstart = repo / ".coldstart"
            coldstart.mkdir()
            save_state([_candidate()], coldstart / "candidates.yaml")

            result = runner.invoke(app, ["questions", str(repo), "--generate"])
            assert result.exit_code == 0
            assert "Generated 1 question" in result.output

            stored = load_state(Question, coldstart / "questions.yaml")
            assert len(stored) == 1
            qid = stored[0].id

            result = runner.invoke(app, ["questions", str(repo)])
            assert result.exit_code == 0
            assert qid in result.output
            assert "1 unresolved" in result.output

            result = runner.invoke(app, ["questions", str(repo), "--defer", qid])
            assert result.exit_code == 0
            assert "deferred" in result.output

            after = load_state(Question, coldstart / "questions.yaml")
            assert after[0].status == QUESTION_DEFERRED
            candidates = load_state(Candidate, coldstart / "candidates.yaml")
            assert candidates[0].status == KnowledgeState.CANDIDATE

    def test_answer_requires_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _repo_with_codeowners(tmpdir)
            coldstart = repo / ".coldstart"
            coldstart.mkdir()
            save_state([_candidate()], coldstart / "candidates.yaml")
            runner.invoke(app, ["questions", str(repo), "--generate"])
            qid = load_state(Question, coldstart / "questions.yaml")[0].id

            result = runner.invoke(app, ["questions", str(repo), "--answer", qid])
            assert result.exit_code == 2

    def test_unknown_question_id_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / ".coldstart").mkdir()
            save_state([], repo / ".coldstart" / "questions.yaml")

            result = runner.invoke(app, ["questions", str(repo), "--defer", "QUESTION-nope"])
            assert result.exit_code == 1

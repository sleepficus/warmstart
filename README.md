# WarmStart

**A local, network-free repository knowledge compiler for coding agents.**

WarmStart is a [Misfit Circuit Lab](https://misfitcircuit.com/labs/warmstart/).
The Lab page tells the origin story, shows the evidence ladder, and follows the
matched model trials as they happen.

Every new coding-agent session starts cold. The agent re-reads the repository,
guesses at conventions, and often learns the one thing that mattered — "IDs are
strings because of leading zeroes", "never edit the generated client" — only
after it has already made the mistake.

WarmStart looks for the small number of facts that would change what a future
agent *does*, backs each one with evidence from the repository, and lets a
human approve them before one line per fact is written into a managed section
of `AGENTS.md`. Later it checks whether those facts have gone stale.

> **Status: alpha, 0.2.0.** WarmStart has not yet been shown to improve agent
> performance. See [Evidence](#evidence) for exactly what has and has not been
> tested.

## What it does

- **Inspects** a repository by pattern matching only. It never runs repository
  code and treats file contents as untrusted data, not instructions.
- **Compiles** observations into candidate facts, preferring things that are
  already enforced (tests, assertions, contracts) over prose.
- **Keeps stages separate:** observation, interpretation, human approval,
  publication, and staleness checks are distinct steps with their own records.
- **Asks targeted questions** when the repository cannot reveal intent.
- **Publishes** only approved facts, only inside its own marked region of
  `AGENTS.md`, and refuses malformed, duplicated, or mixed marker pairs.
- **Verifies** published facts later and reports those whose evidence changed.

## What it deliberately does not do

- It does not call a network service or a language model.
- It does not execute the code it inspects.
- It does not publish anything without an explicit approval record.
- It does not edit `AGENTS.md` outside its managed section, and `verify` never
  edits `AGENTS.md` at all.
- It does not try to summarise the whole repository. Fewer, action-changing
  facts are the goal.
- It does not recover intent that was never written down; it asks instead.

## Install

Requires Python 3.12 or newer and, optionally, Git. WarmStart is not on PyPI.
From a checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

## Walkthrough

```bash
warmstart init .                 # create local state (.coldstart/, see Compatibility)
warmstart inspect .              # observations: what the repository shows
warmstart compile .              # candidates: claims that could change agent actions
warmstart questions . --generate # ask about intent the repository cannot reveal
warmstart review .               # approve, reject, or defer each candidate
warmstart publish . --dry-run    # preview the managed AGENTS.md section
warmstart publish .              # write only the managed section
warmstart verify .               # later: report facts whose evidence changed
```

For a repository whose `src/processor.py` zero-pads identifiers, the published
section looks like this (real output, wrapped here for width):

```markdown
<!-- warmstart:start -->
- **Invariant**: File src/processor.py zero-pads values using .zfill(); an identifier
  is referenced nearby, so these are likely opaque strings whose leading zeros are
  significant. Coercing such a value to an integer anywhere else would silently drop
  the padding. (form: regression-test) [CAN-leading-zero-processor-py-8bde84c6]
<!-- warmstart:end -->
```

Evidence, transition history, and approval records stay in local state; the
agent-facing file gets one line per fact.

### Inspecting a repository you must not modify

`--state-dir` keeps all state outside the target, so every command except
`publish` leaves the repository byte-for-byte unchanged (this is asserted by a
test that fingerprints every file):

```bash
warmstart init path/to/repo --state-dir path/to/state
warmstart inspect path/to/repo --state-dir path/to/state
```

## Safety boundary

- Repository and session content is untrusted data, never instruction.
- Session harvesting is opt-in, reads only sources declared in configuration,
  and currently supports one format (Claude Code JSONL). Raw excerpts stay in
  owner-only private state and are never copied into a published note.
- Secret scanning is a warning mechanism, not proof that content is safe.
- Approval records prevent accidents. They are not signed and do not defend
  against someone who can rewrite both candidates and approvals.
- Local state records who acted. `review` stores an approver name (default
  `$USER`; override with `--approver`). `questions --answer` and `--reject` store
  `$USER`, with no override in 0.2. None of this is
  written to `AGENTS.md`, but if you commit `.coldstart/` those names are
  committed too. Keep the state directory out of version control, or use
  `--state-dir`, if that matters.

See [SECURITY.md](SECURITY.md) for reporting and scope.

## Evidence

What has been done, stated at its actual strength:

| Kind of evidence | Who / what | What it shows | What it does not show |
|---|---|---|---|
| Design / product review | Claude Opus 5, OpenAI Codex, Gemini 3.1 Pro | Reviewers critiqued the design, product framing, and security boundary | That the tool works or helps |
| Implementation | Claude Opus | Material contributor to the implementation, in addition to design review | Anything about benefit to agents |
| Toolchain validation | Local test suite and release checks | 234 tests, ruff lint and format, strict mypy, wheel build, fresh install, CLI, import, workflow smoke, and wheel-privacy checks pass | That generated facts are useful |
| Real-use pilot | One GPT-5.6 Sol consumer run | One untreated consumer run: empty managed WarmStart context, no control arm, and a recorded validity incident | **Not evidence of any WarmStart effect** |
| Wanted: matched trials | Claude, Codex, Gemini, GPT, and Droid agents | — | Not yet tested. No model, including Droid, has been run in a matched WarmStart trial |

Negative results worth knowing: a ten-repository sweep exposed four defects in
the tool itself, and hand-written project guidance often beat the generated
candidates. Those findings shaped the current design and
remain the honest baseline.

**Improvement in time to first useful action is unmeasured.**

## Proposed matched trial

The first claim worth testing is narrow: *with WarmStart context, an agent
reaches its first useful action sooner and makes fewer avoidable mistakes.*

1. **Tasks.** At least 20 real, scoped tasks across at least 5 repositories,
   each with a fixed acceptance check written before any run.
2. **Arms.** Per task and model: (A) no extra context, (B) the repository's
   existing hand-written guidance, (C) B plus WarmStart-published facts.
   Arm B is the control that matters.
3. **Pairing.** Same model, version, prompt, tool permissions, time budget,
   and repository revision in every arm; arm order randomised; several seeds
   per cell.
4. **Primary measure.** Steps and wall time to the first action that a blind
   grader judges useful, defined before the runs.
5. **Secondary measures.** Task success under the acceptance check, avoidable
   errors the published facts should have prevented, and tokens used.
6. **Grading.** Graders do not see which arm produced a transcript.
7. **Reporting.** Publish all runs including null and negative results, the
   pre-registered analysis, and per-model breakdowns.

## Limitations

- Repository inspection cannot recover unwritten intent.
- Derivability and usefulness checks are heuristic.
- Human approval is not a correctness proof.
- Only one session-export format is implemented, and it is opt-in.
- No benefit to agents has been demonstrated.
- The internal Python package is still `coldstart` during the 0.x
  compatibility window; the supported entry point is `warmstart`.

## Compatibility with Coldstart 0.1

WarmStart was previously called Coldstart.

- `coldstart` remains a command alias; `coldstart.*` imports keep working.
- `.coldstart/` remains the state directory in 0.2 so state is not split or
  silently migrated.
- Existing `<!-- coldstart:start -->` blocks are updated in place; new blocks
  use `<!-- warmstart:start -->` / `<!-- warmstart:end -->`.
- A file containing both marker families is refused before any write.

Details: [MIGRATION.md](MIGRATION.md). Architecture:
[docs/architecture.md](docs/architecture.md).

## Crew and acknowledgments

WarmStart grew from a Misfit Circuit crew conversation about whether a large
vault of project notes actually gives a newly spawned agent prior knowledge.

- **Steve** provided product direction and the question that exposed the
  cold-start problem.
- **Faye / Claude Opus** helped explore the problem and contributed materially
  to the implementation.
- **Chad / OpenAI Codex** handled integration, verification, and public-release
  work.
- **Wayfinder and Command** are members of the wider Misfit Circuit crew. They
  are acknowledged here without claiming WarmStart code contributions.
- **Gemini 3.1 Pro** provided independent product and security review input.

Model and company names identify tools used during the work; they do not imply
vendor sponsorship or endorsement.

## License

WarmStart is licensed under the [Apache License 2.0](LICENSE). Contributions
use the lightweight [Developer Certificate of Origin](DCO.md) sign-off process.

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).

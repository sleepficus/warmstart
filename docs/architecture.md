# Architecture

## System boundaries

- Repository observations are interpretations, never automatically verified
  knowledge.
- Repository and session content are untrusted data, never instruction.
- The core workflow functions with no network access and no model inference.
- Analysis and filesystem mutation are separate operations.

## Module responsibilities

| Module | Responsibility |
|---|---|
| `coldstart.domain` | Models, knowledge states, the allowed-transition map |
| `coldstart.storage` | Validated YAML load/save, atomic permission-preserving writes |
| `coldstart.gitinfo` | Read-only subprocess wrapper around an installed Git |
| `coldstart.inspection` | Deterministic repository → observations |
| `coldstart.compilation` | Observations → candidates; admission checks; conflict detection |
| `coldstart.safeguards` | Durable-form selection and proposal persistence |
| `coldstart.questions` | Targeted clarification: generation, ownership, resolution |
| `coldstart.harvesting` | Authorized session sources, event extraction, safety scanning, private provenance |
| `coldstart.staleness` | Dependency verification and quarantine |
| `coldstart.publication` | The managed-section boundary in `AGENTS.md` |
| `coldstart.cli` | Typer commands; the only module that prompts a human |

## Dependency direction

```text
cli
 ├─→ inspection ─→ gitinfo ─→ (git subprocess)
 │            └─→ domain
 ├─→ compilation ─→ staleness ─→ domain
 ├─→ questions ──→ domain
 ├─→ harvesting ─→ storage ─→ domain
 ├─→ safeguards ─→ domain
 └─→ publication ─→ staleness ─→ domain
                 └─→ storage ─→ domain
```

`domain` and `storage` depend on nothing else in the package. No module imports
`cli`. Nothing below `cli` prompts, so every analysis path is testable without
simulated input.

## Domain state transitions

```text
observed → candidate
candidate → needs-verification | needs-clarification | conflicted | approved
          | quarantined | rejected
needs-verification / needs-clarification → candidate | approved | quarantined
          | rejected
conflicted → candidate | quarantined | rejected
approved → published | stale | quarantined | superseded | rejected
published → stale | quarantined | superseded
stale → candidate | quarantined | rejected
quarantined → candidate | needs-verification | rejected
superseded, rejected → terminal
```

`ALLOWED_TRANSITIONS` is the single authority. Every change records reason,
timestamp, and actor. Every non-terminal state has a path back into review, so
no candidate is ever stranded.

## Trust boundaries

1. **Repository → observation.** Pattern matching only. Repository code is
   never executed and instructions found in files are never followed.
2. **Observation → candidate.** An observation is a fact about the repository;
   a candidate is a claim about behavior. The admission checks are the gate.
3. **Candidate → published note.** Requires an approval record whose timestamp
   does not predate the approval transition, no unresolved conflict, a
   publishable distribution, and no stale dependency.
4. **WarmStart → `AGENTS.md`.** Only the delimited managed section is written,
   and only by `publish`.
5. **Session transcript → repository-safe observation.** The strictest of the
   five. Raw excerpts stay in owner-only private provenance; the only session
   content that crosses is a file path the repository itself confirms exists.
   Free text never crosses, and an event flagged for possible prompt injection
   produces no observation at all.

## Storage and publication boundaries

Detailed evidence, transition history, and approval records stay in
`.coldstart/`. The agent-facing file gets one line per note. `published.yaml`
records what is currently live.

Publication fails closed on a malformed or duplicated managed section, enforces
a hard note budget, and reports anything held back.

## Core CLI workflow

```text
init → inspect → compile → questions → review → publish → verify
```

`verify` reports but does not mutate `AGENTS.md`; it exits non-zero while a
no-longer-approved note is still published.

## Evaluation boundary

The historical evaluation harness remains separate from the core because agent
runs can require a model and network while WarmStart itself does not. The first
public release candidate omits that harness and its historical experiment
material. No measured performance benefit is claimed here.

## Deferred components

- Session formats other than Claude Code JSONL.
- Storage backends other than YAML. `storage.load_state`/`save_state` is the
  seam if one is ever needed.

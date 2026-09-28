# Security and privacy

WarmStart is local and network-free at its core. It inspects repository text as
untrusted data and does not execute target code.

Session harvesting is opt-in. Raw excerpts stay in private state, are
permission-restricted, and are not copied into published agent notes.

Before publishing a managed block, WarmStart requires an approval record,
checks scope and staleness, and refuses malformed, duplicated, or mixed marker
pairs. These controls protect against mistakes; they do not provide signed
approval or defend against an attacker who can rewrite all local state.

Local state records who acted: approval records store an approver name
(default `$USER`, override with `--approver`), and `questions --answer` and
`--reject` record `$USER` (no override in 0.2). Keep the
state directory out of version control, or use `--state-dir`, if that matters.

Do not place credentials or sensitive personal material in candidate claims,
approval justifications, or public repository fixtures.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use the
repository host's private vulnerability reporting, if enabled, and include the
version, the command run, and the smallest reproduction you can share without
real secrets.

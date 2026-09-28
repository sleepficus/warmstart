# Changelog

## 0.2.0 — 2026-09-28

- Product renamed from Coldstart to WarmStart; `warmstart` is the primary
  command.
- New managed markers `<!-- warmstart:start -->` / `<!-- warmstart:end -->`;
  existing Coldstart markers are updated in place; mixed marker families are
  refused before any write.
- Coldstart compatibility kept: `coldstart` command alias, `coldstart.*`
  imports, and the `.coldstart/` state directory.
- Public documentation: honest evidence summary, limitations, matched-trial
  plan, contribution guide, and release checklist.
- First public source release under Apache-2.0 with DCO 1.1 contributor
  sign-off.

## 0.1.0

- Initial Coldstart implementation (not publicly released).

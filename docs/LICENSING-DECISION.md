# Licensing decision

**Decision: Apache License 2.0 with Developer Certificate of Origin 1.1
sign-off.** The project owner accepted the maintainer's recommendation on
2026-09-28.

Apache-2.0 permits use, modification, redistribution, and commercial use with
attribution, and includes an explicit patent grant and patent-retaliation
clause. The DCO provides a lightweight contribution certification without a
separate contributor license agreement.

| Option | Lets others | Main consequence |
|---|---|---|
| No license (current) | Read only | Low adoption; forks and contributions are not clearly permitted |
| MIT | Use, modify, redistribute, including commercially, with attribution | Maximum adoption; no patent grant; closed forks allowed |
| Apache-2.0 | Same as MIT plus an explicit patent grant and patent-retaliation clause | Common for developer tools; slightly longer NOTICE obligations |
| MPL-2.0 | Use and combine freely; changes to WarmStart files must stay open | Middle ground; file-level copyleft |
| GPL-3.0 / AGPL-3.0 | Use and modify; distributed (AGPL: network-served) derivatives must be open under the same license | Strong reciprocity; many companies avoid these |
| Source-available (e.g. PolyForm Noncommercial) | Terms vary; typically non-commercial use | Not open source by the OSI definition; limits adoption |

Contributions require a `Signed-off-by:` line certifying DCO 1.1. No CLA is
required.

## Applied changes

1. Apache-2.0 text is in `LICENSE`.
2. `pyproject.toml` declares `Apache-2.0`, packages `LICENSE`, and includes the
   matching classifier.
3. `NOTICE.md`, `README.md`, and `CONTRIBUTING.md` state the decision.
4. `DCO.md` contains the verbatim DCO 1.1 terms and contribution instructions.

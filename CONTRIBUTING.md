# Contributing

Thanks for your interest. WarmStart is licensed under Apache-2.0. Contributions
are accepted under the same license using the lightweight
[Developer Certificate of Origin](DCO.md) process.

Please sign off every commit with:

```bash
git commit -s
```

The resulting `Signed-off-by:` line certifies the statements in `DCO.md`.
Use your real name and an email address you are comfortable recording in the
public Git history. Open an issue before starting a substantial change so the
direction can be discussed first.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
mypy src
```

All four must pass.

## Principles for changes

- Stay local and network-free in the core workflow. No model calls.
- Never execute code from the inspected repository.
- Keep observation, interpretation, approval, publication, and staleness
  separate. Nothing below `cli` may prompt a human.
- Prefer fewer, evidence-backed, action-changing facts over coverage.
- A bug fix comes with a test that fails without the fix.
- Keep the Coldstart 0.x compatibility promises in [MIGRATION.md](MIGRATION.md)
  unless a versioned migration replaces them.

## Evidence claims

Do not add claims that WarmStart improves agent performance unless they come
from a matched trial as described in the README. Null and negative results are
welcome and should be reported as such.

## Fixtures

Test fixtures must be synthetic. Never commit real session exports, real
credentials, personal information, or private repository content.

# Release checklist

Run from a fresh clone in a new virtual environment.

**Publish from the manifest only.** Create the public repository by copying
exactly the paths in `PUBLIC-FILE-MANIFEST.json` into a new directory and
verifying their SHA-256 hashes. Never `git add .` in a release-prep working
directory; review files, build output, and check logs live beside the public
files there.

1. `python -m pip install -e ".[dev]"`
2. `pytest` — all tests pass.
3. `ruff check .` and `ruff format --check .` — clean.
4. `mypy src` — strict, no issues.
5. `python -m build` (or `pip wheel . --no-deps -w dist`) and list the wheel:
   only `coldstart/`, `warmstart/`, and `*.dist-info/` entries.
6. Install the wheel into another fresh environment; run `warmstart --help`,
   `coldstart --help`, and `python -c "import warmstart, coldstart"`.
7. Run the workflow from the README against an empty temporary repository.
8. Search the tree for local paths, usernames, credentials, and session data.
9. Check the README evidence table still matches reality; no new benefit
   claims without a matched trial.
10. Update `CHANGELOG.md` and the version in `pyproject.toml` and
    `src/coldstart/__init__.py`.
11. Regenerate the public file manifest after the last edit and re-verify it.
12. Confirm Apache-2.0 metadata is present, `LICENSE` is included in both
    distributions, and contribution instructions require DCO sign-off.

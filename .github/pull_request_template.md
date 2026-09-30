## What and why

<!-- The change, and the problem it solves. Link the issue if there is one. -->

## How it was verified

- [ ] `.venv/bin/python -m pytest -q` passes
- [ ] `make lint` is clean (ruff + mypy)
- [ ] A screen changed: `make a11y` passes and `docs/guide.md` says what's new

## If it builds or runs a `gam` command

- [ ] The argv is built in `GAMCommands` as a list; each operator-supplied value is exactly one element
- [ ] A write goes `ChangePreview` → `guard.enforce()` in the apply route → `_run_write`, and a new POST route is in `tests/test_write_routes_guarded.py`
- [ ] Checked against the vendored grammar (`gamgui/resources/gam7/GamCommands.txt`); a mock change fails the way real GAM fails
- [ ] Live status stated below — a write that hasn't run against a real domain stays *not yet* in the README

**Live status:** <!-- "Ran on a throwaway user in a real domain: …" or "Offline only." -->

## Nothing private

- [ ] No credentials, real domain names, real addresses, or captures from a real tenant (screenshots come from `scripts/readme_screenshots.py`)

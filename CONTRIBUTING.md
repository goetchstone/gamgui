# Contributing to GamGUI

Thanks for helping out. GamGUI is a local, open-source macOS GUI for managing Google Workspace via
[GAM7](https://github.com/GAM-team/GAM).

**Read [CLAUDE.md](CLAUDE.md) first.** It is addressed to an AI coding agent, but it is the shortest
route to the invariants this codebase actually enforces — the argv-only chokepoint, why every
mutation goes through the guard and the audit log, why only read-only commands may be auto-promoted
to runnable, and why a mock that is *more permissive* than real GAM is worse than no mock. Each entry
is there because something broke once. If a change of yours contradicts one of them, that is the
conversation to have in the PR.

Looking for something to pick up? [ROADMAP.md](ROADMAP.md) is the ranked backlog, and its
"Deliberate trade-offs" section explains the two things GamGUI does not do today, and why.

## Quick start

You need **Python 3.10+** (`pyproject.toml` sets `requires-python = ">=3.10"`). macOS's own
`/usr/bin/python3` is 3.9 and cannot install this project, so `make setup` auto-picks the newest
`python3.1x` on your PATH; pin it with `make setup PYTHON=python3.13` if you'd rather choose. If
nothing suitable is installed, `make setup` says so and stops instead of building a broken venv.

```bash
git clone <your-fork-url> && cd gamgui
make setup        # creates .venv (Python 3.10+) and installs dev + native-window deps
make gam          # vendors the GAM7 binary into gamgui/resources/gam7 (needs network)
make test         # runs the offline test suite
make run          # launches the app
```

`make help` lists every target.

You do **not** need the GAM binary or any Google credentials to run the tests — the suite drives a
mock `gam` (`tests/fixtures/mock_gam.sh`) and an in-memory Keychain, so it is fully offline and runs
in CI on macOS and Linux.

To look at a screen without a tenant, `.venv/bin/python scripts/preview_mock.py` serves the app at
<http://127.0.0.1:8766/?token=t> on the same mock `gam`, an in-memory Keychain and made-up
`example.com` data (a few users and groups, an onboarding role, a small calendar index); its state
lives in a temp dir that is deleted when it stops. A write there only proves the mock accepted it.

## Project layout

```
gamgui/core/        # engine: GAM runner, parser, command builders, secrets, guard, audit, connectors
gamgui/web/         # FastAPI app + Jinja/HTMX templates (the UI)
gamgui/app.py       # entry point: pywebview window wrapping the local server
gamgui/resources/   # vendored GAM7 binary (fetched, not committed)
tests/              # offline test suite + fixtures (incl. the mock gam)
requirements/       # hash-locked dependencies: *.in (inputs) -> *.txt (make lock)
scripts/            # fetch_gam.sh (vendor GAM7 + grammar), gam_checksums.txt (SHA-256 pins),
                    # build_command_catalog.py (regenerate the browse catalog after a GAM bump),
                    # build_app.sh (PyInstaller .app), acceptance.py (read-only live check),
                    # vendor_assets.sh (vendor the JS/CSS the UI loads),
                    # preview_mock.py (the UI on the mock gam, for looking at a screen)
```

## Conventions

- Target **Python 3.10+**, matching `requires-python` in `pyproject.toml` (`from __future__ import
  annotations`; no 3.11+ runtime-only syntax — CI's floor job is 3.10).
- All `gam` invocations are built in `gamgui/core/gam/commands.py` as **arg lists** (never shell
  strings) — keep it that way; it's the injection-safety boundary, and the arg-shape tests pin it.
- Mutations go through the destructive-op guard and the audit log.
- Add or update tests for any change; keep `pytest` green.

## Coding standards

Write like the surrounding code — a reviewer shouldn't be able to tell which lines were generated.

- **KISS.** The smallest change that does the job. No speculative abstraction, no config knobs nobody
  asked for, no dead code. Delete more than you add when you can.
- **Comments explain *why*, not *what*** — one line above a non-obvious block, skip the obvious. No
  banner art, no restating the code in prose, no "Step 1/Step 2" narration.
- **Names carry the meaning** so comments stay sparse. Match the existing naming and file layout.
- **No AI tells:** no "Note that…"/"It's worth noting", no "robust/seamless/leverage", no emoji in
  code, no comments that hedge or apologize. Terse and factual.
- **Reuse before adding** — look for an existing helper (`guard.evaluate`, `_run_write`,
  `parse_records`, the `BatchJob` runner, the catalog) before writing one.
- **Errors surface, never crash** — map a failure to a friendly message and return an error partial,
  never a 500 or a silent success.

## Adding a Builder command

Every *write* the Builder (`/builder`) can run is curated by hand. Full recipe + safety invariants in
[`docs/builder-commands.md`](docs/builder-commands.md). In short: verify the syntax against the
vendored `GamCommands.txt`; add the arg-list builder to `core/gam/commands.py` (+ an arg-shape test;
the grammar contract checks every builder by itself); add a `CatalogCommand` with typed slots and an
authoritative `RiskLevel` to `core/catalog/catalog.py`; add a web test; give the mock gam a strict
handler for it. Two rules hold everywhere: a slot value is always exactly one argv element (never
shell-spliced, never f-stringed into a token), and only `buildable=True` commands run. Anything
that can write must be hand-curated in `GAMCommands` — the generic grammar-derived builder in
`core/catalog/readbuilder.py`, which does assemble argv from the vendored grammar line, is attached
only to commands confidently classified `RiskLevel.READ_ONLY`.

## Dependencies

`pyproject.toml` holds the flexible ranges, and `make setup` installs from them. What CI runs and
what the `.app` ships are hash-locked instead: `requirements/dev.txt` (the runtime dependencies plus
the test and lint tools) and `requirements/app.txt` (the runtime dependencies plus pywebview and
PyInstaller), each compiled from the `.in` file beside it and installed with
`pip install --require-hashes`. To add, remove or re-range a dependency:

1. edit `pyproject.toml` **and** the matching `requirements/*.in` — `tests/test_locks.py` fails on
   either one alone;
2. run `make lock` (it uses the uv pinned in the dev extra; it keeps every other pin, and
   `make lock ARGS=--upgrade` refreshes them all);
3. commit the `.in` and `.txt` together.

The locks are made with `uv pip compile --universal` rather than pip-tools because one file then
installs on Linux and macOS across Python 3.10–3.14 (`pip-compile` locks for the interpreter it runs
on, and would need a file per CI job). `pip install --require-hashes -r requirements/dev.txt` in a
fresh venv gives you exactly CI's environment. Dependabot keeps both locks current through its `uv`
ecosystem, which regenerates them with the same flags.

## Before opening a PR

```bash
make test
make lint
make cov     # optional: the suite under CI's coverage gate
```

`make lint` runs [ruff](https://docs.astral.sh/ruff/) with the rule set in `pyproject.toml`: checks
that catch bugs (pyflakes, pycodestyle errors, bugbear, blocking calls in `async` code), not style.
Ruff's formatter is not adopted — it would rewrite most files — so match the surrounding layout by
hand. A `# noqa: CODE` carries its reason after it.

It then runs [mypy](https://mypy.readthedocs.io/) over `gamgui/core` and `gamgui/web` (settings in
`pyproject.toml`'s `[tool.mypy]`). The bar is non-strict for now: the bodies of unannotated
functions are checked, but annotations aren't required yet. When mypy can't follow code that is
correct, restructure it so the type is visible (bind the value to a local before narrowing it, as a
lambda doesn't keep an `is None` check) before reaching for a narrow `# type: ignore[code]` with its
reason after it — `warn_unused_ignores` fails one that stops being needed. Only the optional
`abapit` backend is exempt from missing stubs; a new dependency without types fails the check.

CI runs the lint once, and the suite on Ubuntu and macOS across Python 3.10, 3.12 and 3.14, each
installed from `requirements/dev.txt`. The macOS 3.14 run also measures line coverage and fails
below `fail_under` in `pyproject.toml` (90%; the suite covered 90.5% when the gate went in) — `make
cov` runs the same check locally. Raise the floor as tests land; don't lower it to get a PR green.
On top of that there's a macOS `gam-compat` job that vendors the *pinned* GAM7 and runs
`tests/test_command_contract.py` against the real command reference, plus a non-blocking
`gam-latest-preview` job that runs the token contract against the *newest* GAM7 as an early warning
that a command we use was renamed or removed.

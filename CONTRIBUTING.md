# Contributing to GamGUI

Thanks for helping out. GamGUI is a local, open-source macOS GUI for managing Google Workspace via
[GAM7](https://github.com/GAM-team/GAM).

## Quick start

```bash
git clone <your-fork-url> && cd gamgui
make setup        # creates .venv and installs dev + native-window deps
make gam          # vendors the GAM7 binary into gamgui/resources/gam7 (needs network)
make test         # runs the offline test suite
make run          # launches the app
```

`make help` lists every target.

You do **not** need the GAM binary or any Google credentials to run the tests — the suite drives a
mock `gam` (`tests/fixtures/mock_gam.sh`) and an in-memory Keychain, so it is fully offline and runs
in CI on macOS and Linux.

## Project layout

```
gamgui/core/        # engine: GAM runner, parser, command builders, secrets, guard, audit, connectors
gamgui/web/         # FastAPI app + Jinja/HTMX templates (the UI)
gamgui/app.py       # entry point: pywebview window wrapping the local server
gamgui/resources/   # vendored GAM7 binary (fetched, not committed)
tests/              # offline test suite + fixtures (incl. the mock gam)
scripts/            # fetch_gam.sh (vendor GAM7), build_app.sh (PyInstaller .app)
```

## Conventions

- Target **Python 3.9+** (`from __future__ import annotations`; no 3.10+ runtime-only syntax).
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

The Builder (`/builder`) runs only *curated* commands. Full recipe + safety invariants in
[`docs/builder-commands.md`](docs/builder-commands.md). In short: verify the syntax against the
vendored `GamCommands.txt`; add the arg-list builder to `core/gam/commands.py` (+ an arg-shape test
+ a contract token); add a `CatalogCommand` with typed slots and an authoritative `RiskLevel` to
`core/catalog/catalog.py`; add a web test. Never assemble argv from raw grammar tokens, and never let
a browse-only command run.

## Before opening a PR

```bash
make test
```

CI runs the same suite on Ubuntu and macOS across Python 3.9 and 3.12.

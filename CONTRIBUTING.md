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

## Before opening a PR

```bash
make test
```

CI runs the same suite on Ubuntu and macOS across Python 3.9 and 3.12.

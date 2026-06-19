# GamGUI

A local, open-source **macOS GUI for [GAM7](https://github.com/GAM-team/GAM)** — drive the full
power of Google Workspace administration without memorizing CLI commands, with your credentials
kept in the macOS **Keychain**.

> GAM exposes far more of Google Workspace than the Admin Console surfaces (Gmail
> signatures/delegates/forwarding, advanced group settings, bulk operations, reporting). GamGUI
> puts a safe, native front end on top of it.

## Status

Early development (Phase 0 — foundations). Not yet ready for general use.

## Design goals

- **Local & native** — a single bundled `.app`; no server, nothing leaves your machine.
- **Secure** — secrets live in the macOS Keychain; GAM's plaintext credential files are
  materialized into a locked-down temporary directory only for the duration of each `gam`
  invocation, then wiped. ([details](#security-model))
- **Easy but powerful** — form/table UI for the common painful tasks, full GAM power underneath.
- **Connector-ready** — Google Workspace is the first connector; the architecture is built so
  Apple Business Manager, Mosyle MDM, and Sangoma PBXact can plug in later for cross-system
  person lifecycle management.

## Architecture

```
HTMX views → FastAPI routes → Services → Connector protocol → GAMConnector
                                              → GAMRunner (subprocess)
                                              → SecretsVault (Keychain) + EphemeralConfig (temp GAMCFGDIR)
```

Wrapped in a `pywebview` native window (WKWebView). See `docs`/the plan for the full design.

## Security model

GAM stores credentials as plaintext files (`client_secrets.json`, `oauth2.txt`,
`oauth2service.json`) in its config dir. `oauth2service.json` can impersonate **any** user in the
domain and `oauth2.txt` is effectively an admin password, so GamGUI:

1. keeps the canonical copies in the **Keychain** (`keyring`, device-bound, not synced);
2. materializes them into a `chmod 700` temp dir (files `chmod 600`) set as `GAMCFGDIR` only for
   each `gam` call;
3. wipes that dir on completion (success or failure);
4. writes refreshed OAuth tokens back to the Keychain.

## Build from source

Requirements: **Python 3.9+** and **macOS** (to run the native window; the test suite itself runs on
Linux too). No Google credentials are needed to build or test.

```bash
git clone <repo-url> && cd gamgui
make setup     # create .venv, install dev + native-window deps
make gam       # vendor the pinned GAM7 binary into gamgui/resources/gam7 (needs network)
make test      # offline test suite — uses a mock gam, no binary/credentials required
make run       # launch the app (native window; prints a browser URL if pywebview is absent)
```

`make help` lists all targets. Prefer raw commands? `pip install -e ".[dev,desktop]"`, then
`scripts/fetch_gam.sh`, `pytest`, `python -m gamgui.app`. For an exact pinned install instead of the
flexible one, use `pip install -r requirements.txt`.

The GAM7 binary is **not committed** (platform-specific, large) — `make gam` / `scripts/fetch_gam.sh`
fetches the pinned version (`v7.46.01`) from the official releases and records its checksum.

### Build a standalone `.app` (macOS)

```bash
make app       # PyInstaller -> dist/GamGUI.app (bundles Python + the GAM7 binary)
```

For distribution to other Macs you must codesign + notarize the bundle (including the embedded gam
binary); running it yourself needs no signing.

### Tests & CI

`pytest` is fully offline (mock gam + in-memory Keychain). CI runs it on Ubuntu and macOS across
Python 3.9 and 3.12 — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Connectors

Google Workspace (via GAM7) is the built-in connector. Apple Business Manager and Mosyle are
provided by the companion [`abapit`](https://github.com/) project, whose duck-typed clients are
wrapped as async connectors in [`abapit_connector.py`](gamgui/core/connectors/abapit_connector.py).
abapit is an **optional** dependency — install it editable to enable those connectors:

```bash
pip install -e /path/to/abapit
```

Without it, gamgui runs as a Google-Workspace-only tool (the ABM/Mosyle tests skip automatically).

## License

MIT — see [LICENSE](LICENSE).

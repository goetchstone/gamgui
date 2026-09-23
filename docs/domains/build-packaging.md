# Domain: Build & Packaging

**One line:** Vendors the GAM7 binary (checksum-pinned), regenerates the browse catalog, and builds the codesigned macOS `.app` — plus the drift guards that fail CI when any pin/count goes stale.

**Owns invariant(s):** #7 (the vendored GAM pin fails closed — an asset with no committed SHA-256 is refused, `--allow-unpinned` is CI-preview only). Also owns the three drift guards named in CLAUDE.md.
**Enforcement home:** `tests/test_workflow_safety.py` (no expression inside a `run:` script), `tests/test_fetch_gam.py` (the fail-closed gate, hermetic; also `test_ci_only_grants_the_opt_in_to_the_preview_job` and `test_pywebview_is_pinned`), `tests/test_command_contract.py` (`test_required_command_tokens_present`, `test_catalog_matches_grammar`, `test_pinned_version_consistent`), `tests/test_bump_gam.py` (bump edits still match their targets), `tests/test_polish.py` (CLAUDE.md counts/pin/floor stay true — `test_claude_md_command_counts_match_the_real_catalog`, `test_claude_md_states_the_current_gam_pin_and_python_floor`).

## Files
- `Makefile` — `setup` (auto-picks newest `python3.1x` ≥3.10; system 3.9 is refused), `gam`→`fetch_gam.sh`, `test`, `run`, `app`→`build_app.sh`.
- `scripts/fetch_gam.sh` — downloads the GAM7 macOS asset for this arch, verifies SHA-256 against the pin **before extracting**, installs into `gamgui/resources/gam7/`, writes `VERSION` + `SHA256`. `TAG="v7.48.07"`.
- `scripts/gam_checksums.txt` — the committed pin: `<sha256>  <asset-filename>`. One line today.
- `scripts/bump_gam.py` — the whole "Updating GAM" runbook as one command: attest → pin → vendor → bump version strings + mock → regen catalog → refresh CLAUDE.md counts. Opens a PR in CI.
- `scripts/build_command_catalog.py` — parses `GamCommands.txt` via `parse_grammar` → writes `command_catalog.json` stamped with `EXPECTED_GAM_VERSION`.
- `scripts/build_app.sh` — PyInstaller build of `dist/GamGUI.app`; auto-vendors GAM if missing; pins `pyinstaller==6.21.0` + `pywebview==6.2.1`; codesigns with the stable "GamGUI Local" identity.
- `scripts/vendor_assets.sh` — snapshots front-end vendor JS (htmx SRI-verified, Tailwind Play snapshotted) into `web/static/vendor/`, so no remote scripts load.
- `gamgui.spec` — PyInstaller spec (`main.py` entry, bundles `resources/gam7`, pyobjc/webview via `collect_all`).
- `gamgui/resources/gam7/` — the vendored, **gitignored** binary + `GamCommands.txt` grammar + generated `command_catalog.json`.

## How it works
`make gam` runs `fetch_gam.sh`, which queries the GitHub release JSON, picks the highest-`macosNN` `.tar.xz` for `uname -m`, hashes it, and looks up `<asset-name>` in `gam_checksums.txt`. Match → install; no pin → **exit 1** (fail closed); mismatch → exit 1 always (even with `--allow-unpinned`). After a fetch the catalog is stale, so `build_command_catalog.py` must re-run. `bump_gam.py` chains all of this: it downloads, runs `gh attestation verify` (the trust anchor that lets it write the pin automatically without becoming trust-on-first-use), writes the pin, re-vendors, and rewrites `EXPECTED_GAM_VERSION` (commands.py), `TAG` (fetch_gam.sh), the mock's `gam version`, and the CLAUDE.md counts via regex `_sub` (refusing to guess if a pattern no longer matches). At runtime `runner.py` resolves `gam` via env override → `sys._MEIPASS/resources/gam7/gam` (bundled `.app`) → repo source tree.

## Invariants & the failure history
- **#7 fail-closed pin.** The asset name comes from the release JSON the script just downloaded, so "no pin for this name" is exactly what renaming an asset produces — treating it as trust-on-first-use would make the pin decorative, and the bundled `gam` can impersonate any domain user via DWD. Verified *before* extraction. `--allow-unpinned` exists only for the `gam-latest-preview` CI job (`test_ci_only_grants_the_opt_in_to_the_preview_job` asserts the pinned `gam-compat` job never gets it).
- **Three drift guards.** `test_pinned_version_consistent` (fetch TAG + mock all == `EXPECTED_GAM_VERSION`), `test_catalog_matches_grammar` (catalog version + command count == a fresh `parse_grammar`), `test_required_command_tokens_present` (every token our builders use still exists in `GamCommands.txt`; skips on a clean clone, runs in `gam-compat` CI after a real fetch). Bump step 1 (`fetch_gam.sh` with a new TAG but old pin) **fails by design**.
- **CLAUDE.md counts are load-bearing.** Two agent-facing docs once claimed only the 26 curated commands run (538 do) — a reviewer agent then flagged correct `readbuilder` code. `test_polish.py::test_claude_md_command_counts_match_the_real_catalog` turns the claim into something CI fails on.
- **No `${{ }}` inside a workflow `run:` script.** Actions substitutes expressions into the script text before the shell parses it, so `gam-watch.yml` once ran `bump_gam.py "v${{ steps.check.outputs.latest }}"` — an upstream release tag became shell code in a job holding `contents: write`. Values reach scripts through `env:`, and the check step admits only a tag matching `^[0-9]+(\.[0-9]+){2,3}$` (bash `=~`, so a newline can't forge a second `$GITHUB_OUTPUT` line). `tests/test_workflow_safety.py` scans every workflow's `run:` blocks.
- **pywebview/pyinstaller pinned.** Both go inside the shipped bundle (`build_app.sh`); an unpinned floor would ship an unreviewed WKWebView host. `test_pywebview_is_pinned` (in `test_fetch_gam.py`) guards the **pywebview** pin — in both `pyproject.toml` and `build_app.sh`; the pyinstaller pin lives only in `build_app.sh` and is not separately test-asserted.

## Gotchas / mock-lies traps
- **The catalog and drift guards are the ONLY GAM check that runs offline.** `mock_gam.sh` proves nothing about command *syntax* — it only echoes canned output. The real syntax source of truth is the vendored grammar `gamgui/resources/gam7/GamCommands.txt`; `test_required_command_tokens_present` is a substring check against it, not a live run.
- **A green `pytest` never proves a GAM write works** (CLAUDE.md invariant #2 / "the mock lies"). After a bump: skim `GamUpdate.txt` for breaking changes and run `scripts/acceptance.py` (read-only) against a real tenant — `bump_gam.py` deliberately omits both because they need judgment, not mechanism.
- **`make gam` wipes `resources/gam7/` (`rm -rf $DEST`)**, including the generated `command_catalog.json` — always regenerate it, or `test_catalog_matches_grammar` goes red on a count mismatch.
- **macOS-only vendoring.** `fetch_gam.sh` uses BSD `find -perm +111`; `test_fetch_gam.py` only asserts a completed install on Darwin (it still checks the gate on Linux). System `/usr/bin/python3` (3.9) cannot install the venv.
- **Bump `_write_pin` keeps only non-`gam-` lines + the new pin** — it replaces the single active GAM pin rather than appending, so `gam_checksums.txt` holds one asset line at a time.

## Testing / live-verification status
- `tests/test_fetch_gam.py` — hermetic: copies the script into a throwaway root, drives it with a stub `curl` on PATH, real `.tar.xz`; never touches api.github.com or the real `resources/gam7`. Covers refuse-unpinned, opt-in install, matching pin, mismatch-always-refuses, unknown-flag, and static branch/CI assertions.
- `tests/test_bump_gam.py` — unit-tests the pure text transforms (`select_asset`, the regex rewrites) so a future rename fails here instead of no-op'ing in CI. Network/subprocess orchestration is not covered.
- `tests/test_command_contract.py` + `tests/test_polish.py` — the drift/consistency guards above. All run in the offline `pytest` suite; the token/catalog tests additionally re-run in the `gam-compat` CI job against a freshly fetched real binary.
- **Still needs a real tenant:** attestation verify (`bump_gam.py`), the `.app` build + codesigning (`build_app.sh` — no automated test), and any GAM write. The `.app` and a live output-shape pass are unproven by the offline suite.

## To do common tasks here
- **Bump GAM:** `python scripts/bump_gam.py vX.Y.Z` → `.venv/bin/python -m pytest -q` → skim `GamUpdate.txt` → `scripts/acceptance.py` on a tenant. (Manual path: edit pin in `gam_checksums.txt`, `make gam TAG=vX.Y.Z`, run `build_command_catalog.py`, bump `EXPECTED_GAM_VERSION`/`TAG`/mock/CLAUDE.md counts.)
- **Add a new builder command's contract token:** add its GAM token to `REQUIRED_TOKENS` in `tests/test_command_contract.py` alongside the new `GAMCommands` builder (invariant #1).
- **Change the bundled `.app` (deps, plist, signing):** edit `gamgui.spec` (datas/hiddenimports/plist) and/or `scripts/build_app.sh` (pins, codesign), then `make app`.
- **Bump a front-end vendor lib:** edit `HTMX_VER`/`HTMX_SRI` (or the Tailwind snapshot) in `scripts/vendor_assets.sh`, re-run it, update the `<script>` tags in `web/templates/base.html`.

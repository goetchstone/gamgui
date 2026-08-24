# Working on GamGUI

A **local, single-operator, macOS-native GUI for GAM7** (the Google Workspace admin CLI). FastAPI on
`127.0.0.1` (random port) behind a per-launch token and an Origin check, displayed in a pywebview /
WKWebView window. The same app also opens in a normal browser. Public repo, MIT.

The credentials this app reaches are the reason it is careful: **`oauth2service.json` can impersonate
any user in the domain, and `oauth2.txt` is effectively an admin password.**

## Invariants — do not regress these

Each of these exists because something went wrong. Several have tests guarding them; a change that
"simplifies" one of these is a bug, not a cleanup.

1. **argv-only, never a shell.** Every GAM invocation is an explicit argv list built by a
   `GAMCommands` static method and run with `create_subprocess_exec`. An operator-supplied value is
   always exactly one list element, never interpolated into a string.
2. **Every mutation goes through the chokepoint**: `GAMCommands` builder → `ChangePreview` →
   `guard.evaluate()` → connector `_run_write(...)`, which is serialized and appended to the audit
   log. There is no second write path. Don't add one.
3. **Only read-only commands may become runnable automatically.** Of 1075 catalog entries, 538 run:
   26 hand-curated (the only ones that can *change* anything) plus 512 grammar-derived commands
   auto-promoted by `core/catalog/readbuilder.py` **because they are confidently `READ_ONLY` and not
   flagged uncertain**. Everything else is inert, syntax display only. Adding write coverage is a
   deliberate act of curation. (Two agent-facing docs once claimed only the 26 curated commands run —
   that was wrong, and it made a reviewer flag correct code.)
4. **Secrets live in the Keychain.** They are materialized into a `0700` dir (`0600` files) for one
   `gam` call and wiped — including via an `atexit` hook and an owner-PID marker, because "quit
   mid-call" was how plaintext got stranded. Never persist them anywhere else.
5. **The credentials-import bound is inode-based on purpose** (`core/setup.py`). Comparing resolved
   *paths* wrongly rejected an accented (NFD) home and the `/System/Volumes/Data` firmlink spelling
   of home; `(st_dev, st_ino)` collapses case, Unicode normalization and firmlinks correctly. The
   directory is pinned to a descriptor and each credential file is opened `O_NOFOLLOW` relative to it
   — that is what closes a winnable read/wipe race. Don't rewrite it as a string comparison.
6. **The loopback server rejects cross-origin callers.** Cookies are not port-scoped, so the token
   cookie alone would let any page on another `127.0.0.1` port drive mutating POSTs.
7. **The vendored GAM pin fails closed.** An asset with no committed SHA-256 in
   `scripts/gam_checksums.txt` is refused, not installed. `--allow-unpinned` exists for CI's
   throwaway preview job only.
8. **Never `| tojson` inside a double-quoted HTML attribute.** Jinja's `tojson` does not escape `"`.
   Directory data from Google goes in an autoescaped `data-*` attribute and JS reads `el.dataset.*`.
9. **Bound anything polled.** Live progress feeds keep a fixed rolling window (~12 rows) and cap
   retained failure lists, so a domain-wide run doesn't make each 1s poll grow with the user count.

## The recurring failure mode: the mock lies

The bug class this project keeps hitting is **"the mock passed, the live tenant broke."** It has
happened repeatedly — a command that rejects `formatjson`, a mock that returned group members for
*any* address so a user looked like a group.

So: when you touch `tests/fixtures/mock_gam.sh`, make it **fail the way real GAM fails**. A mock that
is more permissive than GAM is worse than no mock, because it converts a live break into a green
test. Check syntax against the vendored grammar (`gamgui/resources/gam7/GamCommands.txt`) — it is the
source of truth, not your memory of GAM.

Corollary: **passing tests do not mean a GAM write works.** Anything that mutates a real domain is
unproven until it has run against one — see the live-verification status in the README.

## Rules of engagement

- **Never run a mutation against a real tenant** without explicit, per-action permission from the
  operator. Read-only checks are fine (`scripts/acceptance.py` is the read-only pass).
- Destructive/complex GAM commands must be verified live on a **throwaway** user/event/calendar
  before being relied on.
- **macOS-only** is deliberate: don't build or pitch Windows/Linux support. But don't *foreclose* it
  either — keep platform specifics in the shell (window, Keychain, codesigning, `.app`), not in
  `core/`. Same framing for a free-text `gam` runner: not planned, not forbidden. See ROADMAP.md.

## Working here economically

This file loads every session (~1k tokens). Subagent fan-out has cost **millions** in a single
session — one security audit ran 31 agents for 2.15M tokens and ended a monthly budget. So the
efficiency that matters is not shorter instructions, it is fewer and better-aimed agents:

- **Point subagents at this file** instead of restating the invariants in every prompt. One session
  re-typed them into nine workflow prompts, which is both wasteful and a drift risk.
- **Scale fan-out to the stakes.** Adversarial verification (one agent tries to *break* another's
  work, proving it by running code) genuinely earns its cost on security properties, path handling
  and anything touching credentials — it caught a symlink read of `/etc/passwd`, a wipe that deleted
  an un-imported file, and a regression that silently skipped the plaintext wipe. For ordinary
  feature work, one agent or none is as good and far cheaper.
- **Put a cheap deterministic filter before an expensive one.** Let a script decide whether there is
  work before a model is invoked.
- **Long procedures belong in `.claude/skills/`, not here** — a skill costs nothing until it is
  needed, while everything in this file is paid for on every turn.
- **Summarize large tool output, don't re-read it.** Parse a 100 KB workflow result down to the few
  fields that matter rather than pulling it into context whole.

## Layout

```
core/gam/         commands.py (ALL argv construction) · runner.py (the only subprocess) · errors, parser, models
core/secrets/     vault.py (Keychain) · ephemeral.py (temp GAMCFGDIR + wipe)
core/catalog/     catalog.py (curated overlay) · readbuilder.py (auto-promoted reads) · parser.py
core/            guard.py · audit.py · setup.py · lifecycle.py · onboarding.py · signatures.py · reports.py
core/connectors/  gam_connector.py (every _run_write lives here)
web/routes/       one module per screen · web/jobs.py (polled BatchJob) · web/templates/
```

## Commands

```bash
make setup                          # venv (needs Python 3.10+; macOS /usr/bin/python3 is 3.9)
.venv/bin/python -m pytest -q       # full offline suite: mock gam + in-memory Keychain
make gam TAG=vX.Y.Z                 # re-vendor GAM (fails closed on an unpinned asset — see README)
make app                            # build dist/GamGUI.app
```

GAM is pinned at `EXPECTED_GAM_VERSION` in `core/gam/commands.py` (currently 7.48.00); three drift
guards (`test_command_contract`, `test_catalog_matches_grammar`, `test_pinned_version_consistent`)
fail if a bump breaks a command we use. The bump runbook is in the README — step 1 **fails by
design**.

## Also worth reading

[SECURITY.md](SECURITY.md) (threat model + what is in/out of scope) ·
[ROADMAP.md](ROADMAP.md) (ranked backlog and the deliberate trade-offs) ·
[CONTRIBUTING.md](CONTRIBUTING.md) · [docs/builder-commands.md](docs/builder-commands.md).

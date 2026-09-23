---
name: pre-commit
description: The checklist to apply before a `git commit` in GamGUI — catches the failure classes this repo keeps hitting (the mock lying, a mutation slipping the chokepoint, a secret reaching the log, a stale catalog). The pre-commit hook injects the items relevant to your changed files automatically; read this file for the full list.
---

# Pre-commit checklist

Not every item every time — act on the ones your diff touches. Each failure
class below has bitten this repo before. See
[FRAMEWORK.md](../../../docs/FRAMEWORK.md) for how this skill (soft) relates to
the hook (hard) and the drift-guard/mock-lies tests (tripwire).

## Always

1. **The offline suite is green.** `.venv/bin/python -m pytest -q`. A red or
   unrun suite is not committable. (`make setup` first if there's no `.venv`.)
2. **Read before you wrote.** You opened the file/function you changed rather
   than editing from memory. Verify claims against the code, not against a doc
   or a recollection.
3. **Docs follow code in the same commit.** Touched a domain? Update its
   [docs/domains](../../../docs/domains) runbook in this commit. Fixed a
   regression? Add a [failure-log](../../../docs/failure-log.md) entry — the
   pre-commit hook hard-blocks a `fix:`/`fix(` commit that skipped it (bypass
   with `no-failure-log:` in the body for a non-regression fix). See
   [post-failure](../post-failure/SKILL.md).

## If your diff touches GAM

4. **argv-only (invariant 1).** Every `gam` call is a `GAMCommands` static
   builder returning a list; every operator-supplied value is exactly one list
   element, never interpolated into a string.
5. **Every mutation goes through the chokepoint (invariant 2).** New writes go
   `GAMCommands` → `ChangePreview` → `guard.evaluate()` → `_run_write(...)` in
   `gam_connector.py`. There is no second write path. A secret in the argv
   (a password) is redacted before audit via `_run_write(audit_argv=...)`.
6. **The mock lies.** If you touched `tests/fixtures/mock_gam.sh`, it must
   **fail the way real GAM fails** — a mock more permissive than GAM turns a
   live break into a green test. Check every command/flag against the vendored
   grammar `gamgui/resources/gam7/GamCommands.txt`, not memory. Passing tests
   do **not** mean a GAM write works — say so if it's unproven on a tenant.
7. **Keep the drift guards true.** New builder → the grammar contract in
   `tests/test_command_contract.py` covers it; a validated argument goes in `ENUM_ARGS`. Bumped GAM → regen
   `command_catalog.json` (`scripts/build_command_catalog.py`) and keep the
   catalog counts in CLAUDE.md accurate. Only confidently READ_ONLY commands
   may auto-promote to runnable (invariant 3) — adding write coverage is
   deliberate curation.

## If your diff touches secrets, the web layer, or anything polled

8. **Secrets stay in the Keychain (invariant 4).** Nothing plaintext persisted
   anywhere but the ephemeral `0700`/`0600` GAMCFGDIR that gets wiped. The
   credentials-import bound stays inode-based (invariant 5) — not a string
   compare.
9. **No `| tojson` in a double-quoted HTML attribute (invariant 8).** Google
   directory data goes in an autoescaped `data-*` attribute read via
   `el.dataset.*`. The loopback server still rejects cross-origin (invariant 6).
10. **Bound anything polled (invariant 9).** Live progress feeds keep a fixed
    rolling window and cap retained failure lists.

## Scope

11. **macOS-only is deliberate** — keep platform specifics (window, Keychain,
    codesigning, `.app`) in the shell, not in `core/`. Don't pitch/build
    Windows/Linux. Never run a real-tenant mutation without explicit
    per-action permission; verify destructive commands on a throwaway first.

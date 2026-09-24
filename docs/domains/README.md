# Domain runbooks

These are **load-on-demand runbooks**, one per area of GamGUI. They are deliberately *not* loaded
every session (unlike `CLAUDE.md`) — pull up only the one(s) for the area you're about to touch, read
them before you start, and skip the rest. Each runbook covers the same shape: what the domain is, the
files it owns, how it works, the invariants and the failure history behind them, the "the mock lies"
traps, the live-verification status, and how to do the common tasks. The invariant numbers (#1–#9)
are the ones in `CLAUDE.md`; the "Invariant(s) owned" column below says which runbook is the
authority for each.

## Runbooks

| Domain | One-line | Key files | Invariant(s) owned |
|---|---|---|---|
| [gam-commands](gam-commands.md) | Every `gam` invocation is built as an argv **list** by a `GAMCommands` static method — no shell, one list element per operator value | `core/gam/commands.py` | #1 (argv-only); `EXPECTED_GAM_VERSION` pin |
| [gam-runner](gam-runner.md) | The single subprocess boundary: spawns `gam`, materializes/tears down the ephemeral `GAMCFGDIR`, classifies errors, parses stdout into models | `core/gam/runner.py`, `errors.py`, `parser.py`, `models.py` | #1 (the only `create_subprocess_exec`); `serialize=True` write-lock |
| [secrets](secrets.md) | Stores the three GAM credentials in the Keychain; materializes them into a `0700`/`0600` `GAMCFGDIR` for one call, then wipes (atexit + PID marker + sweep) | `core/secrets/vault.py`, `ephemeral.py` | #4 (Keychain-only, ephemeral wiped) |
| [setup-credentials](setup-credentials.md) | The guided setup wizard: inode-bounded, race-safe import of `oauth2service.json`/`oauth2.txt`/`client_secrets.json`; DWD verify | `core/setup.py`, `web/routes/setup.py` | #5 (inode-based import bound); exercises #4 |
| [connectors-chokepoint](connectors-chokepoint.md) | `GAMConnector` builds argv and funnels **every mutation** through the single `_run_write` (serialize + redact + audit) | `core/connectors/gam_connector.py`, `base.py` | #2 (the one write path); leans on #1, #4 |
| [guard-audit](guard-audit.md) | The confirmation gate (`guard.evaluate`) and the append-only redacted JSONL audit log (rotation, generation-spanning readers) | `core/guard.py`, `core/audit.py` | #2 (guard is the mandatory middle); #4 (redaction); retention bound (#9-class) |
| [catalog-builder](catalog-builder.md) | Turns the vendored grammar into a browsable catalog and decides which commands become **runnable** (26 curated + over 500 auto-promoted reads) — powers `/builder` | `core/catalog/*.py`, `web/routes/builder.py` | #3 (only confident reads auto-promote); edges of #1, #8 |
| [onboarding](onboarding.md) | `/onboard`: create the account (one-time password on a printable sheet, or emailed via `notify`), apply OU + signature, add groups + calendars, push a Tasks checklist, send welcome mail — one hire or a CSV | `core/onboarding.py`, `web/routes/onboarding.py` | Temp-password redaction (a specialization of #2); touches #1 |
| [lifecycle-offboarding](lifecycle-offboarding.md) | The ordered "user is leaving" routine (reset/delegate/autoreply/transfer/calendar-sweep/reminder) + the separate gated account **delete** | `core/lifecycle.py`, `web/routes/lifecycle.py`, `web/routes/users.py` | Delete gated on transfer completion; best-effort calendar-ACL sweep (via #1, #2) |
| [signatures](signatures.md) | Design one HTML signature template with `{vars}`/`[[optional]]`, scope it, preview as a real user, bulk-apply with a live ✓/✗ feed | `core/signatures.py`, `web/routes/signatures.py` | #9 (bound the poll — rolling feed + capped failed list); carries #2, #8 |
| [reports](reports.md) | Read-only directory-insight buckets (2SV, inactive, admins, completeness…) plus a lazy per-user storage/mail usage report | `core/reports.py`, `web/routes/reports.py` | None directly (read-only); leans on #1, #3, #9 |
| [web-screens-jobs](web-screens-jobs.md) | The FastAPI/HTMX screens (Users, Groups, Calendars) and the in-memory job registry (one bounded `Job` base) for polled long-loop progress | `web/jobs.py`, `core/bulk.py`, `web/routes/users.py`, `calendars.py`, `groups.py` | #9 (bound anything polled) |
| [web-server-security](web-server-security.md) | The loopback app factory and the one middleware that authenticates every request (per-launch token, cross-origin reject, headers) | `web/server.py`, `app.py`, `web/templates/base.html` | #6 (loopback rejects cross-origin); #8 (no `\| tojson` in a double-quoted attr) |
| [build-packaging](build-packaging.md) | Vendors the checksum-pinned GAM binary, regenerates the catalog, builds the codesigned `.app`, and the drift guards that fail CI on a stale pin/count | `Makefile`, `scripts/fetch_gam.sh`, `scripts/bump_gam.py`, `scripts/build_app.sh`, `scripts/check_app.py` | #7 (the vendored pin fails closed); the three drift guards |

## By task — which runbook(s) to read first

- **Add a GAM command / a buildable Builder command** → [gam-commands](gam-commands.md) (add the argv
  builder), [connectors-chokepoint](connectors-chokepoint.md) (route a mutation through `_run_write`),
  [catalog-builder](catalog-builder.md) (surface it in `/builder`). Add a new output shape? also
  [gam-runner](gam-runner.md). Skill: `add-builder-command`.
- **Add a guarded mutation** → [connectors-chokepoint](connectors-chokepoint.md) (the `_run_write`
  path + `RiskLevel`), [guard-audit](guard-audit.md) (confirmation policy + redacting any secret it
  carries), [gam-commands](gam-commands.md) (the argv builder).
- **Anything touching secrets / credentials** → [secrets](secrets.md) (Keychain + ephemeral wipe) and
  [setup-credentials](setup-credentials.md) (the inode-bounded import). A new secret-bearing flag also
  needs [guard-audit](guard-audit.md) / [connectors-chokepoint](connectors-chokepoint.md) for
  redaction.
- **Add or change a web screen** → [web-screens-jobs](web-screens-jobs.md) (routes, partials, polled
  jobs) and [web-server-security](web-server-security.md) (the request gate + the `data-*`/`dataset`
  rule for rendering Google-supplied data). Feature screens have their own runbooks:
  [onboarding](onboarding.md), [lifecycle-offboarding](lifecycle-offboarding.md),
  [signatures](signatures.md), [reports](reports.md).
- **Bump the GAM version** → [build-packaging](build-packaging.md) (the whole runbook; `bump_gam.py`,
  step 1 fails by design), then [gam-commands](gam-commands.md) (`EXPECTED_GAM_VERSION`) and
  [catalog-builder](catalog-builder.md) (regenerate `command_catalog.json`; keep the counts in CLAUDE.md,
  README and ROADMAP true).
- **Handle a new GAM error or output shape** → [gam-runner](gam-runner.md) (error taxonomy + parser +
  tolerant models).

**Before trusting any mutation:** passing the offline suite does not prove a GAM write works — the
mock lies. Check syntax against `gamgui/resources/gam7/GamCommands.txt` and verify destructive/complex
commands live on a **throwaway** account per the CLAUDE.md rules of engagement.

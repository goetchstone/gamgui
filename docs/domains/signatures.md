# Domain: Signatures

**One line:** Design one HTML Gmail-signature template with `{variables}` and `[[optional]]` blocks, scope it (user / company / OU / department / location / group), preview it rendered as a real person, and bulk-apply it with a live per-user ✓/✗ feed.

**Owns invariant(s):** #9 (bound anything polled — the apply job's rolling feed + capped failed list, kept by the shared `web/jobs.py` `Job`). Also carries #2 (the apply goes through the connector `_run_write` chokepoint) and #8 (saved-template bodies ride an autoescaped `data-*` attribute, never `| tojson` in the attribute).
**Enforcement home:** `tests/test_signatures.py` (render/scope), `tests/test_sig_templates.py` (store, web flow, smart-quote + linear-scan ReDoS guards), `tests/test_users_web.py` `test_signatures_*` and `test_job_record_tracks_tallies_and_feed` (the apply flow: default scope, the test user is the connected admin or a blank choice, typed count, feed bounds; `test_signatures_apply_runs_only_what_was_previewed` — a widened scope, another person or an edited template after Preview writes nothing — and `test_signatures_apply_is_single_use`), `tests/test_write_routes_guarded.py` (no write on a bare POST). No drift guard specific to this domain.

## Files
- `gamgui/core/signatures.py` — render engine (`render_signature`, `_expand_optional`), `match_scope`, `scope_options`, `smart_quote_warning`/`_curly_quote_in_tag`, and `SignatureStore` (JSON persistence + 3 seeds).
- `gamgui/web/routes/signatures.py` — routes: `page`, `preview`, `retry` (the preview for a finished apply's failed people, plan U5 — see web-screens-jobs), `apply` (spawns bg task; with `retry=<job id>` it takes that retry's held preview), `apply_status` (poll target), `list_templates`/`save_template`/`delete_template`; the apply's progress is a plain `web/jobs.py` `Job`.
- `gamgui/core/gam/commands.py` — `GAMCommands.set_signature` (argv `["user", email, "signature", <body>, "html"]`) and `show_signature` (`["user", email, "show", "signature"]`).
- `gamgui/core/connectors/gam_connector.py` — `set_signature` (→ `_run_write`, `RiskLevel.LOW`), `get_signature`, and `_parse_signature` (scrapes `Signature:` from text output).
- `gamgui/web/routes/users.py` — `/users/signature` (single-user set) and `/users/signature/current` reuse the same connector + `smart_quote_warning`. A failed set shows `ChangeResult.remediation` as the message and GAM's raw `detail` in `_action_result.html`'s collapsed "GAM's error" disclosure.
- `gamgui/web/templates/` — `signatures.html` (page; each scope's "Which" choices as Jinja-escaped `<option>`s in a `<template data-scope>`), `_sig_preview.html`, `_sig_apply.html`, `_sig_templates.html`, `_sig_current.html`; `gamgui/web/static/signatures.js` fills "Which" from those templates and runs a template's Load (`data-action="sig-load"`). No inline script — the CSP forbids it (web-server-security).

## How it works
`page` loads active users + groups and hands scope dropdowns to `signatures.html`; the scope opens on "Specific user (test)", and `preview`/`apply` default `scope_type` to `user` too, so a POST that names no scope matches nobody. Its "Which" list opens on the connected admin's own address (`_test_user`: `SecretsVault.oauth_admin_email`, when that is an active user) and otherwise on a blank "Choose a user…", whose Preview matches nobody. It once preselected whoever sorted first in the directory — a colleague, whose live signature Preview → Apply then overwrote (review F18, failure-log 2026-09-23). `preview` resolves the scope via `_matched` (group scope needs a live `list_group_members`; everything else is in-memory `match_scope`, where only `company` means everyone — any other scope with no value matches nobody; an empty Department or Group once resolved to the whole company, failure-log 2026-09-24), renders the template for `matched[0]`, and returns it inside a **sandboxed `<iframe srcdoc>`**; it also holds the template and the resolved people under a single-use token (`AppState.previews`, flow `signatures`) that the Apply button posts back. **`apply` writes exactly what was previewed:** it takes the held template and people — a missing, used or expired token, or a live form (`hx-include="#sig-form"`) whose scope, which-value or template differs from the previewed one, is refused with "preview again" and writes nothing (Apply once re-resolved the live form, so previewing one user and switching the scope to Whole company overwrote everyone under the stale "Apply to 1 user" button, failure-log 2026-09-23). It never re-resolves the scope: someone who joined it since the preview is not written. It then refuses without `confirmed=1` (`guard.enforce(..., confirm_step=True)`; a bare POST once overwrote every signature in the company, failure-log 2026-09-23) and, above `guard.COUNT_CONFIRM_ABOVE` (25) people, without `confirm_count` equal to the *previewed* count (`typed_count_above=`; the preview renders that input and keeps Apply disabled until it matches), registers a `Job`, and fires `_run_apply` as a detached `asyncio.create_task` (the job keeps a strong ref so it isn't GC'd); the browser then polls `apply/status` every 1s. `render_signature` first drops `[[…]]` blocks whose referenced variables are empty for that user, then substitutes every variable in one regex pass — pure Python, so the preview is byte-identical to what each mailbox gets.

## Invariants & the failure history
- **#9 — bound the poll.** `Job.record` (`web/jobs.py`, shared by every bulk job) keeps only `RECENT_WINDOW = 12` recent results and caps the failed sample at `FAILED_SAMPLE_CAP = 200` (`failed_total` still counts all). Each failure is an `Outcome` carrying *why*: `reason` (the connector's `ChangeResult.remediation`, a fixed short text per `GAMErrorKind`) and `detail` (GAM's raw error), each cut to `DETAIL_CAP = 300` chars — on a usage error GAM echoes the argv, i.e. the whole signature body. The live feed shows `✗ email — reason`; the final summary lists each failure with GAM's error in a `<details>`. Without this, each 1s poll of a domain-wide run would grow with the user count. `register_job` also drops old finished jobs so the registry is bounded.
- **The loop stops at an account-wide failure.** After each write `_run_apply` asks `core/bulk.py` `stop_reason(result.kind, …)`: a failure every later mailbox would share (sign-in expired, GAM not set up, a scope not granted) ends the run with "Stopped: <remediation> The remaining N were not attempted.", and the final panel lists the failure that stopped it with GAM's error. It once kept going through the whole company (failure-log 2026-09-23; `test_signatures_apply_stops_when_the_sign_in_has_expired`, `tests/test_bulk_stop.py`). The operator can halt it too: the running panel's (and the jobs tray's) **Stop** sets `job.cancel_requested`, which `_run_apply` checks before each mailbox (`stop_requested`) — the signature being set finishes, and the panel says "Stopped by you — N not attempted." (plan U5, `tests/test_jobs_tray.py`).
- **Serial loop is deliberate.** `_run_apply` sets signatures one mailbox at a time *because* the per-user ✓/✗ feed is the point (see MEMORY `gamgui-scale`). Don't "optimize" it into a GAM-native bulk call — that kills the live feed. GAM-native bulk is deferred until asked.
- **#8 — no `tojson` in a double-quoted attribute.** Saved bodies go out as `data-body="{{ t.body }}"` (Jinja attribute-escaped) and JS reads `btn.dataset.body`, never `innerHTML`. `hx-vals='{"name": {{ t.name | tojson }}}'` is fine — it's inside single quotes. The ReDoS commit (`648763f`) rewrote `_expand_optional` and `_curly_quote_in_tag` from lazy regexes to linear `find` walks (regex backtracked quadratically on unclosed brackets, and render runs once per mailbox).
- **Smart-quote trap** (`bc36d52`): curly quotes pasted from Word/Mail *inside a tag* silently swallow `{variables}`; `smart_quote_warning` flags only quotes between `<` and the next `>`, never in visible text.

## Gotchas / mock-lies traps
- **`show signature` rejects `formatjson`** — it returns indented text, parsed by `_parse_signature`, not JSON (MEMORY `gamgui-gam-formatjson`; grammar `<SignatureContent>` at line 609). The mock returns fixed text, so a formatjson regression here would pass tests and break live.
- **The mock checks the write's shape, not its effect.** `mock_gam.sh`'s `signature` handler accepts only the grammar's form (`user <e> signature <String> [html [<Boolean>]] …`) and fails a missing value or an unknown keyword like GAM's usage error. It cannot prove Gmail accepts the body: signature length limits and HTML acceptance are unverified by tests.
- `render_signature` substitutes a fixed dict in **one pass** (2026-09-30): variable-by-variable `str.replace` expanded a value that itself held a variable — a first name typed as `{phone}` came out as the phone number (`test_render_signature_inserts_directory_values_verbatim`). It does no HTML escaping of directory values (a user whose title contains `<` lands raw in the signature) — acceptable because it's the admin's own directory, but worth knowing.
- **A store file that can't be read is moved aside, never saved over.** `SignatureStore` starts from the seed only when there is no file. A file the OS won't read, that isn't JSON, or isn't a store goes to `signatures.json.unreadable-<time>` (`set_aside`), and the screen says so. It used to load the seed on any error, so the next save wrote the starters over the operator's templates (failure-log 2026-11-18). Same pattern as `offboard_templates.py` and `roster_maps.py`.
- **A group scope whose members can't be read is unknown, never 0 users.** `_matched` lets the lookup's failure through and the preview says who it applies to isn't known (failure-log 2026-11-18).

## Testing / live-verification status
Offline suite only: `.venv/bin/python -m pytest -q tests/test_signatures.py tests/test_sig_templates.py`. Covers render/optional-block/scope semantics, the store (seed/round-trip/an unreadable file set aside/`0600`/validation), the templates web flow, smart-quote detection, and the two linear-time (ReDoS) bounds. **`set signature` IS confirmed live** per the README's live-verification table (derived from real audit logs). The *bulk apply loop itself* — many mailboxes, failures mid-run, the ✓/✗ feed under load — has no live-tenant proof; test it on a throwaway OU before a domain-wide run.

## To do common tasks here
- **Add a template variable:** add it to `VARIABLES` and to the `values` dict in `render_signature` (both in `core/signatures.py`); add a case to `match_scope`/`scope_options` only if it's also scopable (see the Location commit `4a2c274`). Add a render test to `test_signatures.py`.
- **Add a scope type:** extend `match_scope` (in-memory) or `_matched` in the route (if it needs a live GAM lookup, like `group`); surface it in `scope_options` + the `signatures.html` dropdown JS.
- **Change the live feed / bounds:** `_sig_apply.html` and the shared `failures` macro (`_job_failures.html`); the bounds themselves live in `web/jobs.py` (`Job.record`, `RECENT_WINDOW`, `FAILED_SAMPLE_CAP`, `DETAIL_CAP`) and change every bulk job at once. Keep it bounded (#9) — in rows *and* in the text kept per row.
- **Change how the body is sent to GAM:** `GAMCommands.set_signature` only (argv-only, #1); never build a shell string.

## Backups before a run overwrites (roadmap 4.1)
Gmail keeps no copy of a replaced signature, so a Signatures run (`/signatures/apply`, one person or many) keeps each
person's first:
- **The exact read:** `GAMConnector.signature_exact` runs `user X print signature` (`GAMCommands.print_signature`). GAM's
  print mode writes the stored value verbatim (`printShowSignature`: only `compact` strips line breaks; read from the
  vendored build), where `show signature` is the display form `_parse_signature` strips line by line. A read GAM skipped
  without a word (the titles alone, exit 0, no "Getting Signature for" line, which print mode writes before each person),
  without the person's row, or with a cell missing from that row (cut-off output) is `Incomplete`, never "" (invariant
  10). GAM's titles hold no `signature` column: it comes from Gmail's reply, which leaves an empty one out, so the
  person's whole row without that column is a real empty signature (the mock prints it that way).
- **The order, per person:** read, then keep (`core/signature_backups.py`: the run's `.jsonl` gets one line, written and
  `fsync`ed), then set. A read that fails means that person isn't overwritten and is listed as failed ("there'd be
  nothing to put back"); an account-wide failure stops the run as before. A copy that can't be kept stops the run with
  that person unwritten; no place to keep copies at all replaces nothing.
- **The store:** one append-only file per run, `<run>.jsonl` in `signature-backups/` of the app's data folder (`0700`
  folder, `0600` files, opened `O_NOFOLLOW`): a header (`run`, `when`, `what`), then `email`, `previous`, `set` per
  person. Appending keeps a domain-wide run linear, and a run cut off midway still holds every signature it replaced.
  Only the last `KEEP_RUNS` (5) are kept, and pruning never touches a run still being written (`_open`: a long run lost
  its file to shorter ones started meanwhile, found in review); a run that kept nobody is removed when it closes, so
  failed attempts can't push out the copies that matter. A file that can't be read whole is listed as unreadable, never
  dropped, and never loaded in part (`BackupProblem`).
- **Its cost, said on the preview:** two GAM calls a person, so a run takes about twice as long.
- **The mock** keeps a set signature byte for byte under `GAM_MOCK_STATE` and answers `print signature` with it (or each
  fixture user's canned one); its CSV writer now quotes a cell holding a line break and writes the break as GAM's dialect
  does. Tests: `tests/test_signature_backups.py`.

## Put Back (roadmap 4.1)
The Signatures page's **Kept signatures** panel (`/signatures/kept`) lists the kept runs, newest first, at most five; one
that can't be read whole is listed as such. **Put back…** posts `/signatures/putback/preview`, which loads that run and
splits it (`signature_backups.put_back_plan`): a person whose old signature GAM would read as a file to load
(`read_as_keyword`, GAM's `SORF_FILE_ARGUMENTS`) or change on the way in (`gam_stored`: `_processSignature` with `html`
removes every CR and makes each backslash-n `<br/>`; read from the vendored build) can't go back exactly and is left out
and named. The rest, with each one's old signature and what that run left there as GAM stored it, are held under a
single-use token (flow `signature_putback`, keyed on the run). **Put back** takes them, runs the guard (the count typed
past 25), keeps the signatures it replaces in a new run ("put back of …", so a Put Back can be put back too) and sets the
old ones through the same loop as Apply (`_run_writes`): read each first, and anyone whose signature isn't what that run
left there any more is left as it is ("Changed since that run"). An empty old signature goes back as empty. Tests:
`tests/test_signature_putback.py`. Not run on a real tenant: if Gmail stores a signature differently from what GAM sends,
everyone would read as "changed since" and nothing would go back, which is safe but owed a live check.

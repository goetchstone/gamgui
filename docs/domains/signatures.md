# Domain: Signatures

**One line:** Design one HTML Gmail-signature template with `{variables}` and `[[optional]]` blocks, scope it (user / company / OU / department / location / group), preview it rendered as a real person, and bulk-apply it with a live per-user ✓/✗ feed.

**Owns invariant(s):** #9 (bound anything polled — the `ApplyJob` rolling feed + capped failed list). Also carries #2 (the apply goes through the connector `_run_write` chokepoint) and #8 (saved-template bodies ride an autoescaped `data-*` attribute, never `| tojson` in the attribute).
**Enforcement home:** `tests/test_signatures.py` (render/scope), `tests/test_sig_templates.py` (store, web flow, smart-quote + linear-scan ReDoS guards). No drift guard specific to this domain.

## Files
- `gamgui/core/signatures.py` — render engine (`render_signature`, `_expand_optional`), `match_scope`, `scope_options`, `smart_quote_warning`/`_curly_quote_in_tag`, and `SignatureStore` (JSON persistence + 3 seeds).
- `gamgui/web/routes/signatures.py` — routes: `page`, `preview`, `apply` (spawns bg task), `apply_status` (poll target), `list_templates`/`save_template`/`delete_template`; the `ApplyJob`/`SigResult` progress objects.
- `gamgui/core/gam/commands.py` — `GAMCommands.set_signature` (argv `["user", email, "signature", <body>, "html"]`) and `show_signature` (`["user", email, "show", "signature"]`).
- `gamgui/core/connectors/gam_connector.py` — `set_signature` (→ `_run_write`, `RiskLevel.LOW`), `get_signature`, and `_parse_signature` (scrapes `Signature:` from text output).
- `gamgui/web/routes/users.py` — `/users/signature` (single-user set) and `/users/signature/current` reuse the same connector + `smart_quote_warning`.
- `gamgui/web/templates/` — `signatures.html` (page + `sigLoadTemplate` JS), `_sig_preview.html`, `_sig_apply.html`, `_sig_templates.html`, `_sig_current.html`.

## How it works
`page` loads active users + groups and hands scope dropdowns to `signatures.html`. `preview` resolves the scope via `_matched` (group scope needs a live `list_group_members`; everything else is in-memory `match_scope`), renders the template for `matched[0]`, and returns it inside a **sandboxed `<iframe srcdoc>`**. `apply` re-resolves the scope, creates an `ApplyJob`, and fires `_run_apply` as a detached `asyncio.create_task` (the job keeps a strong ref so it isn't GC'd); the browser then polls `apply/status` every 1s. `render_signature` first drops `[[…]]` blocks whose referenced variables are empty for that user, then does plain `str.replace` substitution — pure Python, so the preview is byte-identical to what each mailbox gets.

## Invariants & the failure history
- **#9 — bound the poll.** `ApplyJob.record` keeps only `_RECENT_WINDOW = 12` recent results and caps the failed-email sample at `_FAILED_SAMPLE_CAP = 200` (`failed_total` still counts all). Without this, each 1s poll of a domain-wide run would grow with the user count. `_prune_jobs` also drops old finished jobs so the registry is bounded.
- **Serial loop is deliberate.** `_run_apply` sets signatures one mailbox at a time *because* the per-user ✓/✗ feed is the point (see MEMORY `gamgui-scale`). Don't "optimize" it into a GAM-native bulk call — that kills the live feed. GAM-native bulk is deferred until asked.
- **#8 — no `tojson` in a double-quoted attribute.** Saved bodies go out as `data-body="{{ t.body }}"` (Jinja attribute-escaped) and JS reads `btn.dataset.body`, never `innerHTML`. `hx-vals='{"name": {{ t.name | tojson }}}'` is fine — it's inside single quotes. The ReDoS commit (`648763f`) rewrote `_expand_optional` and `_curly_quote_in_tag` from lazy regexes to linear `find` walks (regex backtracked quadratically on unclosed brackets, and render runs once per mailbox).
- **Smart-quote trap** (`bc36d52`): curly quotes pasted from Word/Mail *inside a tag* silently swallow `{variables}`; `smart_quote_warning` flags only quotes between `<` and the next `>`, never in visible text.

## Gotchas / mock-lies traps
- **`show signature` rejects `formatjson`** — it returns indented text, parsed by `_parse_signature`, not JSON (MEMORY `gamgui-gam-formatjson`; grammar `<SignatureContent>` at line 609). The mock returns fixed text, so a formatjson regression here would pass tests and break live.
- **`mock_gam.sh` is a permissive echo for the write.** There is no `signature ... html` case in the mock — it falls through to the catch-all `echo "ok"; exit 0`. So the mock proves argv is *built and dispatched*, not that GAM accepts the body, the `html` flag, or a huge/odd signature. Signature length limits and HTML acceptance are unverified by tests.
- `render_signature` substitution is naive `str.replace` over a fixed dict; it does no HTML escaping of directory values (a user whose title contains `<` lands raw in the signature) — acceptable because it's the admin's own directory, but worth knowing.

## Testing / live-verification status
Offline suite only: `.venv/bin/python -m pytest -q tests/test_signatures.py tests/test_sig_templates.py`. Covers render/optional-block/scope semantics, the store (seed/round-trip/corrupt-fallback/`0600`/validation), the templates web flow, smart-quote detection, and the two linear-time (ReDoS) bounds. **`set signature` IS confirmed live** per the README "Confirmed live" list (derived from real audit logs). The *bulk apply loop itself* — many mailboxes, failures mid-run, the ✓/✗ feed under load — has no live-tenant proof; test it on a throwaway OU before a domain-wide run.

## To do common tasks here
- **Add a template variable:** add it to `VARIABLES` and to the `values` dict in `render_signature` (both in `core/signatures.py`); add a case to `match_scope`/`scope_options` only if it's also scopable (see the Location commit `4a2c274`). Add a render test to `test_signatures.py`.
- **Add a scope type:** extend `match_scope` (in-memory) or `_matched` in the route (if it needs a live GAM lookup, like `group`); surface it in `scope_options` + the `signatures.html` dropdown JS.
- **Change the live feed / bounds:** `ApplyJob.record`, `_RECENT_WINDOW`, `_FAILED_SAMPLE_CAP`, `_prune_jobs`, and `_sig_apply.html`. Keep it bounded (#9).
- **Change how the body is sent to GAM:** `GAMCommands.set_signature` only (argv-only, #1); never build a shell string.

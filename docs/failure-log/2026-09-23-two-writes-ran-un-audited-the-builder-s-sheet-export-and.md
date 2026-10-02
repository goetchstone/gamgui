# 2026-09-23 — Two writes ran un-audited: the Builder's Sheet export and reset_password's sign-out

- **Symptom:** the 10/10 review (plan item Q6) found two GAM writes that left no audit record. A
  Builder read with "Export to a Google Sheet" appended `todrive [tduser <user>]` and ran through
  `runner.run_authenticated` as a read — creating a Sheet, possibly in another user's Drive. And
  `reset_password` followed a successful reset with `gam user <x> signout` in a `try/except: pass`,
  serialized but never recorded. Found by reading the code; not seen live.
- **Cause:** "every mutation is audited" was enforced only by `test_audit_record_only_in_run_write_or_allowlist`,
  which looks for `audit.record(` outside `_run_write`. A write that never calls `record()` at all is
  invisible to it. The export was modelled as a read because the command it extends is a read; the
  sign-out was treated as fire-and-forget.
- **Why not caught:** no test enumerated the `run_authenticated(` call sites, and the export and
  offboarding tests asserted the GAM argv, not the audit trail.
- **Fix:** the export is `GAMConnector.export_to_sheet` → `_run_write` (audited `export_to_sheet`,
  target = the Sheet's owner; `ChangeResult.output` carries the Sheet URL); the sign-out calls
  `self.signout_user` (`_run_write`), audited `ok: false` on failure without failing the reset. The
  Builder's plain reads go through `catalog_read`, which refuses a non-`READ_ONLY` command. This commit.
- **Prevention:** `tests/test_command_contract.py::test_every_run_authenticated_call_is_the_chokepoint_or_a_read`
  — every `run_authenticated(` in `gamgui/` is `_run_write`, a named allowlist entry, or traces its
  argv to a builder classified as a read in `tests/test_mock_gam.py` without the write lock (it
  names both old call sites when run against the previous code). Plus the audit assertions in
  `test_builder.py` (export ok/failed, plain read unaudited) and `test_gam_connector.py`
  (sign-out ok/failed). Mock-proven only; neither write has run live from these paths.

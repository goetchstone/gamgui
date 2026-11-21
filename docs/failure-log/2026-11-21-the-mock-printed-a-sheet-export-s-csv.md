# 2026-11-21 — The mock printed a Sheet export's CSV; GAM prints a link, needs the Sheets scope, and opens a browser

- **Symptom:** found by reading GAM's source at the pin (7.52.04, `CSVPrintFile.GetTodriveParameters` and
  `writeCSVfile`'s `writeCSVToDrive`) while building plan F4; never seen live, since no Sheet export has run on a
  tenant. Three things the Builder's one-click export (`todrive [tduser <u>] [tdtitle <t>]`) would have met:
  1. GAM finishes the file as a Sheet through the Sheets API as the service account. Setup's Domain-Wide Delegation
     link (which overwrites the client's scope list) never offered `https://www.googleapis.com/auth/spreadsheets`, so a
     tenant authorized through it would have had GAM make the file, then stop before printing its link: "User: <admin>,
     Sheets API Service/App not enabled", exit 73. The app would have said "failed" over a file that exists.
  2. GamGUI's per-call gam.cfg leaves `no_browser` false, so `todrive_nobrowser` defaults false: on success GAM itself
     runs `webbrowser.open(<link>)`, opening the Sheet in the Mac's default browser, outside the app.
  3. "Service/App not enabled" classifies as SERVICE_NOT_ENABLED, whose remediation says the service is off for the
     user: for a refused delegated scope it sends the operator to the wrong place.
- **Cause:** the mock's `todrive` handling stripped the tail and printed the read's canned CSV — "the canned CSV stays
  the output; GAM would print the Sheet's URL", as its own comment said — and its simulated tenant authorized only
  Setup's scopes, with no upload step to refuse. Nothing modelled what GAM does after the read.
- **Why not caught:** the export's tests asserted the argv and the audit record, and the mock's output for a
  `todrive` read was whatever the read printed. No test asked what GAM prints for an export, or which scopes it uses.
- **Fix:** plan F4's PR. The mock now does what `writeCSVToDrive` does: the CSV is uploaded, never printed; the link on
  stdout; 73 and the source's words when the Drive or Sheets scope is refused (GAM_MOCK_STATE/dwd_missing, shared with
  `check serviceaccount`), the file made in the Sheets case. The export passes `tdnobrowser true tdnoemail true`, is
  previewed with a scope check of both scopes (`check serviceaccount scopes <drive>,<spreadsheets>`), says a
  refused scope in words, and `core/sheet_export.outcome` tells a made file from one never made and from unknown. Setup's
  link offers the Sheets scope (`SHEETS_SCOPE`, optional: not verified at Setup).
- **Prevention:** `tests/test_sheet_export.py` (the mock's output, both refusals, the tail it accepts, the outcomes);
  `tests/test_mock_gam.py::test_mock_accepts_the_todrive_shapes_the_builder_emits` now asserts the link. Mock-proven
  only: the first live export, on the operator's yes, is what proves the output and the exit codes.

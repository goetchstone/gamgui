# 2026-10-08 — A CSV download opened as a page in the app window, with no way back

- **Symptom:** in the app, Builder → a read → **Download CSV** replaced the window's page with the CSV
  shown as text. The window has no Back control (and pywebview disables the Delete-key back), so the
  operator could only quit and relaunch. The Audit export and the onboarding template CSV did the same.
- **Cause:** pywebview's macOS backend downloads a response only when WebKit can't display it
  (`decidePolicyForNavigationResponse` → `canShowMIMEType`) or when the link carries the HTML
  `download` attribute (`decidePolicyForNavigationAction` → `shouldPerformDownload`). A CSV is
  text, so WebKit displays it, ignoring `Content-Disposition: attachment`; none of the three links had
  `download`. `ALLOW_DOWNLOADS = True` in `app.py` only enables those two paths.
- **Why not caught:** every test and the accessibility run use TestClient or headless Chrome, where an
  attachment response downloads whatever the link says; CI builds the `.app` but never launches it, so
  WKWebView's choice was never exercised.
- **Fix:** `download` on every link to an attachment route (`audit.html`, `onboarding.html`,
  `_records_meta.html`); WebKit then hands it to pywebview's save panel, in the window's own session (the
  token cookie included).
- **Prevention:** `tests/test_app_window_downloads.py` finds every GET route that sends
  `Content-Disposition: attachment` and fails unless each is linked, and every link carries `download`.
  It reasons from pywebview 6.2.1's source; that the save panel appears is unproven until the operator
  clicks Download CSV in the rebuilt app.

# 2026-09-23 — The loopback gate answered any `Host`, and exempted any path starting "/static"

- **Symptom:** a review found that `/healthz` answered a request whose `Host` was a DNS-rebound name
  (`evil.example:<port>` resolving to `127.0.0.1`) — a page on that name is same-origin with itself,
  so it could scan for our random port. And the static bypass would have waved a future `/staticfoo`
  or `/static-export` route through without the token. Reproduced offline with `TestClient`; not
  seen live.
- **Cause:** `TokenGateMiddleware.dispatch` (`web/server.py`) checked Origin and the token but never
  `Host` — the Origin comparison even took Host as "our authority" on trust. The static exemption was
  `path.startswith("/static")`, a string prefix rather than a path segment.
- **Why not caught:** every web test ran under TestClient's `Host: testserver`, which the gate had
  no opinion about; no test sent a foreign Host or a route sharing the `/static` prefix.
- **Fix:** `create_app(state, *, allowed_hosts)` — required, a bare string refused. `app.py` picks
  the port first and passes `loopback_hosts(port)` (`127.0.0.1:<port>`, `localhost:<port>`); the
  route suites pass `TEST_HOSTS` (`testserver`) explicitly. Host is checked first, before even
  `/healthz`. The bypass matches `/static` or `/static/…` only.
- **Prevention:** `tests/test_server_security.py` now runs the real `loopback_hosts` rule:
  `test_a_foreign_host_is_refused_everywhere` (7 hosts × 4 paths incl. `/healthz`),
  `test_both_loopback_spellings_of_our_port_are_accepted`,
  `test_allowed_hosts_is_required_and_never_a_bare_string`,
  `test_a_path_that_merely_starts_with_static_still_needs_the_token`; reverting either line fails
  them (mutation-checked). A real browser's Host under rebinding is not re-checked automatically.

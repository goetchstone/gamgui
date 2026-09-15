# Domain: Web Server Security

**One line:** The loopback FastAPI app factory and the one middleware that authenticates every request — per-launch token, cross-origin rejection, and security headers — plus the template rule that keeps Google-supplied directory data from breaking out of HTML attributes.

**Owns invariant(s):** #6 (loopback rejects cross-origin — the token cookie is not port-scoped) and #8 (never `| tojson` inside a double-quoted HTML attribute).
**Enforcement home:** `tests/test_server_security.py` (the token gate: bootstrap, cross-origin, token compare) and `tests/test_template_safety.py` (invariant #8, incl. a durable scanner over all templates). No drift guard beyond these two suites.

## Files
- `gamgui/web/server.py` — the app factory (`create_app`), `AppState`, `TokenGateMiddleware`, `TEMPLATES`, `/` and `/healthz`.
- `gamgui/app.py` — binds a random `127.0.0.1` port (`_free_loopback_port`), runs uvicorn in `_BackgroundServer`, builds the `http://127.0.0.1:<port>/?token=<token>` URL, points WKWebView at it.
- `gamgui/web/templates/base.html` — the shell; loads same-origin vendored `/static` scripts + external Google Fonts (the only cross-origin fetch; guarded by `Referrer-Policy: no-referrer`).
- `gamgui/web/templates/_board_members.html`, `groups.html`, `_calendar*.html`, `_suspend*.html`, … — carry directory data; use the `data-*` + `el.dataset` pattern and single-quoted `hx-vals` (see #8).

## How it works
`AppState.create()` mints `token = secrets.token_urlsafe(24)` per launch. `create_app(state)` adds `TokenGateMiddleware` (given that token), mounts `/static`, and includes one router per screen. Every request hits `TokenGateMiddleware.dispatch`: first `_same_origin` (reject cross-origin with 403), then a `/static` + `/healthz` bypass, then `_token_ok` against the cookie, then against `?token=` (which sets an httponly, SameSite=strict, session-scoped cookie). `_secure` stamps `SECURITY_HEADERS` on **every** response, including the 403s. `_lifespan` cancels in-flight `asyncio` jobs on shutdown.

## Invariants & the failure history
- **#6 — origin check comes *before* the token check, on purpose.** Cookies are not port-scoped (RFC 6265 §8.5), so `SameSite=Strict` treats every other port on `127.0.0.1` as the same site and would attach our cookie to a form POST fired by any other local process — a "simple request", no preflight. `_same_origin` blocks that: `Sec-Fetch-Site` in `{cross-site, same-site}` → reject (`same-site` *is* the other-loopback-port case); else compare the `Origin` header to `scheme://host` (host, because the port is runtime-chosen). Missing `Origin` is allowed (native WKWebView / curl / TestClient send none). Introduced in `f090d04` ("loopback CSRF").
- **Token compare is constant-time and byte-based** (`secrets.compare_digest` on UTF-8 bytes). `compare_digest`'s *str* form raises `TypeError` above U+007F, and starlette decodes raw header bytes as latin-1, so a garbled non-ASCII cookie/query must return **403, not 500** — regression-tested. Timing-safe compare landed in `f2b4a3d`.
- **Session cookie only.** The token rotates every launch, so no `Max-Age`/`Expires` — persisting it just widens the theft window.
- **#8 — directory data never uses `| tojson` in a double-quoted attribute.** `tojson` does not escape `"` (it targets `<script>` and single-quoted attrs). Attacker-influenced group/member text (from Google) goes into an autoescaped `data-email="{{ m.email }}"` and JS reads `el.dataset.email`; where JSON is needed it lives in **single-quoted** `hx-vals='{...}'`. Landed with the loopback-CSRF fix (`f090d04`): the group-members drag board (`_board_members.html`/`groups.html`) had passed `m.email` into JS via `tojson` and now rides in `data-email`.
- **CSP has no `script-src`** by design: the app uses inline event handlers and all scripts are same-origin vendored, so there is nothing remote to constrain. It does lock `object-src`, `base-uri`, `frame-ancestors 'none'`, `form-action 'self'`. `Referrer-Policy: no-referrer` stops the `?token=` in the first URL leaking to `fonts.googleapis` via `Referer`.

## Gotchas / mock-lies traps
- This domain is almost entirely HTTP/CSRF/XSS logic; the mock GAM (`tests/fixtures/mock_gam.sh`) and the vendored grammar (`gamgui/resources/gam7/GamCommands.txt`) do **not** exercise it — the guard is a real browser's request behavior, which `TestClient` only approximates. `Sec-Fetch-*` header semantics and cookie port-scoping are browser facts; TestClient sends whatever you tell it, so tests must set those headers explicitly (they do).
- Any *new* template that needs to hand a Google-supplied value to JS must use the `data-*`/`dataset` pattern — the `test_no_tojson_inside_a_double_quoted_attribute` scanner catches the tojson mistake but not, e.g., building an attribute by string concatenation.
- `Origin: null` (sandboxed iframe / `file://`) is treated as cross-origin and rejected — verified in the parametrized test.
- Not every directory-data XSS here is an invariant-#8 case. `signatures.html`'s scope dropdown had a *separate* DOM-XSS (`f2b4a3d`): JS built `<option>` markup by `innerHTML` string-concat of Google OU/dept/location values. The fix was `createElement` + `value`/`textContent`, **not** the `data-*` pattern — so JS that renders directory values must also avoid `innerHTML` concat, which no scanner catches.

## Testing / live-verification status
`tests/test_server_security.py` drives the middleware end-to-end via `TestClient` (cross-origin GET/POST/export rejected, same-origin + no-Origin allowed, non-ASCII token → 403, headers on the 403). `tests/test_template_safety.py` renders hostile values through the real Jinja env and statically scans every `*.html` for unsafe `| tojson` placement. Both run fully offline. **No real-tenant verification is needed or meaningful here** — this layer never touches GAM; it is exercised purely by the HTTP + template tests. The only live-ish concern is that WKWebView/real-browser header behavior matches the TestClient assumptions, which is not automatically re-checked.

## To do common tasks here
- **Add/patch an auth or header rule:** edit `TokenGateMiddleware` (`_same_origin`, `_token_ok`, `SECURITY_HEADERS`, `CROSS_ORIGIN_FETCH_SITES`) in `gamgui/web/server.py`, then extend `tests/test_server_security.py`. Keep origin-before-token ordering.
- **Add a new screen/route:** add `gamgui/web/routes/<name>.py`, import + `app.include_router` it in `create_app`; it inherits the gate automatically — no per-route auth.
- **Render Google/GAM directory data in a template:** put the value in a `data-*` attribute (autoescaped) and read `el.dataset.*` in JS; if you need JSON in an attribute use single-quoted `hx-vals='...'`, never a double-quoted attr. Add a case to `tests/test_template_safety.py`.
- **Add state shared across requests:** add a field to `AppState` (defaults via `field(default_factory=...)`); it is reachable as `request.app.state.gamgui`.

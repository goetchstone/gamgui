# Security Policy

## Reporting a vulnerability

Please report it **privately**, not in a public issue — a public report on a tool that holds
domain-wide admin credentials is itself a risk to everyone running it.

Use GitHub's private reporting on this repository: **Security → Report a vulnerability**
([direct link](https://github.com/goetchstone/gamgui/security/advisories/new)). If that page is not
available to you, open a public issue containing **no details** — just asking for a private channel —
and the maintainer will follow up.

Include what you did, what happened, and what you expected. A proof of concept helps enormously.
This is a small volunteer project: expect a reply in days, not hours.

## What GamGUI is, for threat-modelling purposes

GamGUI is a **local, single-operator desktop app**. It runs a FastAPI server bound to `127.0.0.1`
on a random port, gated by a per-launch token, and displays it in a native WKWebView window. (Without
pywebview it can instead be opened in a normal browser — a developer fallback with a known weakness,
below; the packaged `.app` never uses it.) There is no hosted service, no multi-tenancy, and no
remote users. Nothing is sent anywhere except to Google, by the bundled `gam` binary.

The stakes are nonetheless high, because of what the app can reach:

> `oauth2service.json` can impersonate **any user in the domain**, and `oauth2.txt` is effectively an
> administrator password. Anything that exposes those, or that causes `gam` to run a command the
> operator did not intend, is serious.

**In scope** — the adversaries this project actually defends against:

- another **local process** running as the same user, reading credentials at rest or reaching the
  loopback server (within the limits under *Known limitations*);
- **hostile or malformed data returned by Google** (display names, signatures, calendar summaries,
  event titles, group descriptions) flowing into HTML, CSV, or a `gam` argument list;
- **supply chain** — a tampered `gam` binary, or a malicious dependency;
- **another page in the operator's browser** reaching `127.0.0.1` (CSRF / DNS rebinding).

**Out of scope** — these are not defects here, and reports about them will be closed:

- no rate limiting, no account lockout, no password policy;
- no role-based access control or multi-user authorization (there is exactly one operator);
- no TLS on the loopback socket;
- anything requiring an attacker who *already* has root, or the ability to modify the app bundle.

## Properties the code is expected to hold

These are deliberate and load-bearing. A change that breaks one is a bug, and several have tests
guarding them:

- **Credentials live in the macOS Keychain.** GAM's plaintext files are materialized into a `0700`
  directory (files `0600`) only for the duration of a single `gam` call, then wiped — including via
  an `atexit` hook and an owner-PID marker, so quitting mid-call does not strand them on disk; a
  quit that cancels a call in flight also kills its `gam` process.
- **`gam` is never invoked through a shell.** Every invocation is an explicit argv list; operator
  input is always a single list element and is never string-interpolated into a command.
- **The launch environment can't steer `gam`.** It inherits only an allowlisted set of variables
  (no `DYLD_*`, no `PYTHON*`); the packaged app always runs its bundled `gam`, ignoring the
  `GAMGUI_GAM_BINARY` development override; and the app and `gam` are signed with the hardened
  runtime, so dyld ignores injected `DYLD_*` variables.
- **Every mutation is guarded and audited.** `guard.evaluate()` classifies risk and resolves the
  concrete affected set for a preview; the route that applies it re-checks the posted confirmation
  with `guard.enforce()` before any write, so a POST that skips the confirm step writes nothing (a
  tripwire posts to every route to prove it); an account delete needs the exact address typed on
  every path; a confirm step that posts the page's form runs the values its preview held under a
  single-use token, so a form edited after the preview writes nothing; and the write is then
  appended to a local audit log — as failed and "interrupted" if quitting cut it off mid-call.
- **Only read-only commands can become runnable automatically.** The Builder promotes
  grammar-derived commands to runnable *only* when they are confidently read-only; every write must
  be hand-curated. Anything uncertain stays inert. The reads whose output is itself a secret or a
  file stay runnable but every run is audited (`sensitive_read`: the command and the target, never
  the output), as a Builder sequence step too, and so is downloading such a result as CSV
  (`sensitive_csv_export`, with the row count). Exactly two kinds: 2-Step Verification backup
  codes and Chrome browser enrollment tokens (`show`/`print backupcodes`, `show`/`print
  browsertokens`, named), and every read with GAM's download verb `get`, by rule — today a user's
  Drive file or Doc, their Keep note attachments, ChromeOS device files, and user and contact
  photos (`get drivefile`, `document`, `noteattachments`, `devicefile`, `photo`, `profilephoto`,
  `contactphotos`). Other reads of a user's content (a mailbox search, Chat messages, Keep notes
  listed as text) are not audited: reads are open by design. Exporting any read to a Google Sheet
  is audited as the write it is.
- **The loopback server rejects cross-origin callers and foreign hosts.** Cookies are not
  port-scoped, so a token cookie alone would let any page on another `127.0.0.1` port drive the app;
  and every request, even the health check, must carry a `Host` of `127.0.0.1:<port>` or
  `localhost:<port>`, so a DNS-rebound page can't reach it.
- **The page runs no inline script.** Every response carries a Content-Security-Policy with
  `script-src 'self'` and no `'unsafe-inline'` or `'unsafe-eval'`: the app's scripts are same-origin
  files, no template has an inline `<script>` or `on*=` handler (a test scans for them), and htmx's
  eval features are off. So markup that directory data smuggled into a page would render but not
  run. `style-src` still allows inline styles — signature HTML is styled inline, as email HTML must
  be, and its preview is a sandboxed `srcdoc` frame that inherits the page's policy — and `img-src`
  allows any `https:` image, for the logos in those signatures.
- **The vendored `gam` binary is checksum-pinned and verified fail-closed.** An asset with no
  committed pin is refused, not installed. `scripts/bump_gam.py` writes a new pin only after
  `gh attestation verify` shows the asset was built by GAM-team/GAM's release workflow
  (`build.yml`, from `main`, on a GitHub-hosted runner) — not by any other workflow in that repo.
- **The `.app`'s Python dependencies are hash-locked.** `scripts/build_app.sh` installs them, in a
  fresh virtualenv, only from `requirements/app.txt` with `pip install --require-hashes`: every
  file, and the build backend of the one package that ships only as source, must match a committed
  SHA-256. CI installs `requirements/dev.txt` the same way, from wheels only.
- **The UI's CSS is built ahead of time, by a pinned tool.** `scripts/build_css.sh` runs the
  Tailwind standalone CLI only if it matches the SHA-256 committed for that platform in
  `scripts/tailwind_checksums.txt` (no pin, no run), and the unminified output is committed, so a
  change to it shows up in review. The page loads only same-origin files: that CSS, the bundled
  fonts (`static/fonts`, OFL, SHA-256s in `SHA256SUMS`), the app's own scripts and the SRI-pinned htmx.
  Nothing but `gam`'s own calls leaves the machine (`font-src 'self'`).
- **Every GitHub Action is pinned by commit SHA.** A tag can be moved to new code by whoever
  controls the action's repository, and the release-watch job holds a token that can push a branch
  and open a PR. `tests/test_workflow_safety.py` fails any `uses:` pinned by tag; Dependabot bumps
  the SHA and its version comment together.

## Known limitations

Accepted and documented rather than fixed; reports that only restate these will be closed.

- **Browser mode shares the session cookie with every other `127.0.0.1` port.** Cookies are not
  port-scoped, so while GamGUI runs in a normal browser, any other local web server that browser
  visits receives the token cookie and can then drive GamGUI from outside the browser. Browser mode is
  a developer fallback for when pywebview is not installed, and it prints a warning saying so; the
  packaged `.app` bundles pywebview and refuses to fall back. Use the native window for real work —
  its WKWebView keeps a cookie store of its own.
- **During a `gam` call, the credentials are readable by your other processes.** The `0700`
  directory keeps other *users* out, not other processes running as you: for the length of the call,
  any same-user process can read the plaintext files. The wipe keeps that window short; it does not
  close it. The real boundary against same-user code is the Keychain item's access control, which
  asks before any other app reads the credentials at rest.

## Using it safely

- Destructive operations are guarded, but a guard cannot prove that a given GAM command does what
  you expect against *your* domain. Check
  [Live verification status](README.md#live-verification-status) and rehearse anything unproven on a
  throwaway user, event, or calendar first.
- Account deletion is reversible only within Google's ~20-day window.
- Build and run it yourself. Released builds are not notarized for distribution to other Macs.
- GamGUI is provided **as-is under the MIT License, with no warranty**. You are responsible for what
  you run against your own tenant.

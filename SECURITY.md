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
on a random port, gated by a per-launch token, and displays it in a native WKWebView window. There
is no hosted service, no multi-tenancy, and no remote users. Nothing is sent anywhere except to
Google, by the bundled `gam` binary.

The stakes are nonetheless high, because of what the app can reach:

> `oauth2service.json` can impersonate **any user in the domain**, and `oauth2.txt` is effectively an
> administrator password. Anything that exposes those, or that causes `gam` to run a command the
> operator did not intend, is serious.

**In scope** — the adversaries this project actually defends against:

- another **local process** running as the same user, reading credentials off disk or reaching the
  loopback server;
- **hostile or malformed data returned by Google** (display names, signatures, calendar summaries,
  event titles, group descriptions) flowing into HTML, CSV, or a `gam` argument list;
- **supply chain** — a tampered `gam` binary, or a malicious dependency;
- **another page in the operator's browser** reaching `127.0.0.1` (CSRF / DNS rebinding), since the
  app can also be opened in a normal browser.

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
  an `atexit` hook and an owner-PID marker, so quitting mid-call does not strand them on disk.
- **`gam` is never invoked through a shell.** Every invocation is an explicit argv list; operator
  input is always a single list element and is never string-interpolated into a command.
- **Every mutation is guarded and audited.** `guard.evaluate()` classifies risk and resolves the
  concrete affected set for a preview; the write is then appended to a local audit log.
- **Only read-only commands can become runnable automatically.** The Builder promotes
  grammar-derived commands to runnable *only* when they are confidently read-only; every write must
  be hand-curated. Anything uncertain stays inert.
- **The loopback server rejects cross-origin callers.** Cookies are not port-scoped, so a token
  cookie alone would let any page on another `127.0.0.1` port drive the app.
- **The vendored `gam` binary is checksum-pinned and verified fail-closed.** An asset with no
  committed pin is refused, not installed.

## Using it safely

- Destructive operations are guarded, but a guard cannot prove that a given GAM command does what
  you expect against *your* domain. Check
  [Live verification status](README.md#live-verification-status) and rehearse anything unproven on a
  throwaway user, event, or calendar first.
- Account deletion is reversible only within Google's ~20-day window.
- Build and run it yourself. Released builds are not notarized for distribution to other Macs.
- GamGUI is provided **as-is under the MIT License, with no warranty**. You are responsible for what
  you run against your own tenant.

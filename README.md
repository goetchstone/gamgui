# GamGUI

A free, local, open-source **macOS GUI for [GAM7](https://github.com/GAM-team/GAM)** — administer
Google Workspace (users, groups, signatures, delegates, vacation responders, reports, and more)
without memorizing CLI commands, with your credentials kept in the macOS **Keychain**.

> GAM exposes far more of Google Workspace than the Admin Console surfaces (Gmail
> signatures/delegates/forwarding, advanced group settings, bulk operations, reporting). GamGUI
> puts a safe, native front end on top of it.

## Status

Actively developed and used against live Google Workspace tenants. Working today: first-run setup
wizard, user list/search/detail, **Gmail signatures** (a scoped designer with a live preview + bulk
apply), mailbox delegates, vacation responders, group membership (incl. a drag-and-drop board),
guarded suspend, directory profile editing (title/department/location) with a bulk "assign store"
tool, and a reports screen (2SV gaps, inactive accounts, admins, missing recovery, and
directory-completeness). You build and run it yourself; it is not yet notarized for distribution to
other Macs.

## Design goals

- **Local & native** — a single bundled `.app`; no server, nothing leaves your machine.
- **Secure** — secrets live in the macOS Keychain; GAM's plaintext credential files are
  materialized into a locked-down temporary directory only for the duration of each `gam`
  invocation, then wiped. ([details](#security-model))
- **Easy but powerful** — form/table UI for the common painful tasks, full GAM power underneath.
- **Connector-ready** — built around a connector protocol, so the Google Workspace connector is
  cleanly isolated and other systems could be added later without touching the UI.

## Architecture

```
HTMX views → FastAPI routes → Services → Connector protocol → GAMConnector
                                              → GAMRunner (subprocess)
                                              → SecretsVault (Keychain) + EphemeralConfig (temp GAMCFGDIR)
```

Wrapped in a `pywebview` native window (WKWebView). See `docs`/the plan for the full design.

## Security model

GAM stores credentials as plaintext files (`client_secrets.json`, `oauth2.txt`,
`oauth2service.json`) in its config dir. `oauth2service.json` can impersonate **any** user in the
domain and `oauth2.txt` is effectively an admin password, so GamGUI:

1. keeps the canonical copies in the **Keychain** (`keyring`, device-bound, not synced);
2. materializes them into a `chmod 700` temp dir (files `chmod 600`) set as `GAMCFGDIR` only for
   each `gam` call;
3. wipes that dir on completion (success or failure);
4. writes refreshed OAuth tokens back to the Keychain.

## Build from source

Requirements: **Python 3.9+** and **macOS** (to run the native window; the test suite itself runs on
Linux too). No Google credentials are needed to build or test.

```bash
git clone <repo-url> && cd gamgui
make setup     # create .venv, install dev + native-window deps
make gam       # vendor the pinned GAM7 binary into gamgui/resources/gam7 (needs network)
make test      # offline test suite — uses a mock gam, no binary/credentials required
make run       # launch the app (native window; prints a browser URL if pywebview is absent)
```

`make help` lists all targets. Prefer raw commands? `pip install -e ".[dev,desktop]"`, then
`scripts/fetch_gam.sh`, `pytest`, `python -m gamgui.app`. For an exact pinned install instead of the
flexible one, use `pip install -r requirements.txt`.

The GAM7 binary is **not committed** (platform-specific, large) — `make gam` / `scripts/fetch_gam.sh`
fetches the pinned version (`v7.46.01`) from the official releases and records its checksum.

### Build a standalone `.app` (macOS)

```bash
make app       # PyInstaller -> dist/GamGUI.app (bundles Python + the GAM7 binary)
```

For distribution to other Macs you must codesign + notarize the bundle (including the embedded gam
binary); running it yourself needs no signing.

### Unlock & Keychain prompts

By default the app is **ad-hoc signed**, so macOS treats each rebuild as a new identity and
re-prompts for the Keychain — and "Always Allow" never sticks. Two optional, **free** improvements:

- **Touch ID unlock.** If your Mac has Touch ID, the app prompts for it on launch (it fails open —
  Macs without Touch ID just skip it, so it's safe for anyone who builds). Turn it off with
  `GAMGUI_NO_BIOMETRICS=1`.
- **Silence the Keychain with a stable self-signed certificate** (no Apple Developer account; that's
  only needed to ship the app to *other* people's Macs). Once macOS sees a stable signing identity,
  your one-time **Always Allow** persists across rebuilds:
  1. Keychain Access → *Certificate Assistant → Create a Certificate…*
  2. Name it e.g. `GamGUI Local`, Identity Type **Self-Signed Root**, Certificate Type **Code
     Signing**; create it (login keychain).
  3. Build signed with it:
     ```bash
     CODESIGN_IDENTITY="GamGUI Local" make app
     ```
  The first launch still asks once; click **Always Allow** and you won't be asked again.

The app also caches the three secrets in-process for a sliding window (default 5 min) so a burst of
actions doesn't re-prompt; tune with `GAMGUI_SECRET_CACHE_TTL` (seconds; `0` disables).

### Tests & CI

`pytest` is fully offline (mock gam + in-memory Keychain). CI runs it on Ubuntu and macOS across
Python 3.9 and 3.12 — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Email signatures

The **Signatures** screen designs one HTML signature with variables, previews it rendered for a real
person, and applies it in bulk — scoped to a single user (for testing), a group, an org unit, a
department, a location, or the whole company. Each user's current signature is also shown *rendered*
on their detail page.

**Template variables** (filled per user from the directory):
`{name}` `{first}` `{last}` `{email}` `{title}` (`{role}` is an alias) `{phone}` `{department}`
`{location}` `{ou}`. Wrap a fragment in `[[ … ]]` to drop it when a variable inside is empty — e.g.
`[[{title} · ]]` vanishes for people with no title, so one template can roll out before every profile
is filled in.

### Hosting signature images (logo, social icons)

Gmail does **not** allow inline/base64 images or Google Drive links in signatures — every image must
be a file at a **public HTTPS URL**. GamGUI is a local app and doesn't host images itself; you point
the template's `<img src="…">` at wherever you host them. Whatever host you choose, the URL must be:

- **HTTPS** and **anonymously reachable** — Gmail fetches images through its own proxy (no
  cookies/referer) and caches them. Test a URL in a private/incognito window; if it loads there,
  Gmail can fetch it.
- served with the correct **`Content-Type`** (`image/png`, …) and **no hotlink/referer protection**
  — referer-based protection is the usual cause of "the logo shows for me but not for recipients."
- **versioned by filename** when an image changes (`logo-2026.png`) — Gmail caches by URL, so
  overwriting the same name can keep serving the old one.

Size icons ~2× their display size and set explicit `width`/`height` on each `<img>`.

**Where to host — pick one:**

- **A web host you already have (simplest).** Drop the files in a public folder, e.g.
  `https://yourdomain.com/email/logo.png`. Done.
- **Google Cloud Storage** (Google-native; reuse the GCP project GAM created). Requires a **billing
  account** linked to the project — but small signature assets fall under the Always-Free tier, so
  the bill rounds to **$0**:
  1. Cloud Console → **Billing** → link a billing account to the project (if not already).
  2. **Cloud Storage → Create bucket** — globally-unique name, a US region, Standard class, Uniform
     bucket-level access.
  3. Make objects public: bucket **Permissions → Grant access → principal `allUsers` → role
     `Storage Object Viewer`**. (If your org enforces *Public access prevention*, allow it on this
     bucket.)
  4. Upload the images.
  5. Reference them at `https://storage.googleapis.com/<bucket>/<path>/logo.png`.
  (Pricing changes — confirm the current free-tier limits, but for a handful of small PNGs it is
  effectively free.)
- **GitHub + jsDelivr (free, no billing).** Commit the images to a public repo and serve them via the
  jsDelivr CDN: `https://cdn.jsdelivr.net/gh/<user>/<repo>@<branch>/path/logo.png`. CDN-fast, no card.
- **Cloudflare R2 / Amazon S3** — or any public-object store — also work.

## License

MIT — see [LICENSE](LICENSE).

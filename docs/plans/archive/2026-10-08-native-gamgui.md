# Plan: SwiftGamGui — GamGUI as a native Mac app, with GAM still the engine

**Status: IN PROGRESS — progress is tracked in the SwiftGamGui repo's copy of this plan (first local
commit `accf1b9`, 2026-10-08: phase 1 slice 1, scaffold and spikes). This copy stays the frozen
original.**
Written 2026-10-08 at GamGUI `834493c`
for a reader with no session context. It consolidates two drafts and the operator's answers from the
same day (§1). The earlier direct-API draft, `2026-10-08-native-gamgui-direct-apis.md`, is
**superseded** and kept only for its parity map and its costings of calling Google's APIs directly.
GamGUI (Python) is **frozen except GAM updates** and stays the working tool until each screen reaches
parity. When the `SwiftGamGui` repo is created (phase 1), this plan is copied into its `docs/plans/`,
and progress is tracked there. `CLAUDE.md` is auto-loaded; its invariants carry over unless §4 says a
native design retires one. Four questions remain open (§14).

## 0. What and why

- **What:** a macOS-native SwiftUI app with GamGUI's Google Workspace scope: Setup, Home, Users,
  Groups, Signatures, Calendars, Onboard, Offboarding, Builder, Reports, Audit, Jobs. Apple's on-device
  models and Siri/App Intents are first-class, within the rule in §8. It targets macOS 27 only; this
  Mac runs 27.0.1.
- **The engine stays GAM.** Every operation is a `gam` argv, as today, run from the vendored,
  checksum-pinned binary with `Process`, never a shell. Why not call Google's APIs directly (the
  superseded draft):
  - **Coverage and correctness.** GAM already handles pagination, retries, quotas, and Google's
    quirks (the all-users calendar sweep, data transfers, `deprovision signout`), and ships releases
    nearly every week. Re-implementing that means owning every edge case and every API change.
  - **Live proof carries over.** When the Swift builders emit byte-identical argv to GamGUI's
    live-proven Python builders (§5), GAM's verified behaviour transfers instead of resetting to
    "not yet".
  - **Transparency.** Every preview shows a `gam` line an admin recognises and can copy.
  - **The key-on-disk problem is mostly solvable inside GAM** (§2).
- **Direct APIs by exception only.** An operation moves off GAM only when there's a specific, measured
  payoff (speed, a parsing pain, a sandbox need), judged case by case with the superseded draft's
  costings. None is planned.
- **Out of scope:** Apple Business Manager and Mosyle. They become a later module, planned in abapit's
  `docs/plans/2026-10-08-native-mac-app.md`.

## 1. Decisions (operator, 2026-10-08)

| Topic | Decision | Consequence |
|---|---|---|
| Name | The app is **GamGUI**, the name spoken to Siri; the repo and Xcode project are **SwiftGamGui** | The bundle ID must differ from the Python app's (in `gamgui.spec`) so both can be installed during the transition. Recommended: the same reverse-DNS prefix, ending `.swiftgamgui`. The bundle ID is permanent, because Keychain items bind to it; the display name can change later |
| Engine | **GAM stays underneath** (confirmed after both drafts were laid side by side) | §0; the direct-API draft is superseded |
| Repo | A **new, public repo**, `SwiftGamGui` | With GamGUI frozen, the shared assets are copied once and can't drift (§3). Public means the private-terms tripwire is in the first commit, and creating the GitHub repo is a separate OK |
| Distribution | People build it from source in Xcode for now | Developer ID signing and notarization come in phase 6 |
| GamGUI | **Frozen except GAM updates** | Phase 0 may change credentials and configuration, never GamGUI's code (§2). No fixes to GamGUI, so the native app is where improvements go |
| Credentials | Not shared between apps (abapit's are Apple's and Mosyle's and were never shared; GamGUI's are Google's) | The native app keeps its own Keychain items, filled once at setup (§7) |
| Sheet export (`todrive`) | Dropped; CSV is enough | The native Builder exports CSV only |
| Test license | None free | The first native offboarding and delete run on the next real leaver (§9) |
| Siri | Four requests: create an onboarding template, add an email template, set up a signature template, find a Builder command (e.g. list a shared drive's files). **Everything previews in the app by default**, plus an optional **"Just do it"** switch for simple actions | One intent each, delivered with its screen; "Just do it" rules in §8 |
| ABM / Mosyle | Later | Not in this plan |

## 2. Phase 0, in today's GamGUI: a keyless service account

Today GamGUI writes the service-account private key (`oauth2service.json`, which can impersonate
anyone) to a temp `GAMCFGDIR` for every call (invariant 4). The vendored GAM grammar has
`gam create [gcpserviceaccount|signjwtserviceaccount]` (`GamCommands.txt` line 1529). With signJwt,
the service account's tokens are signed by Google's IAM API, so **that private key never exists on
the Mac**. GAM also supports keys held on a YubiKey (`gam rotate sakey yubikey …`, line 1526).

- **Do:**
  1. Read GAM's docs for the signJwt flow: the APIs to enable, the role granting Token Creator on the
     service account, and the `oauth2service.json` it writes.
  2. **Find out which credential GAM uses to call signJwt.** If it's the admin's `oauth2.txt`, that
     refresh token now carries impersonate-anyone power, and §7's Keychain protection matters more.
  3. Check that GamGUI's import (`core/setup.py`) and verify (`check serviceaccount scopes …`) accept
     the new file **unchanged**.
  4. Then, on the operator's tenant, with the live-verify skill and per-action permission: convert,
     re-import, run Setup's Check access, then the acceptance pass.
- **The freeze applies.** If GamGUI would need a code change to accept the file, stop. The keyless
  setup then lands in the native app's phase 1 instead.
- **Result:** the only credential written to disk per call is `oauth2.txt` (0600 in a 0700 temp dir,
  wiped as now). SECURITY.md and the setup runbook say so.
- **Estimate:** 5–10% of a weekly limit, plus the operator's console steps.

## 3. Architecture

```
SwiftGamGui (repo)        App display name "GamGUI"; macOS 27; Swift 6 strict concurrency
├─ App target              SwiftUI screens, one window, native menus, Back/Home, Save panels,
│                          notifications, App Intents (thin: views render @Observable models)
└─ Packages/GamKit         one local Swift package, several products
   ├─ GamEngine            argv builders (port of core/gam/commands.py) · Runner (Process, argv-only,
   │                       env allowlist, per-call GAMCFGDIR from the Keychain, wipe + launch sweep,
   │                       timeouts, serialized writes) · parsers · exit-code kinds
   ├─ ChangeCore           Preview → Guard → Confirm → audited run (§6)
   ├─ Vault                Keychain via the Security framework (§7)
   ├─ Catalog              command_catalog.json + the read-only promotion rule (§10, phase 4)
   ├─ Jobs                 bounded long runs with Stop (port of web/jobs.py)
   ├─ Stores               role, welcome-email and signature templates; saved lists (versioned)
   ├─ Assist               Foundation Models drafts and the catalog-search tool (§8)
   └─ TestSupport          mock_gam.sh runner, golden argv, fixtures, fake Keychain
```

**Copied once from GamGUI** (frozen, so they can't drift): the vendored-GAM scripts and checksum list,
`command_catalog.json`, the strict `tests/fixtures/mock_gam.sh` with its fixtures and tests, the exit-
code table, and a golden `argv.json` generated from GamGUI's builders (§5). GAM bumps run in both repos
while GamGUI is in use, and only in SwiftGamGui after. Each repo has its own pin.

**Concurrency:**
- UI state lives in `@MainActor @Observable` models.
- The Runner is an actor. Writes are serialized through ChangeCore's executor (GamGUI's
  `_run_write` lock). Reads run concurrently.
- A `Job` actor carries each long run. Its feed is bounded (12 recent rows, a 200-failure sample, 300
  characters per error: invariant 9), and Stop takes effect between targets, never mid-call.
- While a write is in flight, quitting returns `.terminateLater`, so the write finishes and is
  audited.
- Every TTL (previews, directory cache, secret cache) uses `ContinuousClock`, which keeps counting
  while the Mac sleeps (failure-log 2026-09-24).

**State:**
- **Secrets:** Keychain only.
- **Directory cache:** memory, keyed by tenant and generation, patched on write.
- **On disk:** the stores and the calendar index, in Application Support.
- **Previews:** memory, single-use.
- **Audit:** an append-only JSONL file in its own Application Support folder (§11).
- **Onboarding credentials sheet:** memory only, with a bounded lifetime.

## 4. Invariants: what native retires and what carries over

- **Retired by the platform:**
  - **6**: no loopback server, no per-launch token, no Origin check. There's no listener.
  - **8**: no `tojson` in attributes, because SwiftUI text isn't HTML.
  - The "Run posted the live form" bug class: a confirm holds the previewed plan itself (§6).
  - pywebview's download trap (failure-log 2026-10-08): files leave through `NSSavePanel`.
- **Carried over unchanged:**
  - **1**: argv-only. `Process.executableURL` plus `arguments`, each operator value one element.
  - **2**: one write path, guarded in the executor, audited.
  - **3**: only confidently read-only commands are auto-promoted.
  - **4**: Keychain plus ephemeral materialization, now with access control (§7).
  - **7**: the GAM pin fails closed.
  - **9**: bounded live feeds.
- **Invariant 5, adapted** (§7): keep the descriptor rules for reading credential files. The
  home/`$GAMCFGDIR` roots bound stays until the sandbox decision in phase 6.

## 5. The parity contract: identical argv, the same mock

- **Golden argv.** A script runs every `GAMCommands` builder over a matrix of inputs, including the
  property-test edge cases (unicode, leading dashes, empty, very long), and writes `argv.json`. Swift
  tests assert each Swift builder emits the identical list. Because GamGUI is frozen, the file is
  regenerated only when a GAM bump changes a builder in both repos.
- **The same mock.** Swift tests run `mock_gam.sh` through the real Runner, using a test-only binary
  override that the app build ignores (failure-log 2026-09-23). The native app inherits every
  "fail the way GAM fails" lesson; an unhandled argv fails.
- **Parsers** read the same fixture files the Python parsers do, with the same expectations.
- **Exit codes** come from the vendored build's `*_RC` table, as a shared fixture (port of
  `tests/test_gam_exit_codes.py`).
- **The grammar contract** (`test_catalog_matches_grammar`, the three drift guards) ports, so a GAM
  bump that breaks a builder fails CI.

## 6. ChangeCore: the write chokepoint

The Python design (`core/guard.py`, `web/previews.py`, `_run_write`) ports. In native, a confirm can
only point at a held preview, so nothing runs a form.

- **A preview holds the exact argv lists** it shows, plus a SHA-256 digest, the tenant, the cache
  generation, its origin (form, Siri, or model draft), and a `ContinuousClock` creation time. It's
  single-use, expires after 15 minutes, and is refused after a tenant switch (failure-log 2026-09-25).
- **The guard runs in the executor, the only path to a write**:
  - confirmed (`confirm_step`)
  - a typed address for deletes, resolved to the primary address, not an alias (failure-log
    2026-09-24)
  - a typed count at the bulk threshold (10)
  - no second run already in flight for the same target (failure-log 2026-09-23)
  - the preview's preconditions re-read: if they changed, a new preview is shown, never a silent
    re-plan
- **Only the executor can start a write.** The run function takes a ticket only ChangeCore can
  create (`package` access, so the app target can't create one). A source-scan test asserts that and
  ports the cases from `test_write_routes_guarded.py`: a bare confirm, an edited form, a replay.
- **Audit:** a begin record is written before each `gam` write and an end record after it, in
  GamGUI's JSONL format: `ts`, `connector`, `action`, `target`, redacted `argv`, `exit_code`, `ok`,
  `actor`, `extra`. The native fields go in `extra`: digest, origin, begin/end. On the next launch, a
  begin record with no end is shown as "outcome unknown — check". Secrets are typed values that can't
  be logged or encoded, which closes the "surname Password" class (failure-log 2026-09-23); the
  positional masks stay as a second layer.
- **Multi-step plans** (onboarding, offboarding, sequences) declare `requires`, and a failed
  prerequisite stops its dependents. Bulk loops stop on an account-wide failure. "Retry the N that
  failed" is a **new preview**, never a write.

## 7. Credentials, setup, Keychain, signing

- **Import.** An open panel picks the GAM config folder. Each file is opened relative to the folder's
  descriptor with `O_NOFOLLOW | O_NONBLOCK`; `fstat` must show a regular file under a size cap; the
  read comes from the same descriptor. Import runs off the main actor. Domains are lowercased
  (failure-log 2026-10-02). The operator's own files are never wiped; the app may offer "Move to
  Trash" as a separate confirm.
- **Fill once from GamGUI, then nothing is shared.** The native items are filled at setup from one
  of two sources, decided by a phase 1 spike:
  1. Copied from GamGUI's `gamgui:<domain>` items after a one-time system Allow. Preferred, because
     the operator's original files may be gone.
  2. Re-imported from the files.

  The items are the same three per domain, under the native app's own service prefix. Both apps hold
  the same credential values unless the operator creates new ones; revoking one revokes both.
- **Keychain attributes:**
  - data-protection keychain, `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`, never synchronizable
  - a user-presence access control on the credential items
  - one `LAContext` reused for a session window (`touchIDAuthenticationAllowableReuseDuration`), plus
    an in-memory sliding cache, so one Touch ID covers a burst of calls (GamGUI's 300 s cache)
  - classify every `OSStatus`: only `errSecItemNotFound` (-25300) means absent (failure-log
    2026-10-01)
- **Signing for source builds.** The data-protection keychain needs the app signed with a team. A free
  Personal Team in Xcode is expected to work; ad-hoc "Sign to Run Locally" is expected to fail with
  `errSecMissingEntitlement` (-34018). Phase 1's first spike confirms this. **Moving to a paid team
  later changes the access group, and the app must re-copy or re-import once**; the README says so.
- **Sandbox.** Phase 1 ships with hardened runtime and **without App Sandbox**, which Developer ID
  distribution doesn't require. GAM runs as a child process, as it does under GamGUI. Phase 6 spikes
  whether GAM can run as a sandboxed child (inherit entitlement, PyInstaller's extension modules
  under library validation, config in the container).

## 8. Local models and Siri

**The rule (operator, 2026-10-08): everything previews in the app by default.**
- **Anything that changes the tenant, and any template**: a model or Siri only *drafts a preview*. The
  operator reviews and confirms it in the app.
- **Builder reads** open with the command filled in; the operator presses Run.
- **The exception is the optional "Just do it" switch**:
  - **Off by default.** It's a Settings toggle, and each action class can be enabled separately.
  - **Default allowlist, editable by the operator; anything else always previews:**
    - save a template (onboarding role, welcome email, signature): local, versioned, undoable
    - set or clear an auto-reply for one person
    - add or remove one person as a **member** of one group (not owner or manager)
    - set title or department for one person
  - **Never allowed, enforced in code:** deletes, offboarding, password resets, sign-outs,
    delegation, forwarding, data transfers, calendar sharing, anything at or above the bulk
    threshold, and any Builder sequence.
  - **It runs only when every target resolves unambiguously** (an exact user or group). Otherwise it
    falls back to a preview.
  - **Siri reads back the exact change** ("Set Alex Kim's auto-reply on until Friday?") before
    running. The run still goes through ChangeCore: a voice-confirm ticket only allowlisted classes
    accept, audited with `origin: siri-just-do-it`. A notification afterwards offers **Undo**, which
    runs as a normal previewed write.

**Model use:**
- On-device only (`SystemLanguageModel`). Private Cloud Compute stays off unless the operator opts in
  (§14). Tenant data never leaves the Mac.
- Availability is checked at runtime. On `.unavailable(…)` or a `LanguageModelError`, the same feature
  opens as a plain form with the operator's words kept. Every feature works with no model.
- Groups, calendars, and managers a model proposes are **looked up, never invented**; one that isn't
  found is flagged, not created.

**The operator's four requests, and the rest:**

| Spoken ("…with GamGUI") | What happens | Phase |
|---|---|---|
| "Set up a new email signature template" | The model fills a **structured** draft: fields, one of the app's layouts, tagline, logo on or off. The app renders the HTML from that layout; the model never writes raw HTML. Opens the designer with a `WKWebView` preview (JavaScript off; only images load) | 2 |
| "Create a new onboarding template" | The model drafts a role template: steps, groups, calendars, OU, signature, welcome email. Groups and calendars are looked up. Opens in the app for review; with "Just do it" on for templates, Siri reads it back and saves on yes | 3 |
| "Add this template for email" | Siri asks for the text (free text can't sit inside a fixed phrase). The result is a welcome-email template whose `{tokens}` are checked against the known list. Opens in the app for review, or is saved on a spoken yes with "Just do it" on | 3 |
| "Find the Builder command to list a shared drive's files" | A catalog-search tool runs over command names, descriptions and GamGUI's `gloss()`. The model picks from the top few (the catalog is too big for its context) and fills the slots. Builder opens with the command; the operator presses Run. If nothing runnable matches, it says so rather than offer a near miss | 4 |
| "Onboard a new warehouse worker" | Matches a role template (or asks "use Default?"), resolves the manager with a confirming question, and opens a filled Onboard preview | 3 |
| "Offboard someone", "Check access" | Open the app at the filled preview, or the Setup check | 5 |

**The shared-drive example needs one new curated read.** Checked against GamGUI's catalog:

- `print shareddrives` is runnable.
- The auto-promoted `print filelist` runs with only its User slot. The read builder drops optional
  groups, so it can't add `select shareddrive <SharedDriveName>` (`GamCommands.txt` lines 1190–1191,
  1256), and it would list that user's whole Drive instead.

The native Builder adds a curated "**List a shared drive's files**" read: a member or admin, plus a
shared drive picked from `print shareddrives`. The exact argv is checked against the grammar and run
once live before it's relied on.

## 9. Live verification

- **What carries over:** a write whose native argv is identical to a GamGUI-confirmed write is marked
  "**argv-identical to a GamGUI-confirmed write**" in a new native column of the README's live table.
- **What the native app proves itself:** credential materialization and wipe, the Runner, parsing real
  output, and job control. A row reads **confirmed** only once the native app has run it live.
- **Order on the operator's tenant**, using the live-verify skill, with per-action permission for each
  write:
  1. The read-only acceptance pass and Check access.
  2. One LOW write on the operator's own account (auto-reply on then off, or a signature set and
     restored).
  3. Writes that need no extra account, on scratch objects: a scratch group (member add and remove)
     and a secondary calendar the operator owns (share, unshare, delete).
  4. **No spare license**, so the first native offboarding runs end to end on the **next real
     leaver**, previewed, as GamGUI's first live offboarding did (2026-10-07). The account is deleted
     only after the Drive transfer completes.
  5. The Builder: one read per area through the native Runner, and the new shared-drive read.
- **Rows GamGUI never confirmed** (sign out everywhere, suspend/unsuspend, undelete, share with a group,
  unshare, delete an event, forward on / add an address, create/delete an alias, create a group) get no
  argv-identical credit. Their first native run is their first-ever confirmation.
- **Optional:** the operator can check whether Cloud Identity Free could supply a license-free test
  account for directory-only writes. This is unverified.

## 10. Testing and UI verification

- **Swift Testing** for the packages: the golden argv, the mock-backed Runner, parsers, ChangeCore
  (bare confirm, edited form, replay, tenant switch, time asleep, an in-flight target), the bounded
  job and scale tests, and Stop.
  - Generated-input tests for every parser: the CSV hire import (CRLF, empty headers, oversized
    cells) and token expansion (failure-log 2026-09-30).
  - A fake Keychain that raises the real `OSStatus` codes.
  - Fixtures never read the operator's real app data; the storage root is injected (failure-log
    2026-09-25).
- **Models**: unit tests use a fake draft provider, including one forced unavailable. Apple's
  `Evaluations` framework (macOS 27) runs an opt-in suite of phrasings per intent, which is recorded,
  not a gate. `AppIntentsTesting` (macOS 27) checks the intent definitions.
- **UI**, which the agent sees only as images:
  - Views stay thin, and the models are unit-tested.
  - Snapshots: `ImageRenderer` renders views to `NSImage`, `Attachment.record` attaches them, and
    `xcrun xcresulttool export attachments` exports PNGs the agent can read.
  - XCUITest covers the flows that matter (import → list → preview → confirm) against a **demo
    tenant** backed by the mock.
  - Every control has a distinct accessibility name, and every write control disables itself while
    in flight (GamGUI failed both three times).
  - Placeholders stay generic (`example.com`, "Sales"), enforced by the private-terms tripwire.
- **CI**: a macOS Swift job. Hosted runners may lag on Xcode 27, so until they catch up, CI builds and
  runs what it can, and full tests run locally before each PR.
- **Process carried over:** the CLAUDE.md constitution, `docs/domains/` runbooks, skills, hooks
  (including the private-terms tripwire), the failure log, RULE-FEEDBACK, and every change through a
  PR.

## 11. Coexistence and cut-over

- **GamGUI keeps working, frozen except GAM updates.** A screen moves to the native app when it reaches
  parity and its live checks pass. Whether GamGUI's copy is then retired or kept as a fallback is
  open (§14).
- **Audit:** each app writes only its own log, in the same JSONL format. The native Audit screen also
  shows GamGUI's `audit.jsonl` (and its rolled generations) **read-only**, labelled by source.
- **No cross-app lock.** The two apps could act on the same person at once, so a given screen is used
  in one app at a time; cut-over is what enforces that.
- **Non-secret stores** (role, welcome and signature templates, saved lists) are imported once from
  GamGUI's files. There's no sync afterwards.

## 12. Phases

Estimates are shares of a Max plan's weekly limit, calibrated on one careful GamGUI week (~1,650
changed lines plus reviews and live checks = 18%). Read them as ±50%. They assume one implementing
session per phase, one review agent only at phases 1 (Vault and Runner), 2 (ChangeCore) and 3
(offboarding), and no workflow fan-out.

| Phase | Scope | Done when | Estimate |
|---|---|---|---|
| 0 | Keyless service account in GamGUI, configuration only (§2) | Converted, re-imported, Check access and acceptance pass live; or deferred to phase 1 if GamGUI would need code | 5–10% |
| 1 | SwiftGamGui repo + Xcode project; framework copied; spikes (Keychain signing, copying GamGUI's items, Siri hearing "GamGUI", model availability, snapshot pipeline); GamEngine (Runner, materialization, wipe, env allowlist); golden argv, mock, parsers, exit codes; Vault + one-time fill; Setup (import, verify, tenants) and Home; demo tenant | Suites green; argv-identical for every builder; reads and Check access live on the tenant | 30–45% |
| 2 | ChangeCore; Users (list, detail, curated writes), Groups, Calendars, Signatures + **signature-template Siri intent** | Each write previewed, confirmed and audited; the LOW write and the scratch-object writes proven live; ChangeCore survived one adversarial review | 33–48% |
| 3 | Onboard (single + CSV) + **onboarding and welcome-email template intents** + the onboard draft; Offboarding; Jobs (background runs, Stop, bounded feeds) | Offboarding and delete proven live on the next real leaver; drafted and form-built plans for the same hire identical | 29–45% |
| 4 | Builder (catalog, promoted reads, the shared-drive read, results, CSV, sequences) + **command finder and its Siri intent**; Reports; Audit screen | Builder parity with GamGUI minus Sheet export; acceptance pass through the native Runner | 23–40% |
| 5 | Remaining intents ("Offboard someone", "Check access"), Shortcuts | No intent can reach the executor (test); read intents work from Siri and `shortcuts run` | 3–5% |
| 6 | Hardening: sandbox spike, VoiceOver and keyboard pass, Developer ID signing and notarization path | Reviewed and documented; GamGUI retired screen by screen | 10–15% |

**Total: about 130–210% of a weekly limit**, against 200–290% for the superseded direct-API draft.
Phases 0 and 1 are independently useful and the natural first stop.

## 13. Risks

1. **Keychain under source builds** (§7). This is the biggest unknown, and phase 1 settles it first.
2. **signJwt shifts power to `oauth2.txt`** if GAM signs with the admin's credentials (§2). Protect
   that item with user presence.
3. **Siri recognising "GamGUI"** when spoken. Checked in phase 1; if it fails, an alternative spoken
   name can be added without changing the bundle ID.
4. **The first destructive runs are on a real leaver** (§9), with no throwaway to rehearse on.
5. **Notarizing a bundled PyInstaller GAM** (phase 6): signing its binaries and the entitlements that
   hardened runtime needs.
6. **CI lag** for Xcode 27 and macOS 27 on hosted runners.
7. **The on-device model's accuracy** for the command finder. Mitigated: a search tool narrows the
   choice, the model picks from a few, and the operator always sees the command before Run.

## 14. Open questions for the operator

Answered on 2026-10-08 (§1): the engine (GAM), the repo (new and public), and Siri (preview by
default, plus "Just do it"). Still open:

1. **Phase 0:** may we convert the tenant's service account to signJwt (console steps and a live
   re-import, each with your OK), provided GamGUI needs no code change?
2. **First destructive run:** is the next real leaver acceptable for the first native offboarding and
   delete, or should Cloud Identity Free be checked for a license-free test account first?
3. **Private Cloud Compute:** keep it off (recommended), or allow it for drafts?
4. **Cut-over:** retire each GamGUI screen as its native one passes, or keep GamGUI as a fallback until
   full parity?

---

## Appendix: checked on this Mac (2026-10-08)

Xcode 27.0 (27A266a), Swift 6.4, `MacOSX27.0.sdk`, macOS 27.0.1.

- **Foundation `Process`** (`NSTask.h`): `executableURL` (line 42), `environment` (line 52),
  `terminationHandler` (line 137).
- **Security** (headers): `kSecUseDataProtectionKeychain`, `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`,
  `SecAccessControlCreateWithFlags` / `kSecAccessControlUserPresence`, `kSecUseAuthenticationContext`,
  `errSecMissingEntitlement = -34018`. **LocalAuthentication**: `touchIDAuthenticationAllowableReuseDuration`.
- **FoundationModels**: `SystemLanguageModel` with `.availability` (`deviceNotEligible`,
  `appleIntelligenceNotEnabled`, `modelNotReady`), `LanguageModelSession`, `@Generable`/`@Guide`, `Tool`,
  and `DynamicGenerationSchema` (macOS 26+); `LanguageModelError` and `PrivateCloudComputeLanguageModel`
  (macOS 27).
- **AppIntents**: `AppShortcutPhrase` and its `applicationName` token (the app name in every phrase);
  `supportedModes` (macOS 26; `openAppWhenRun` deprecated); `IntentAuthenticationPolicy`;
  `requestConfirmation`; `AppShortcutsProvider`.
- **Testing**: Swift Testing `Attachment.record` with `NSImage` attachable; `Evaluations` and
  `AppIntentsTesting` (macOS 27); `xcrun xcresulttool export attachments`. **SwiftUI** `ImageRenderer`;
  `ContinuousClock` (macOS 13). **WebKit** `WKWebpagePreferences.allowsContentJavaScript`,
  `WKContentRuleListStore`.
- **Vendored GAM 7.48.22**: `create signjwtserviceaccount` (`GamCommands.txt` 1529); YubiKey key
  commands (1525–1526); `enable_dasa` (`GamUpdate.txt` 11185). In GamGUI's catalog, 1,075 entries and
  538 runnable; `print shareddrives` is runnable; `print filelist` is runnable with a User slot only;
  `select shareddrive <SharedDriveName>` is in `<DriveFileEntity>`.
- **Not verified, so spiked or checked later:**
  - Keychain behaviour per signing mode, and copying another app's legacy item.
  - Which credential GAM uses for signJwt.
  - Siri's recognition of "GamGUI".
  - GAM as a sandboxed child, and notarizing it.
  - Cloud Identity Free.

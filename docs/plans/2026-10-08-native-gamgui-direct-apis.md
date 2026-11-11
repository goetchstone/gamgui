# Plan: native GamGUI — a Swift Mac app, Google Workspace first

**Status: SUPERSEDED by `2026-10-08-native-gamgui.md` (GAM stays the engine).** Kept for its parity map and its direct-API costings, consulted only when a specific operation is considered for a direct-API exception.
Written 2026-10-08 at `834493c` for a reader with no session context, after the operator chose
Workspace as the native app's first and main module. Apple Business Manager and Mosyle become a
later module. Answer §9's open questions before Phase 0. Mark each phase here as it lands.

## 0. What this plan is, and what it reuses

The direction (one native SwiftUI Mac app, on-device models and Siri where they help, macOS 27 only)
was first planned with abapit as the first module: abapit repo,
`docs/plans/2026-10-08-native-mac-app.md`, written the same day. That plan was not yet committed at
the time of writing; its section numbers are cited below as **[A §n]**. This plan doesn't repeat
those sections. It covers only what's specific to GamGUI.

| Topic | Reused from | What changes for GamGUI |
|---|---|---|
| App target + one local Swift package, `package`-access chokepoint, dependency rules | [A §2.1] | Packages: `GoogleAuth`, `WorkspaceAPI` (Directory, Gmail settings/send, Calendar, Data Transfer, Reports, Tasks), `Lifecycle` (onboarding/offboarding planners), `Signatures` (template rendering), `Jobs`, and later `GAMEngine` (§3). `AppleBusiness`/`Mosyle` come later (§7, Phase 8) |
| Concurrency (`@MainActor` models, one actor per client, single-flight token broker, one write executor, `.terminateLater`) | [A §2.2] | Adds the bounded `Job` actor (feed of 12 rows, failure sample of 200, Stop between targets: GamGUI's `web/jobs.py` and invariant 9) |
| Where state lives | [A §2.3] | Adds the directory cache (users/groups, generation-keyed, patched on write like `UserCache.patch`), the persisted calendar index, and the stores (roles, signature and welcome templates, saved lists) in the container. The onboarding credentials sheet is memory only, with a bounded lifetime |
| ChangeCore: preview holds the wire bytes, single-use, org+generation bound, `ContinuousClock` TTL, re-validate never re-plan, begin/end audit, failure-class mapping | [A §3] | Adds multi-target plans with GamGUI's bulk rules (§1 notes): typed count at `DEFAULT_BULK_THRESHOLD` (10), stop on an account-wide failure, "retry the N that failed" as a **new preview**, and `requires` between offboarding steps |
| Threat model, what native removes, Keychain design, sandbox / hardened runtime / host allowlist | [A §4.1–4.4] | §2 (the GAM engine weakens the sandbox story, so it's isolated) and §4 (import and migration) |
| Direct Google APIs with an RS256 assertion signed in memory | [A §4.5] | Expanded into the engine decision (§2) and the admin-token choice (§2.3) |
| Models and Siri: drafts only, deterministic resolver, read-only tools, no-model path, PCC off | [A §5] | Onboarding and offboarding drafts are the flagship and land with Phase 5, not last (§5) |
| Testing: Swift Testing, strict `URLProtocol` stubs, scrubbed recorded fixtures, fake Keychain with real `OSStatus` codes, snapshots, demo tenant | [A §6] | The Google stub becomes primary. Google's discovery documents are pinned as the "grammar", and GamGUI's strict `mock_gam.sh` is reused as-is for the GAM engine (§6) |
| Process: constitution, failure log, live table from the audit log, acceptance pass, PRs | [A §7] | GamGUI's whole framework moves over (domains runbooks, skills, hooks, the **private-terms tripwire**, RULE-FEEDBACK, improve-rules) |
| Distribution (source builds now, Developer ID later, team change → re-import) | [A §9] | No change |
| API verification against the macOS 27 SDK | [A App. A] | Additions in Appendix B here |

## 1. Parity map

Today GamGUI has 12 screens across 13 route modules (about 11.9k lines of Python, 8.5k of
templates/JS, and 18.4k of tests). Engines: **D** = Google's APIs called directly, **G** = the
vendored GAM binary (§2). A screen **cuts over** to the native app only when every write row it uses
is confirmed live in the native table (§5) and the operator agrees. Until then the operator uses
GamGUI for it.

| GamGUI screen | What it does today | Engine | Phase | What changes natively |
|---|---|---|---|---|
| **Setup** (`/setup`, 6 routes) | Import `oauth2service.json`/`oauth2.txt`/`client_secrets.json` from a GAM config dir (inode-bounded); fresh-setup commands; DWD verify (`gam check serviceaccount scopes …`); tenant switch and Remove | D | 1 | Open-panel import into the Keychain (§4). Verify becomes **one token mint per scope**, with no GAM involved; failing scopes and the pre-filled `admin.google.com/ac/owl/domainwidedelegation?…` link are kept (port `dwd_auth_url`). Fresh setup still needs the GAM CLI (`create project`, `oauth create`, `create svcacct`): the native app shows the commands as text, or the operator uses GamGUI's. A tenant switch is an org switch that bumps the generation. Domains lowercased (failure-log 2026-10-02) |
| **Home** (`/`, `/home/directory`) | Landing page and directory summary | D | 1 | Read-only dashboard from the cached directory, showing its age |
| **Users** (31 routes) | List, search, sort, and page over the cached directory; detail; title/department/OU; bulk department; suspend zone; delete zone; sign out; groups add/remove; delegates; vacation; signature; sharing their own calendar | D | 1 (reads), 2 (single-target writes), 3 (bulk department), 5 (delete) | A native `Table` with sort and search over the whole cache (no pager). Sorting and filtering run off the main actor, with a 50k-user perf check. Each zone becomes a preview sheet backed by ChangeCore. Delegate add keeps `_check_delegate`'s rules (an address, not the owner, not an alias; a warning for a non-cached or suspended user) in the shared planner |
| **Groups** (8 routes) | Group finder, members panel (owners, managers, members; 50 per page), add/remove member with role | D | 1 (reads), 2 (writes) | The type-ahead caps at 15 like `PICK_LIMIT`. Role validated in the planner, not the view (failure-log 2026-09-23, role checked on one path) |
| **Signatures** (8 routes) | Template designer with tokens, per-user preview, bulk apply job, retry the failed | D | 3 | Port `core/signatures.py` rendering with generated-input tests (`{token}` inside a value, failure-log 2026-09-30). Preview in a `WKWebView` with JavaScript off (`allowsContentJavaScript = false`) and a content rule list allowing only image loads. Bulk apply is a `Job`, and it stops on an account-wide failure (sign-in expired, scope missing) |
| **Calendars** (16 routes) | Find a calendar (persistent index built by a domain-wide job); resources; ACL detail; share/unshare; share with a group (fan-out subscribe, confirm step at ≥10); delete a secondary calendar; event search/delete | D | 4 | The index rebuild is a bounded-concurrency domain-wide read with backoff, persisted in the container and keyed by domain (the cross-tenant guard). Share with a group resolves members *before* the preview and holds them |
| **Builder** (15 routes) | Browse 1,075 catalog entries; run 538 (26 curated + 512 auto-promoted reads); preview/run; sequences; results table with filter, External only, CSV; sensitive-read audit; Sheet export | G (reads), D (curated) | 6 | §3. The curated entries open the native D actions; the GAM engine runs **reads only** |
| **Onboard** (15 routes) | Role runbooks; welcome templates; one hire or a CSV; create account; groups; signature; OU; calendar subscribe; Tasks checklist; welcome email; credentials sheet; not-ready retry (~7 min) | D | 5a | One multi-step plan per hire with `requires`. The temp password is a `Secret` value: never audited, shown once on the sheet or sent to `notify`. The not-ready retry window is ported. The model drafts the hire (§5). The CSV parser is ported with GamGUI's cases (CRLF after a round trip, empty headers, oversized cells) |
| **Lifecycle / offboarding** (5 routes) | Eight ordered steps with `requires`: reset password; revoke (sign out, app passwords, backup codes, OAuth tokens); forwarding off; delegate + auto-reply; transfer Drive+Calendar (one call, privacy `all`); remove the leaver from everyone's calendars (best-effort, up to 1 h); manager reminder; later, delete gated on the transfer finishing | D | 5b | The same eight steps as API calls (§2.2). A tolerance list for the sweep (`cannotChangeOwnAcl`, no ACL, no Calendar service, nothing else) at the HTTP-reason level. The refusal to offboard the connected admin is kept. Stop asks first. A cut-off run never reads "complete" (failure-log 2026-09-24) |
| **Reports** (2 routes) | Nine buckets over the cached users (no 2SV, inactive, admins, no recovery, …); lazy per-user storage and mail usage (`report users`) | D | 1 (buckets), 4 (usage) | `build_reports` ported as pure functions. Usage via the Reports API, 25 rows like today |
| **Audit** (3 routes) | Tail, filter, and CSV export of `audit.jsonl` | local | 2 | A viewer of the native audit. Optionally shows GamGUI's log **read-only** through an open-panel grant, labelled by source (§7) |
| **Jobs** (3 routes) | Header tray polled every 2 s; job page; Stop | local | 3 | A toolbar popover over the `Job` actors (no polling: `@Observable`); Stop between targets; an optional notification when a job finishes |

## 2. The engine decision

### 2.1 What each engine costs in a native app

**G: keep driving the vendored GAM binary.** GAM7 is Python in a PyInstaller bundle (`gam` plus 65
entries in `lib/`, including CPython extension `.so` files). On every call it reads
`oauth2service.json` and `oauth2.txt` as plaintext from a `GAMCFGDIR`. The vendored build has
`enable_dasa` ("Delegated Admin Service Account", `GamUpdate.txt` line 11185), which removes the
need for `oauth2.txt` but not the plaintext key file. In the native app, G costs:

- **Plaintext on disk per call.** Port `ephemeral.py`'s 0700 dir, wipe, atexit equivalent,
  stale-dir sweep, PID trust ceiling, bounded overwrite, and symlink/FIFO refusals. That's about
  340 lines plus their tests, and the riskiest code GamGUI has.
- **The sandbox.** A sandboxed app's child process must itself be signed with
  `com.apple.security.app-sandbox` and `com.apple.security.inherit`. Every Mach-O in GAM's bundle
  has to be re-signed with our identity. With hardened runtime, library validation will reject
  GAM's extension modules unless they carry our team's signature, or unless we opt out of library
  validation, which weakens [A §4.4]. **Unverified; this is a Phase 6 spike.** The fallback is a
  bundled **XPC service** that alone holds plaintext and runs GAM, with the main app checking the
  peer via `XPCPeerRequirement.isFromSameTeam` (macOS 26, verified). That needs a team identity,
  the same signing question as [A §4.3].
- **The build-packaging domain** (GamGUI's 173-line runbook): checksum pin (fails closed), bump
  script, three drift guards, the exit-code table, `formatjson` rejections, and stderr
  classification. On top of that, every GAM bump means re-signing and re-notarizing the bundled
  binary.
- **Speed**: GamGUI's argv builders, parsers, and exit-code knowledge port almost one-to-one.

**D: call Google's APIs directly.** The service-account key is held as a `SecKey` and signs RS256
in memory ([A §4.5]); nothing touches disk.

- **Re-derive what GAM did implicitly**: pagination; backoff on `rateLimitExceeded` /
  `userRateLimitExceeded` / 429 / 503; alias resolution (`users.get` with an alias returns the
  owner, the delete-by-alias class); Data Transfer application IDs; domain-wide iteration; error
  mapping from Google's JSON `error.errors[].reason`, never from prose.
- **About 45 operations** (table below), each needing a request builder, response model, error
  mapping, strict stub handler, and tests. That's roughly 60–120 lines apiece, **3–5k lines spread
  across Phases 1–5**. It's comparable to what porting the G path costs, and buys removing
  plaintext from every screen.
- **Every write is re-proved live** (§5). That's needed under G too, because the code around each
  call is new.

### 2.2 Recommendation: a split

- **D for everything a screen does**: setup verify, every read behind Home, Users, Groups,
  Signatures, Calendars, Reports, Onboard and Lifecycle, and **all 26 curated writes plus the
  onboarding and offboarding steps**. Nothing screen-backed ever writes a credential file.
- **G only for the Builder's ~512 grammar-derived reads and the long tail** (ChromeOS devices, Drive
  file lists, message search, everything not curated). It lands last (Phase 6), behind the XPC
  helper. The helper accepts only `(catalog ID, slot values)`, rebuilds the argv from its own catalog
  copy, and refuses any ID that isn't a promoted read. **No write ever goes through GAM in the native
  app**, which is stricter than GamGUI's invariant 3.
- **Until Phase 6 the Builder stays in GamGUI.** If the Phase 6 spike fails, or the operator prefers,
  the Builder stays in GamGUI for good, and the native app never launches a process or handles a
  plaintext credential (§9 Q3).
- **Rejected: "all G first, swap engines later".** It ports Phases 1–5 about 25–30% faster but puts
  plaintext credentials and the sandbox/XPC spike on the critical path from day one, and every
  operation is then written twice.

Mapping from GamGUI's `GAMCommands` builders to Google's APIs. Method names come from Google's public
REST references and **were not verified in this session**. Phase 0 pins the discovery documents and
tests every request against them (§6).

| GamGUI builders | Direct API (D) | Auth | Phase |
|---|---|---|---|
| `print_users`, `info_user`, `print_domains`, `print_groups`, `print_group_members`, `print_groups_member` | Directory `users.list/get`, `domains.list`, `domainAliases.list`, `groups.list` (incl. `userKey=`), `members.list` | admin | 1 |
| `show_signature`, `show_vacation`, `print_delegates`, `print_forwarding_addresses` | Gmail `users.settings.sendAs.get`, `getVacation`, `delegates.list`, `forwardingAddresses.list`, `getAutoForwarding` | SA as the user | 1 |
| `check_svcacct` | one token mint per scope (no API call) | SA | 1 |
| `add_group_member`, `remove_group_member` | Directory `members.insert/delete` | admin | 2 |
| `update_organization`, `set_suspended`, `signout_user` | Directory `users.patch`, `users.signOut` | admin (`user.security` for sign-out) | 2 |
| `set_signature`, `set_vacation`, `vacation_off`, `add_delegate`, `remove_delegate` | Gmail `sendAs.patch`, `updateVacation`, `delegates.create/delete` | SA as the user | 2 |
| `print_calendar_acls*`, `add/delete_calendar_acl*`, `subscribe_calendar`, `remove_calendar`, `print_user_calendars`, `print_all_calendars`, `print_events`, `get_event`, `delete_event`, `print_resources` | Calendar v3 `acl.list/insert/delete`, `calendarList.list/insert/delete`, `calendars.delete`, `events.list/get/delete`; Directory `resources.calendars.list` | SA as the user; admin for resources | 4 |
| `report_users` | Reports `userUsageReport.get` | admin | 4 |
| `create_user`, `create_task(list)`, `send_email` | Directory `users.insert`; Tasks `tasklists.insert`, `tasks.insert`; Gmail `users.messages.send` | admin; SA as assignee or sender (`gmail.send`, narrower than today's `mail.google.com`) | 5a |
| `reset_password`, `deprovision_user`, `forward_off`, `set_forward`, `add_forwarding_address`, `create_datatransfer`, `print_datatransfers`, `remove_all_calendar_acls`, `add_calendar_event` | Directory `users.patch` (password, change at next login), `asps.list/delete`, `verificationCodes.invalidate`, `tokens.list/delete`, `users.signOut`; Gmail `updateAutoForwarding`, `forwardingAddresses.create`; Data Transfer `applications.list`, `transfers.insert/get/list`; Calendar `acl.delete` across users, `events.insert` (`sendUpdates=all`) | admin; SA as the user | 5b |
| `delete_user`, `undelete_user`, `create_user_alias`, `delete_alias`, `create_group` | Directory `users.delete`, `users.undelete`, `users.aliases.insert/delete`, `groups.insert` | admin | 5b |
| `print_cros`, `print_filelist`, `search_messages`, `todrive_args` | stay on G (Builder), or drop `todrive` for local CSV (§9 Q6) | — | 6 |

### 2.3 The admin token: no `oauth2.txt` file, and no wider delegation

GamGUI's admin calls use `oauth2.txt` (an admin's OAuth refresh token for GAM's own OAuth client).
User-impersonation calls use the service account with domain-wide delegation, and `DWD_SCOPES`
today covers Calendar, Gmail, Drive, and Tasks. There are two ways to make admin calls directly:

1. **Recommended: reuse the admin refresh token.** At import, parse `oauth2.txt` in memory. Keep the
   refresh token, client ID/secret (from it or `client_secrets.json`), the granted `scopes`, and the
   admin's email. Store them in the Keychain and discard the file. Mint access tokens with the
   refresh grant. **No Admin console change**; admin power stays tied to one admin's consent and is
   revocable by them; `admin.directory.user.security` (sign-out) is already granted there. Both apps
   then hold the same refresh token: revoking it stops both.
2. **Delegation to an admin**: add `admin.directory.*`, `admin.datatransfer`, and
   `admin.reports.usage.readonly` to the service account's delegation and impersonate a super admin.
   That's one credential, but the SA key alone then carries directory-admin power on top of its
   impersonate-anyone power. Possible later; not the default.

Either way: one token per scope set ([A §4.5]). Delegation can also be **narrowed**: if the Builder
stays in GamGUI, the native app needs neither `mail.google.com` nor `drive`.

## 3. The Builder in Swift (Phase 6)

- **Catalog**: port `core/catalog/parser.py`. Shallow-parse `GamCommands.txt`; categories come from
  `# ` headers; risk comes from the **first known verb within the first six tokens**; an unknown verb
  means `uncertain = true` and risk `LOW`, never `READ_ONLY`. Commit the parsed catalog as JSON, as
  GamGUI's `command_catalog.json` is, and add a drift test that re-parses the vendored grammar and
  compares. Port the counts tripwire (`test_docs_state_the_real_catalog_counts`) so the docs can't
  drift from the real numbers.
- **Auto-promotion (invariant 3, unchanged)**: a command is buildable if and only if
  `risk == .readOnly && !uncertain`. The generic builder (`readbuilder.py`'s `parse_read_template` /
  `make_build`) **emits no verb of its own** and drops optional groups, except
  `[<UserTypeEntity>]`/`[<CrOSTypeEntity>]` as `user <x>`/`cros <x>` and a single-token optional
  positional. It builds `[String]` with each slot value as exactly one element. The boundary tests
  port by name: only reads became buildable, every read is buildable, the generated argv is
  injection-safe, and sensitive reads are flagged and audited.
- **Curated entries**: the 26 curated commands map to native D actions. In the Builder they open the
  native preview sheet for that action instead of building an argv.
- **Sensitive reads**: the `get` downloads and `SECRET_READS` (backup codes, browser tokens) are
  audited as `sensitive_read` with the catalog ID and never the output. Downloads land in the
  helper's container and leave only through `NSSavePanel`. A sensitive result's CSV export is
  audited as `sensitive_csv_export` with its row count.
- **Invocation, argv only (invariant 1)**: Foundation `Process` with `executableURL` and
  `arguments` (verified, `NSTask.h:42`), never `/bin/sh`, never a joined string.
  - `environment` is set to an explicit allowlist (`NSTask.h:52`): port `ENV_ALLOWLIST` and its
    tripwire (no `DYLD_*`, `PYTHON*`, or `_PYI_*`; failure-log 2026-09-23).
  - `GAMCFGDIR` is the helper's ephemeral dir.
  - Timeouts stay at 120 s, or 1 h for domain-wide reads, then terminate and kill; the result says
    the call may have done part of its work.
  - stdout/stderr are captured through bounded pipes. Exit codes come from the vendored build's
    `*_RC` table (port `test_gam_exit_codes.py`).
- **Results**: filtering runs over **every row** in the model (failure: the filter once saw only the
  first 100). "External only" counts every tenant domain and alias as internal, falling back to the
  primary when the domains read fails. CSV export is formula-injection-safe, through `NSSavePanel`.
- **Sequences**: read steps run through G. A sequence containing a curated write becomes one
  ChangeCore multi-step plan, held and previewed as a whole, refused if a step is added, removed, or
  moved after preview.
- **Sheet export (`todrive`)**: GamGUI treats it as an audited write. Drop it for local CSV unless
  the operator wants it (§9 Q6).
- **The mock**: reuse GamGUI's strict `tests/fixtures/mock_gam.sh` (unhandled argv fails, it requires
  the materialized credentials, it answers in GAM's shapes) as the test binary the helper launches.
  Copy it with its tests, and keep the rule that it must fail the way GAM fails.

## 4. Credentials and setup

**Invariant 5 under the App Sandbox.** GamGUI bounds an import to home plus a sane `$GAMCFGDIR` by
`(st_dev, st_ino)` identity. That's needed because the web route could be handed any path, and
string comparison failed on case, NFD, and the `/System/Volumes/Data` firmlink. In the sandboxed
native app the **operator's open-panel selection is the bound, enforced by the kernel**: the app can
read only what was picked. So the allowed-roots, `_root_is_sane`, and `_spellings` machinery isn't
ported, and paths are never compared. What still carries over, inside the selection:

- Open the picked directory, then each file **relative to it** (`openat`) with
  `O_NOFOLLOW | O_NONBLOCK`. `fstat` must show a regular file under a size cap (64 KiB is ample),
  and the read comes from the same descriptor. That refuses a symlinked credential and keeps a FIFO
  named `oauth2service.json` from hanging the import (failure-log 2026-09-23).
- Do the import off the main actor.
- **Never delete or overwrite the operator's files.** GamGUI wipes only its own managed staging dir,
  matched by inode. The native app has no staging dir: it only offers "Move the originals to the
  Trash" as a separate confirm.
- Lowercase the domain at import, and refuse a second spelling (failure-log 2026-10-02).

What gets stored, per domain, in the data-protection keychain under the native app's own access
group, with the [A §4.3] attributes (this-device-only, `userPresence`):

- the service-account RSA key as a `kSecClassKey`, after unwrapping PKCS#8 to PKCS#1 ([A §4.5])
- `client_email` and `private_key_id`
- the admin refresh token, client ID, and client secret (§2.3)
- for G (Phase 6) only: `oauth2service.json` and `oauth2.txt` as opaque blobs the XPC helper
  materializes. They're kept separate from the D items so removing the Builder removes them.

**Migrating from GamGUI's Keychain items.** GamGUI stores items through `keyring` in the legacy login
keychain: service `gamgui:<domain>`, account `oauth2service` / `oauth2` / `client_secrets`, with an
ACL trusting the stable "GamGUI Local" signature. The two apps should **not** share items: a
sandboxed app's access group can't include an unsandboxed Python app, and sharing would weaken
[A §4.3]. So there are two migration paths:

1. **Recommended: re-import** from the GAM config directory through the open panel. GamGUI never wipes
   files it imported from the operator's own directory, only its staging dir, so they're usually
   still there (§9 Q5).
2. **"Import from GamGUI"**: one `SecItemCopyMatching` per item against the legacy keychain, approved
   by the operator in the system's allow prompt. **Unverified** whether a sandboxed app gets that
   prompt for another app's legacy item; it's a Phase 0 spike. If it works, it's a convenience
   button. It never writes to GamGUI's items.

If neither path works (files gone, no legacy read), the operator creates a new SA key with GAM and
imports it into both apps. GamGUI's Remove still clears its own items, unchanged.

## 5. Live verification: re-proving every write

The port **resets every row of GamGUI's live table to "not yet"**. The native app's code from preview
to wire is new, and under D so is the wire itself. The native README gets its own table, filled from
the native audit log. Ten GamGUI rows were **never confirmed even in GamGUI**: sign out everywhere,
suspend/unsuspend, undelete, share with a group (the expansion), unshare, delete an event, forward
on / add an address, create/delete an alias, create a group, and Sheet export. For those, the native
run is a first-ever confirmation and gets extra care.

Order on the operator's tenant: reads first, then reversible single-target writes, then bulk, then
irreversible. Every write needs the operator's per-action permission, runs on throwaway objects
(§9 Q7), and each "do" is paired with its "undo".

| Tier | Writes (pairs run together) | Throwaway target | Phase |
|---|---|---|---|
| 0 | Every read: the native acceptance pass (Swift `scripts/acceptance.py`), per-scope token check | the real tenant, read only | 1 |
| 1 | add → remove group member; set → restore title/department/OU; set → clear vacation; add → remove delegate; set signature (one user); **suspend → unsuspend**; **sign out everywhere** | a throwaway user, a scratch group | 2 |
| 2 | bulk department (2–3 users) → restore; signature bulk apply on a scratch group of throwaways; Stop mid-run; retry the failed | 3 throwaway users | 3 |
| 3 | share a calendar → **unshare**; subscribe → remove; **share with a group** (expansion); delete a secondary calendar | a scratch calendar and group | 4 |
| 4 | Onboard a throwaway hire: create account (uses a license), OU, groups, signature, subscribe, Tasks checklist, welcome email to the operator's address; the not-ready retry; **create → delete an alias**; **create a group** | a throwaway hire | 5a |
| 5 | Offboard that hire with a second throwaway as manager: reset password, revoke access, **forward off** (and **forward on / add address** first), delegate + auto-reply, Drive+Calendar transfer, the all-users calendar sweep, the manager reminder (with an invitee) | the throwaway hire and manager | 5b |
| 6 | Irreversible: delete the account after the transfer completes → **undelete** (within Google's window) → delete again | the throwaway hire | 5b |
| 7 | Builder: each read class once (a sample per area), a sensitive read's audit record | read only | 6 |

## 6. GamGUI-specific testing notes (on top of [A §6])

- **Google's discovery documents are the grammar.** Phase 0 fetches the public discovery documents
  for Directory, Data Transfer, Reports, Gmail, Calendar, and Tasks; that's documentation, not a
  tenant call. They're pinned in the repo. A test checks every native request's method, path
  template, and required parameters against them, the way `test_catalog_matches_grammar` holds
  GamGUI to `GamCommands.txt`. A re-pin is a reviewed bump, like a GAM bump.
- **The Google stub fails the way Google does**, using what GamGUI learned live, re-expressed at the
  HTTP level. The exact reasons are captured by the read-only recorder before the stub encodes them:
  - an alias resolving to its owner
  - 404 for an unknown user, 409 for a member that already exists
  - 403 for a missing admin privilege versus a rate limit (never conflated)
  - `cannotChangeOwnAcl`, and a user without the Calendar service
  - 409 on two overlapping same-user data transfers
  - a new account refusing signature and calendar calls for minutes after creation
  - `unauthorized_client` for an undelegated scope
  - a license-limit refusal on create
- **Port the cases, not the code**, from GamGUI's 18.4k lines of tests:
  - `test_write_routes_guarded.py` → ChangeCore entry-point tests: a bare confirm writes nothing, an
    edited form isn't run, a replay writes nothing
  - `test_tenant_switch.py`, `test_previews.py` (including time asleep), `test_bulk_stop.py`,
    `test_offboard_safety.py`, `test_onboarding.py` (only the previewed hire runs; the password is
    redacted), the Hypothesis properties (CSV, token expansion), and the bounded-job scale tests
- **UI**: GamGUI's "every button has a distinct name" (three failures) and "every write control
  disables itself in flight" become UI-test assertions. Placeholders stay generic (`example.com`,
  "Sales", "Training Calendar"), backed by the private-terms tripwire.

## 7. Coexistence with GamGUI (Python)

- **GamGUI stays the working tool**, unchanged, until each screen cuts over (§1). Recommendation:
  once a screen has cut over, GamGUI gets only fixes for that screen. After full parity, the operator
  decides whether to retire or keep GamGUI as a fallback (§9 Q8).
- **Keychain: not shared** (§4). The two item sets are independent; removing credentials in one app
  doesn't touch the other. The admin refresh token is the same grant in both: revoking it stops both.
- **Audit logs: separate, and each app writes only its own.** GamGUI's is `audit.jsonl` plus up to
  10 rolled generations in its app-data directory, recording `ts`, `connector`, `action`, `target`,
  redacted `argv`, `exit_code`, `ok`, `actor`, and `extra`. The native app writes begin/end records
  with typed fields, a digest, and an origin [A §3]. The native Audit screen can show GamGUI's log
  read-only through a security-scoped grant, merged by time and labelled by source. Each app's live
  table comes from its own log only.
- **Non-secret stores** (onboarding roles, signature and welcome templates, saved lists): a one-time,
  read-only import from GamGUI's app-data files through the open panel, with the formats documented
  from GamGUI's store code. No sync afterwards; edits made in one app aren't seen by the other.
- **No cross-app lock.** Both apps could act on the same user at once (two offboardings, failure-log
  2026-09-23). The rule is that a screen is used in one app at a time, and cut-over is what enforces
  it. This is a risk while both apps are live (§8).
- **GAM**: GamGUI keeps its vendored GAM and pin. If Phase 6 lands, the native app vendors its own
  copy under its own checksum list and is bumped separately.

## 8. Phases

**Calibration** (same as [A §8]): one GamGUI week of ~1,650 changed lines plus reviews and live
checks used 18% of a Max weekly limit, about 1.1% per 100 lines. Native Swift on a new toolchain is
estimated at **1.2–1.6% per 100 lines**. Assumptions:

- one implementing session per phase
- **one** review agent only at 0 (Keychain spikes), 2 (ChangeCore), 5b (offboarding), and 6 (GAM
  helper)
- no workflow fan-out
- the operator runs the live checks while the agent reads the audit output

UI iteration could add 20–30%.

| Phase | Scope | Est. changed lines | Share of weekly limit |
|---|---|---|---|
| 0 | Scaffold, spikes, framework, pinned discovery docs | 600–900 | 7–11% |
| 1a | Setup + auth (RS256, refresh grant, per-scope verify) + Users list | 1,800–2,200 | 22–30% |
| 1b | User detail reads, Groups, Home, Reports buckets, demo tenant | 1,400–1,800 | 16–24% |
| 2 | ChangeCore + Audit + Tier-1 single-target writes, re-proved | 2,000–2,600 | 26–36% |
| 3 | Jobs + Signatures + bulk department, re-proved | 1,800–2,400 | 22–32% |
| 4 | Calendars (index, ACLs, fan-out, events) + usage report | 2,000–2,600 | 24–34% |
| 5a | Onboard (single + CSV) + the model's onboarding draft | 1,600–2,000 | 20–28% |
| 5b | Offboarding + gated delete, alias and group writes | 1,500–1,900 | 20–27% |
| 6 | Builder: catalog, GAM XPC helper, results, sensitive audit, sequences | 3,000–3,800 | 36–52% |
| 7 | Siri / App Intents for Workspace | 700–1,000 | 9–14% |
| **Workspace parity (0–7)** | | **~16k–21k** | **~200–290%: two to three weekly limits** |
| … without the native Builder (6) | | ~13k–17k | ~165–240% |
| 8 | ABM/ASM + Mosyle module: abapit plan phases 1a–3c, minus shared infrastructure already built | ~9k–12k | ~95–130% |

**Phase 0: scaffold and spikes.** This Mac must be on macOS 27 and §9 answered first. Scope:

- The Xcode project and AdminKit package ([A §8, Phase 0]), and the GamGUI framework copied in:
  CLAUDE.md constitution, domains, skills, hooks with the private-terms tripwire, failure-log,
  RULE-FEEDBACK.
- The pinned discovery documents and their contract test.
- Spikes:
  1. Data-protection keychain under each signing mode ([A §4.3]).
  2. **Reading GamGUI's legacy `gamgui:<domain>` item from the sandboxed app**, using a throwaway
     item under a test service name, never the real one.
  3. Foundation Models availability.
  4. The snapshot pipeline.

Done when the build and tests run from the CLI, the entitlement check passes, the spike results are
written into this plan, and one review agent has read the Keychain spikes.

**Phase 1a: setup and users.** Scope:

- Open-panel import (§4); RS256 assertion with PKCS#8→PKCS#1; refresh grant; per-scope verify with
  the failing scopes and the DWD link.
- Tenant list, switch, and Remove (classifying a refused delete, failure-log 2026-10-01).
- The cached user list with sort, search, and age. The strict Google stub, including **signature
  verification** of the RS256 assertion.

Done when:
- The operator imports a real tenant. The originals are untouched, and nothing in the container
  holds a PEM or a refresh token.
- Verify reports the same scopes GamGUI's check does.
- The user count matches GamGUI's, and the 50k synthetic list sorts and filters smoothly.
- The acceptance pass covers these reads.

**Phase 1b: read parity.** Scope: user detail (groups, delegates, vacation, signature, forwarding),
Groups, Home, Reports buckets, and the demo tenant. Done when every Phase-1 screen's reads match
GamGUI's on the live tenant and the acceptance pass covers each.

**Phase 2: ChangeCore and the first writes.** Scope: ChangeCore in full [A §3], the Audit screen,
and the Tier-1 writes. Done when:
- One adversarial review agent tried to break ChangeCore and failed (the [A §8 Phase 2a] list, plus
  a bulk preview edited after preview).
- The lid-close TTL test passed.
- Every Tier-1 pair is confirmed live with per-action permission, and the README rows read
  **confirmed**. Suspend/unsuspend and sign-out are first-ever confirmations.
- Users and Groups are eligible to cut over.

**Phase 3: jobs and signatures.** Scope: the `Job` actor, the tray, Stop, stop on an account-wide
failure, retry-the-failed as a preview; Signatures (designer, render port, `WKWebView` preview,
apply); bulk department. Done when Tier 2 is confirmed live, the scale tests pass at 50k, and
Signatures is eligible to cut over.

**Phase 4: calendars and usage.** Scope: the index rebuild (bounded, persisted, keyed by domain),
ACLs, share and the group fan-out, subscribe, delete calendar, events, and the usage report. Done
when Tier 3 is confirmed live, the index matches GamGUI's for the same tenant, and Calendars and
Reports are eligible to cut over.

**Phase 5a: onboarding.** Scope: role runbooks; single and CSV hires; the multi-step plan with
`requires`; the credentials sheet; the not-ready retry; the onboarding draft with the no-model form
path (§5 below). Done when Tier 4 is confirmed live, the drafted and the form-built plans for the
same hire are identical, and Onboard is eligible to cut over.

**Phase 5b: offboarding and delete.** Scope: the eight steps as API calls with `requires`; the
sweep's tolerance list; transfer completion gating delete; undelete; alias and group writes. Done
when:
- One review agent tried to break it: a cut-off run, a failed prerequisite, offboarding the connected
  admin, two runs of one leaver.
- Tiers 5 and 6 are confirmed live.
- Lifecycle is eligible to cut over. **Workspace parity without the Builder ends here.**

**Phase 6: the Builder.** Scope: §3. The spike first: GAM inside the sandbox versus the XPC helper,
and library validation. If neither passes without weakening [A §4.4], stop, and the Builder stays in
GamGUI. Done when the boundary tests pass, the helper refuses non-read IDs, the Tier-7 reads succeed
live, and GAM's pin, checksum, and bump work in the new repo.

**Phase 7: Siri and App Intents.** Read intents: look up a user, who's suspended, a group's members.
Write-shaped intents: "Prepare offboarding for …" opens the preview. Done per [A §8 Phase 3c].

**Phase 8: ABM/ASM and Mosyle.** Follow the abapit plan's Phases 1a–3c on the infrastructure already
built.

### Models and Siri for Workspace (by reference to [A §5])

| Draft | Generated fields | Resolver (deterministic) |
|---|---|---|
| "Onboard a new warehouse worker starting Monday" | `OnboardingDraft {firstName?, lastName?, role, manager?, startDate?}`; `role` comes from a `DynamicGenerationSchema` built from the operator's own role runbooks, so the model can only pick an existing role | Missing fields become form questions; the manager is resolved to a canonical `primaryEmail` by a read-only directory-cache tool; the address is generated by the role's own rule, never by the model |
| "Offboard Jane; her manager is Sam" | `OffboardDraft {leaver, manager, autoReplyText?}` | Both resolved to canonical users. The connected admin is refused. The eight steps come from the planner, not the model |
| "Suspended people in Sales without 2-step" | `UserFilter` | Applied to the cached list as editable chips; read only |

The model may draft the **auto-reply text** into an editable field, never a recipient or a target.

## 9. Open questions only the operator can answer

1. **App name and bundle ID.** Keep "GamGUI" for the native app, or a new name? Keychain items and
   the container bind to the bundle ID, so renaming later means re-importing.
2. **Repo**: a new repo (recommended: a Swift toolchain and CI, while GamGUI's Python CI stays put),
   or a `native/` folder in this one? Public or private? If public, the private-terms tripwire is
   needed from the first commit.
3. **The Builder**: native through a GAM helper in Phase 6 (about 36–52% of a week, and plaintext
   credentials return in one isolated process), or keep it in GamGUI indefinitely, so the native app
   never handles a plaintext credential or launches a process?
4. **Admin auth for direct calls**: reuse the `oauth2.txt` refresh token (recommended: no Admin
   console change), or widen delegation to admin scopes?
5. **Migration source**: are the original `oauth2service.json`/`oauth2.txt` still in a GAM config
   directory to re-import? If not, is a new service-account key acceptable, or does the "Import from
   GamGUI" spike need to succeed?
6. **Sheet export (`todrive`)**: drop it for local CSV, or keep it (Builder/GAM only)?
7. **Throwaway targets for re-proving**: a free license for a throwaway hire (the first live
   onboarding hit the domain's user limit), a second throwaway to act as manager, a scratch group, a
   scratch calendar. Which exist, and may the operator's own address receive the test welcome email?
8. **Cut-over and retirement**: freeze each GamGUI screen to fixes once it cuts over? After parity,
   retire GamGUI or keep it as the fallback?
9. **Model features**: which drafts matter (onboarding, offboarding, the user filter)? Keep Private
   Cloud Compute off (recommended)?
10. **Phase 8 priority**: does ABM/Mosyle follow straight after Workspace parity, or after the
    Builder? The abapit plan's Mosyle and ABM-write questions still stand for that module.

---

## Appendix B: additions to [A App. A], checked against the installed SDK on 2026-10-08

Xcode 27.0 (27A266a), `MacOSX27.0.sdk`.

- **Foundation `Process` (NSTask.h)**: `executableURL` (macOS 10.13, line 42), `environment`
  (line 52), `terminationHandler` (line 137).
- **XPC (Swift overlay)**: `XPCPeerRequirement` (macOS 26.0), `.isFromSameTeam(andMatchesSigningIdentifier:)`,
  `XPCSession.setPeerRequirement(_:)`.
- **WebKit (headers)**: `WKWebView`, `WKWebpagePreferences.allowsContentJavaScript` (macOS 11.0,
  line 102), `WKContentRuleListStore` `compileContentRuleListForIdentifier:…` (line 39).
- **Vendored GAM 7.48.22** (`gamgui/resources/gam7/`): a PyInstaller onedir (`gam`, an arm64 Mach-O,
  plus 65 entries in `lib/`, including CPython 3.14 extension modules); `GamUpdate.txt` documents
  `enable_dasa` (line 11185). The Google discovery documents aren't readable inside the bundle, so
  the API method names in §2.2 are **unverified** until Phase 0 pins them.
- **Unverified, to spike**:
  - A sandboxed app reading another app's legacy keychain item (Phase 0).
  - `com.apple.security.inherit` and library validation for GAM's bundle under hardened runtime
    (Phase 6).
  - Whether the sandbox refuses a symlink inside a picked directory that points outside it
    (`O_NOFOLLOW` is used regardless).

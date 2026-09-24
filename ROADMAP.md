# Roadmap

Where GamGUI is likely to go next. Nothing here is a commitment — it is a ranked backlog, ordered by
how much a **solo Workspace admin at a 50–5,000 person company** would actually use it, weighed
against the effort to build it on top of what already exists.

The organizing constraint: every mutation must keep going through the same chokepoint —
`GAMCommands` argv builder → `guard.evaluate()` → audited `_run_write()`. A feature that needs a way
around that is not on this list. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Next — small, and each closes a real daily gap

- **Make the report buckets actionable.** `/reports` already computes the worklists that matter
  (inactive 90+ days, no 2SV, suspended, missing recovery). Every one is currently a dead end you
  can only click through one user at a time. The bulk executor and the guard already handle exactly
  this shape of operation.
- **Job control: a Stop button, and retry-only-the-failures.** A mis-scoped bulk apply currently
  runs to completion with no way to halt it, and a run with 40 transient failures can only be redone
  in full. Every job type already tracks its failures; the missing pieces are a `cancelled` flag
  checked in the loop, and a scope that means "just these users."
- **Offboarding: skip a step that doesn't apply.** Half there: each step has an "already done" box,
  so a re-run skips the steps that succeeded, and preview and run stay in sync off the step `key`s.
  Left: a "doesn't apply" choice (transferring 40 GB of Drive nobody wants) that the preview shows
  as skipped rather than counting it as done.

## Then — bigger, still clearly worth it

- **Back up signatures before a bulk overwrite.** Bulk apply replaces every matched user's signature
  with no backup and no undo. Capture the previous value per user first, and offer a restore.
- **Bulk apply from CSV.** The counterpart to the CSV export that already ships. Real work arrives as
  a spreadsheet — new hires already import from one on the Onboard screen; a reorg or a phone-number
  cleanup still can't.
- **Org-unit management.** Every Workspace policy derives from the OU, and GamGUI is read-only about
  it: it displays `orgUnitPath`, buckets reports by it and places a new hire in their role's OU, but
  cannot move anyone.
- **License assign / remove / report.** Offboarding transfers Drive and sets an auto-reply but leaves
  the paid seat assigned. That is money, every month, silently.
- **Group settings** — posting permissions, moderation, who can join, external senders. The Groups
  screen manages membership only, while the actual tickets are settings.
- **Compromised-account playbook.** A password reset is the reflex and is not enough: attackers
  persist via forwarding rules, filters, delegates and app passwords. One screen that surfaces what
  was changed and reverses it.
- **Delegate & forwarding review, domain-wide.** "Who can read whose mailbox, and whose mail is
  leaving the building?" is answerable today only one user at a time, so nobody audits it.
- **Shared drives** — inventory, membership, and the orphaned-drive check before deleting a
  departing user.

## Later

- **Grow the buildable command set — specifically the writes.** Of 1,075 catalog entries, 538 can
  run today: 26 hand-curated commands (the only ones that can *change* anything) plus 512
  grammar-derived commands auto-promoted because they are confidently read-only. The other 537 —
  every write, and anything whose risk could not be inferred with confidence — are inert, syntax
  display only. That asymmetry is the safety boundary: a command can become runnable automatically
  only if it cannot mutate, so adding write coverage stays a deliberate, reviewed act of curation.
- **Step-to-step dataflow in the sequencer** — bind one step's output to the next step's input.
  Deferred from the original Builder design; steps are independent today.
- **Export a sequence to `gam batch` / CSV**, so the visual builder composes with GAM's own batch
  machinery instead of competing with it.
- **A GAM-native fast path for domain-wide signature apply.** The per-user loop is deliberate — it is
  what powers the live ✓/✗ feed — but a genuine whole-domain push would be minutes instead of hours
  via `gam all users` or a `gam csv` batch. Worth adding as an *opt-in alternative*, not a
  replacement.
- **A second connector** (Mosyle MDM, Apple Business Manager, PBXact) plus cross-system person
  lifecycle — the original reason the connector protocol exists.
- **Notarization**, if the app is ever to be handed to another Mac. Needs an Apple Developer ID.

## Deliberate trade-offs

Two things this project does not do today. Neither is a principle, and neither is closed off: they
are simply not on the maintainer's list. The reasoning is written down so it can be argued with — and
the design should keep both *possible*, so a change here should be a decision, never a dead end you
discover later.

- **Windows / Linux builds.** Not a technical barrier: the core is portable and the test suite
  already runs on Linux in CI. What is macOS-specific is the *shell* — the WKWebView window, Keychain
  storage, codesigning, and the `.app` bundle — plus GAM's own per-platform binaries. Nobody has
  asked for it, so the effort goes elsewhere. The connector/core split is where you would start if
  that changed.
- **A free-text `gam …` box.** The tempting escape hatch, and the trade-off is narrower than it
  sounds. *Reads are already open*: 512 grammar-derived read commands run today with no curation at
  all (the handful whose output is a secret or a file — backup codes, browser tokens, and every
  `get` download: Drive files and Docs, Keep attachments, ChromeOS device files, photos — are
  audited when run; SECURITY.md lists them). The line is drawn at **writes** — every command that can change something is hand-modeled, so
  a preview can show the real blast radius before you confirm, and every write lands in the audit
  log. A free-text box that could write would end that property, because the risk of an arbitrary
  command line has to be *inferred* from its verb, and the catalog itself marks some verbs
  uncertain. It could still be built safely — split into argv with `shlex` (never through a shell),
  classify, and treat anything unrecognized as destructive so it fails safe into a typed
  confirmation. That is a design worth doing on purpose rather than by accident. Ask if you want it.

## Verification debt

Some write paths have never run against a real tenant. That list, and what *has* been confirmed
live, is tracked in the README under
[Live verification status](README.md#live-verification-status) — it is a safety notice, not a
roadmap item, and it is the first thing to check before trusting a destructive action.

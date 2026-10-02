# Plan: configurable offboarding

**Status: IN PROGRESS — P1.1 applied; P1.2–P1.5 and P2 open.** Written 2026-10-02 at `b00a140` for a reader with no session
context. `CLAUDE.md` is auto-loaded; read `docs/domains/lifecycle-offboarding.md` before any item.
When a PR closes an item, mark it here in the same PR.

## Context

Offboarding runs eight fixed steps in a fixed order (`core/lifecycle.py` `build_offboard_steps`):
reset password, revoke access, forwarding off, delegate to the manager, auto-reply, transfer Drive
and Calendar to the manager, remove from everyone's calendars, a reminder for the manager. The only
choice today is an "already done" box per step, for a re-run.

GamGUI's users are anyone who manages Google Workspace with GAM on a Mac, so the routine has to fit
processes other than the operator's. The operator's own routine is exactly today's eight, the
reminder to delete the account included, so every item must leave the default untouched. They don't
archive and have no OUs, but may later, and other users already do.

A research and design pass (2026-10-02; Google's admin guidance, the GAM community, and MSP
checklists from Suitebriar, AccessOwl, Damson, GAT and larsen161) found that orgs differ on four
axes: which steps run, who receives the data, what happens to the account afterwards (suspend, a
"Departed" OU, the license), and what waits until later (day 30–90) rather than happening on the
day. Common steps the app lacks: remove from groups (after saving the list and handing over owned
groups; near-universal), hide from the address book (GAL off), remove admin roles, clear the
recovery email and phone, a data recipient other than the manager, an auto-reply end date, suspend
and move OU, license removal or an Archived User swap, and the old address as an alias after
delete. Two styles recur: security-first exits (lock at once, no auto-reply) and friendly exits
(hand the mailbox over, transfer after 30 days).

**Direction (the operator, 2026-10-02): keep it simple.** One offboarding screen with a short list of
common steps almost anyone wants, each on or off, defaulting to today. No playbooks and no step
library. Anything else is the Builder's job: its reads run any read-only GAM command (for example
Users → Print filelist for the leaver's Drive) and export to a Sheet or CSV.

## Rules every item keeps

- **Defaults are today.** A run with no option changed sends today's eight argv lists byte for byte
  (pinned by P1.1's golden test).
- **Steps come from code, never data.** A choice or a saved playbook names step keys and
  parameters; it never holds argv or GAM text (CLAUDE.md #1, #3). Step order is fixed in code.
- **The run executes what the preview held.** Every option is part of `_form_key` and the held
  `_Preview`; nothing is re-read at Run (CLAUDE.md #2; the onboarding precedent is
  `test_run_uses_the_role_as_previewed`).
- **The minimum lock always runs.** Password, revoke and forwarding-off offer Run or Already done,
  never "doesn't apply".
- **"Doesn't apply" is not "skipped".** A step that doesn't apply never counts as succeeded (unlike
  an "already done" tick), never makes the panel read "stopped", and is named in the preview, the
  panel and the audit record.
- **`REQUIRES` stays pinned.** A step that doesn't apply is replaced in a dependency by its own
  requirements; a step that failed still blocks.
- **Every new write step is curation:** a `GAMCommands` builder from the vendored grammar, a strict
  mock handler that fails the way GAM fails, tests, and a live run on a throwaway user before it is
  relied on (the live-verify skill; the operator's yes per command).
- **Never:** the account delete inside the routine, a full remote device wipe, a scheduler, or calls
  to HR systems or webhooks.

## Items

### P1 — "Doesn't apply" and per-run options (the ROADMAP item)

- **P1.1** Golden test pinning today's eight default argv lists, then a "Doesn't apply" box for each
  optional step (delegate, auto-reply, transfer, calendar sweep, reminder) beside the "already done"
  boxes; ticking both for one step is refused. `effective_requires(key, not_applicable)`;
  `job.not_applicable`; the preview names each left-out step's cost, and its dependency paragraph,
  the reminder wording and the run panel follow what runs; one `offboard_plan` audit record when a
  step is left out (the #36 precedent).
- **P1.2** Remember my choices: only if a user asks for it (the default is everyone's starting point,
  and a routine that differs on every run is the case this would serve). Same store rules as
  onboarding's, except a corrupt file is refused and said so, never silently replaced.
- **P1.3** Transfer options: a recipient (default the manager; checked at preview and at run like the
  manager), services (Drive, Calendar, Looker Studio), and `release_resources` only after the mock is
  tightened (it accepts it without Calendar, likely more permissively than GAM). The preview states
  what a skipped transfer costs ("Drive is not transferred; deleting the account loses it"). With
  every manager-facing step left out (delegate, auto-reply, transfer, reminder) the manager field
  should become optional instead of required and checked (P1.1 review).
- **P1.4** Auto-reply end date, held as an absolute date.
- **P1.5** A parametrized test: changing any option changes the form key.

### P2 — A few more common steps, off by default, in the same list

One PR each: GAL off; `deprovision popimap` as part of revoke; clear recovery email and phone (only
after a live test shows GAM 7.48.14 accepts an empty value; the mock rejects it until then); remove
admin roles (read in the preview, one argv per assignment); groups (promote a new owner, then remove
the leaver group by group, the memberships recorded in the preview so it can be undone).

### Not planned (keep it simple)

- **Playbooks and a step library.** Steps stay a short fixed list, each on or off.
- **A separate "finish offboarding" screen.** Suspend is already on the user's page; archiving or
  removing the license comes with the ROADMAP's license item, on the same page; the run panel points
  there. Moving to a Departed OU comes with the ROADMAP's org-unit item.
- **Anything one-off**, like listing a leaver's Drive or exporting their groups: the Builder.
- Forwarding to the manager, shared drives, device wipe, Vault, a recipient per service, rename, bulk
  offboarding.

## Before P2 ships

Offboarding has never run on a real user (README "Live verification status", runbook "first live run
checklist"). Prove today's eight on a throwaway user first, so new steps aren't layered on unproven
ones.

## Status by item

- **P1.1** — applied (this PR: "Doesn't apply" per optional step, the pinned default, `offboard_plan`).
- P1.2–P1.5, P2 — open.

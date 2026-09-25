# Using GamGUI

A task guide for the Google Workspace admin who runs GamGUI day to day. It says where each task
lives, what to check before you start, and what to check afterwards. Building the app is in the
[README](../README.md#build-from-source); this page starts once it opens.

Two things hold everywhere:

- **Reading never changes anything.** Searching, opening a person, Reports and every Builder read
  only look. The big changes — onboarding, offboarding, bulk runs, suspend, delete, and anything run
  from the Builder — show a preview first, with the exact `gam` command, and run only after you
  confirm. A single small change (a delegate, a group member, one calendar share) runs when you
  click it; a few ask first. Every change is written to the audit log.
- **"Not yet verified live" means untested on a real domain.** The
  [live verification table](../README.md#live-verification-status) marks each kind of change
  **confirmed** (it has succeeded against a production Google Workspace domain) or **not yet** (the
  offline tests pass and the command matches GAM's documented syntax — nothing more). Run anything
  marked *not yet* once on a test account, group or calendar before you rely on it, and check the
  result in Google yourself.

## First setup

Open **Setup** (every screen points you there until a domain is connected).

1. **Your domain** — type your super-admin email; the primary domain fills itself in.
2. **Import existing credentials** — if you already use GAM, GamGUI lists the GAM folders it found
   (`$GAMCFGDIR`, `~/.gam`) and which credential files each holds. Click **Import** on the right
   one, or give a custom folder. The files are moved into the macOS Keychain. No GAM yet? **Show
   setup commands** gives the exact Terminal commands to create them, then import the result.
3. **Domain-Wide Delegation** — the one step Google makes you do by hand. Setup shows the service
   account's client ID and the scopes GamGUI uses, with Copy buttons. **Authorize in the Admin
   Console** opens Google with both filled in: sign in as a super admin, click **Authorize**, wait
   about 30 seconds, then click **Verify access** back in GamGUI.

   | Scope | What it is for |
   |---|---|
   | `https://www.googleapis.com/auth/calendar` | Calendar sharing, the offboarding sweep and reminder |
   | `https://mail.google.com/` | Gmail (GAM's default); message search |
   | `https://www.googleapis.com/auth/gmail.modify` | Gmail messages and labels (GAM's default) |
   | `https://www.googleapis.com/auth/gmail.settings.basic` | Signatures and auto-replies |
   | `https://www.googleapis.com/auth/gmail.settings.sharing` | Mail delegates and forwarding |
   | `https://www.googleapis.com/auth/drive` | A user's Drive file list |
   | `https://www.googleapis.com/auth/tasks` | Onboarding task lists |

   Offboarding's sign-out needs one more permission that delegation can't give: the admin token's
   "Directory API - User Security" scope, ticked when `gam oauth create` runs. Setup says whether
   your imported token has it.
4. **Check the header.** Once verified, the top left of every page names the connected domain and
   the admin GamGUI acts as. Glance at it before any change. Click it to reconnect, or to switch
   when the Keychain holds more than one domain.

macOS asks for Keychain access the first time each credential is used. **Always Allow** sticks only
if the app is signed with a stable certificate — see
[Stop the Keychain prompts](../README.md#stop-the-keychain-prompts).

## Find a user

**Users** lists everyone, cached for speed ("as of …" says how old the list is; **Refresh**
re-reads it). Search by name, email, title, department or org unit, and open a person for their
profile, groups, delegates, vacation responder, signature and calendar sharing.

![The Users list](screenshots/users.png)

## Onboard a new hire

**Before:** on **Onboard → Role templates**, check the role you'll use — its org unit, signature,
groups, shared calendars and checklist steps. Check the new address isn't already taken (search
Users).

**One person** — **Onboard → Generate**: name, email, role, manager, and whose Google Tasks gets the
setup checklist. Tick **Create the Google account** to create it, and **Also send the welcome
email to the new hire** if you want one. **Preview** lists every step with its `gam` command; the
button under it (**Create account & run onboarding**, or **Create the task list** without an
account) runs exactly that (change the form and you must preview again). The temporary password
appears once, on a sheet you can copy or print — the new hire must change it at first sign-in.

**A CSV of hires** — **Onboard → Bulk import**: download the template CSV, fill one row per hire
(the `role` column picks the template), then **Preview** and confirm. It runs in the background with
live progress. Leave `notify` blank to put each password on the printable sheet, or give a personal
address and Google emails the sign-in details there.

**After:** in the Admin console, check the account exists in the right org unit. Check the groups,
calendars, signature and the checklist in the assignee's Google Tasks. Account creation, task lists,
the welcome email and adding to a group are all *not yet* verified live.

## Offboard a leaver

**Lifecycle** runs the whole routine for one person: reset the password → revoke access and sign
out → turn off forwarding → make the manager a delegate → set the auto-reply → transfer Drive and
calendars to the manager → remove the leaver from everyone's calendars → add a reminder to the
manager's calendar. It does not delete the account; do that later from the user's page, once the
transfer has finished.

![The offboarding preview](screenshots/lifecycle.png)

**Before:** enter the leaver and the manager and click **Preview steps**. Read every warning. GamGUI
refuses to offboard the admin it is connected as — revoking that account's access would cut off
GamGUI mid-run — so connect as a different super admin first if you need to. Several steps, and
the routine as a whole, have not yet run on a live domain, so **work through the
[first live run checklist](domains/lifecycle-offboarding.md#first-live-run-checklist)** with it
open: what to look at in the preview, and what to verify in Google afterwards.

**While it runs:** the calendar sweep can take minutes. Don't quit the app; to halt it, use
**Stop** (see [Jobs](#jobs-and-stop)).

**After:** the panel should say "Offboarding complete — 8 of 8 steps succeeded". If a step failed,
its line says why and later steps that depended on it were not run. Fix the cause, tick the steps
that already succeeded, preview again and run — the checklist covers each failure.

## Roll out a signature

**Signatures** designs one HTML signature with variables such as `{name}` and `{title}`, previews it
for a real person and applies it to a scope: one user, a group, an org unit, a department, a
location or the whole company. The [README](../README.md#email-signatures) lists the variables and
how to host logo images.

![The signature designer](screenshots/signatures.png)

**Before:** test on yourself. The scope starts on a single user — the admin you're connected as,
when that is an active user — so the first apply changes only your own signature. Send yourself an
email and check how it looks, including the images, before widening the scope. Apply writes exactly
the people and template the preview showed; more than 25 people asks you to type the count.

**After:** the run shows a ✓ or ✗ for each person. If some failed, **Retry the N that failed** gives
them their own preview and confirm step — just those people.

## Share a calendar

**Calendars** → **Find a calendar by name**. The first time, click **Build index**: a one-time
background scan, minutes on a large domain. Open the calendar to see **Who has access**, then share
with a person or group at a role and click **Share**.

![Calendars: search and who has access](screenshots/calendars.png)

Sharing also subscribes the person, so the calendar actually appears in their list. Sharing with a
group subscribes each member as a background job; a group of ten or more asks first, naming the
count, and the job lists anyone it couldn't add. To share one person's own calendar, open them on
**Users** → **Sharing**.

**After:** ask one recipient to confirm they see it. Sharing with a group and unsharing are *not
yet* verified live.

## Audit external sharing

The **Builder** runs any of GAM's read commands and shows the result as a table. To look for
sharing outside your organization, run a read that lists sharing — for example **Users → Calendars →
Print calendaracls**, or **Users → Drive → Print drivefileacls** — then tick **External only**. It
keeps the rows that mention an address outside your domains; your primary domain, secondary domains,
aliases and their subdomains all count as inside. It filters every row, not just the first page,
and the CSV download takes the filtered rows.

Clicking an address in a result offers follow-ups for that person (delegates, forwarding,
signature, suspend and more). A Builder change only ever runs from its preview.

## Jobs and Stop

Longer runs — a signature rollout, a bulk department change, a CSV of hires, an offboarding, a group
calendar share, a Builder sequence, the calendar index — run in the background. The **Jobs** button
in the header lists them from any page, with progress, the result and a link back to each one.

**Stop** finishes the person or step in hand, then ends and says how many it didn't attempt. An
offboarding asks first, and marks the steps after it "not run: stopped". Jobs live in memory:
**quitting the app ends them**, so let a job finish or Stop it first.

## Read the audit log

**Audit** lists every change GamGUI has made, newest first: when, what, to whom, and the result.
Filter by action, target or error text, tick **Failures only** to see what went wrong, and **Export
CSV** for a record (the export is the whole log, not the filtered view). Passwords never appear in
it. The log stays on this Mac; Google's own Admin console audit log is separate and worth checking
for the changes that matter.

Reading this screen never runs `gam`.

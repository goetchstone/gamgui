# Incident

**One line:** `/incident` works a compromised account: Check Account reads what an attacker commonly changes and changes nothing, the operator
switches steps and ticks what to undo, and one preview runs it with the person's address typed. Ported from SwiftGamGui, whose
`docs/domains/compromised-account.md` holds GAM's behaviour for each command and the history behind each rule. Treat that runbook's list of
past fixes as a checklist when porting a slice.

**Owns invariants:** #1 (a delegate or send-as address is one plain address, a list of IDs is `id_list`'s), #2 (one held preview, read fresh before the first write).

## Files
- `core/incident/check.py`: the reads in words and `findings`. Pure.
- `core/incident/reads.py`: `read_check`, the reads at once, each failure said beside its list.
- `core/incident/secure.py`: `steps`, in run order, as offboarding's `OffboardStep`, run by `lifecycle.run_offboard`.
- `web/routes/incident.py`; `incident.html`, `_incident_check.html`, `_incident_confirm.html`, `_incident_run.html`.
- `tests/test_incident_check.py`; the mock's `print forward`, `print sendas`, `delete sendas`, `print admins`, the drafts' listing and `delete messages ids`. carol@ is the taken-over account.

## What to know
- **A read that failed is said beside its list, never "none"**: `Read.problem`. Only the account's own read stops the check, since an alias reads as its owner and every other read needs the primary address.
- **Nothing to undo is ticked to start.** The lock is on, except clearing recovery, which is a loss when the values are the person's own.
- **Every step but the picks is a switch.** While the reset runs every other step `requires` it. Otherwise nothing stops anything: a refused sign-out (the admin token's scope) mustn't stop forwarding off (the service account's).
- **What a preview holds runs**, bound to that check's `run`. A pick has to name what the check listed, whatever was posted, and the primary send-as address is never one.
- **Read fresh before the first write**: the same address, suspension and admin flags, not the admin GamGUI signs in as (`cut_off_refusal`), and each scheduled email held still scheduled. One Gmail sent since may be its Sent copy under the same ID, the only copy with the whole Bcc list, so a refused run drops the check: previewing it again would hold the same IDs.
- **The address is always typed** (`guard.enforce(typed_address=...)`), with `confirm` too from ten steps.
- **Scheduled emails go first after the reset**: Gmail sends one at its time with nobody signed in, the lock doesn't stop it, and it isn't a draft. Only one plainly labelled `SCHEDULED` by GAM's own count, and never `SENT`, is deleted as one.
- **Drafts are deleted, not trashed**: a draft in Trash may still be a draft, and sendable. Only the subject is read, since a phishing draft's Bcc runs to hundreds of addresses. The listing and the delete get ten minutes (`MESSAGES_TIMEOUT`).
- **"None" needs GAM's word.** GAM skips a refused token without a word and exits 0 (`handleOAuthTokenError`), so no rows can mean Gmail was never asked. A listing of drafts or scheduled emails is none only on exit 60. Gmail always lists the primary send-as address, so an empty send-as list is `Incomplete`, and then no filters, delegates or forwarding addresses isn't "none" either. An auto-reply read with no `Enabled:` line is unknown, never off.
- **A delete counts what GAM says it deleted.** GAM skips a batch on `serviceNotAvailable` without a word and exits 0, and a scheduled email in it still goes out, so a step short of the count fails.
- **"Already done" belongs to one person**: after a run, what finished is switched off in the next check of that person alone (`incident_done`, added to by each run, so a second run's check doesn't switch the reset back on). Those ticks once carried to the next person typed in SwiftGamGui, whose plan then skipped the reset and the sign-out.
- **The auto-reply's text is the attacker's**, as are subjects and filter words: autoescaped, and `Vacation.from_show_text` reads settings only above `Message:`.

## The logs
- **Each log is read whole, oldest first** (`pull.complete`). One read of the window first: most come back short of the cap. One at the cap is read a day at a time from the window's start, each day read back from its end until it's whole, an event read twice kept once. Bounded: 100 reads a day, 200 a log. Past either, a read that fails, or a day still at its cap, it stops at that day's start and says how far it read, with Read the Rest. The attack is the oldest part, and the newest is the easiest to read again.
- **The Chrome and admin logs' reads for accounts** are low volume and read newest first, both ways at once: Chrome by the profile's account and by GAM's device match, the admin log by what was done to the account and what it did. One read failing fails the log in GAM's words, never a part passed as the whole.
- **A window is fixed once, as times**, when the pull starts: GAM's `-72h` counts from each read and rounds to the minute, and a Gmail log queued behind others would drift past 30 days. Not for the Chrome and admin logs' reads for accounts, which use `-Nh` and `now`. Gmail reads at most 30 days, and the screen says so. Read the Rest on Gmail ends 30 days past where it stopped: moving the start up instead would skip the stretch between and claim the log whole (a fix SwiftGamGui still needs).
- **A row whose address wasn't asked counts toward each account asked**, for whether a read reached its cap: an old name, or no `actor.email`, could be any of theirs. Otherwise 999 + 1 read as whole at a cap of 1000. An extra read, never a missed one (also still to fix in SwiftGamGui).
- **A log stopped before anything of it stood isn't exported** as an empty file, and a part-read one is named `-partial`.
- **A pull by address reads the Devices log's registrations for the accounts asked**: that log records no address, so filtering would drop it whole.
- **One address that isn't a user fails the whole log in GAM**, so the route checks the accounts against the directory first.
- **Parsing runs off the event loop** (`asyncio.to_thread` in `GAMConnector.activity`), and a report's columns are worked out once, not per row (`_Columns`): a pull is up to hundreds of reads of a thousand events each.
- **A log that wasn't read is said, never an empty CSV** that would pass as no events. Export All's `about.txt` says the window, the accounts, the address and how far each log read.
- **Not kept on disk yet:** a pull is held in memory until a kept incident saves it (E4).

## The log reader
`core/incident/reader.py`, ported from SwiftGamGui's `Assist/LogReading.swift` with its tests. Code finds and code recommends; a draft only names a person or a Message-ID for a screen to open.
- **A suspect address is one Google called suspicious, or the one you pulled by**, never one only because many accounts used it. A person's own failed challenge is flagged too, and makes no suspect. Past ten accounts signing in from one, it's set aside as likely shared: securing everyone behind an office's address would lock the office out.
- **Google's mark isn't always the attacker**: a sign-in proved with a passkey is the person, unless the account added a passkey in these logs first, and a re-sign-in from an address they use unmarked is theirs.
- **Google's own warnings have no actor**: the account is `affected_email_address` (failure log 2026-10-22). Where there is an actor, that parameter names someone else.
- **Every Gmail event is `delivery`**, and `event_info.mail_event_type` says what happened. Only 1 and 35 are sends: the rest, from the attacker's address, are the person's own mail the attacker read, and pulling it would take real mail from everyone (failure log 2026-10-23).
- **A device action counts only once `EXECUTED`**: a guessed `SUCCESS` matched nothing Google sends, so a computer an admin blocked stayed on the list (failure log 2026-10-22).
- **The lock is read in time order**: a reset and a sign-out since the attacker's first event, by an admin not from a suspect address nor through an account the attacker held, or by Google. A suspension later undone isn't one. A sign-in from a suspect address after a lock starts it over.
- **Linear in the events**: each account's rules read only its own events and the admin's done to it (`_Accounts`), and Who it reached is two passes over the Gmail events. Run off the event loop, once a pull. 200,000 events read in under a second.
- **A difference from Swift:** Python compares strings by code point, so the two forms of an accented address count apart. A warning to one doesn't show the other warned, which over-warns, the safe side.

## Not in this slice
 a kept incident, where each account stands, who it reached, devices and scripts, and the report: back-port plan E2-E8.

## Testing / live-verification status
Offline only here. In SwiftGamGui the lock steps (reset, sign-out, forwarding off, IMAP and POP off, clear recovery) are confirmed live. Deleting filters, send-as addresses and drafts, and the scheduled-send clean-up, are not, in either app: check them on a throwaway account before relying on them, and whether Gmail's API honours `in:scheduled` and that a delete stops the send.

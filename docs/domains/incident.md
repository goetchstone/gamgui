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

## Not in this slice
Logs, the log reader, a kept incident, where each account stands, who it reached, devices and scripts, and the report: back-port plan E2-E8.

## Testing / live-verification status
Offline only here. In SwiftGamGui the lock steps (reset, sign-out, forwarding off, IMAP and POP off, clear recovery) are confirmed live. Deleting filters, send-as addresses and drafts, and the scheduled-send clean-up, are not, in either app: check them on a throwaway account before relying on them, and whether Gmail's API honours `in:scheduled` and that a delete stops the send.

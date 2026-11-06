# Domain: Phishing

**One line:** take an email out of every mailbox that has it, by its Message-ID, or several at once. Ported from SwiftGamGui, whose `docs/domains/phishing.md` holds GAM's behaviour in full (P1-P12) and each decision (D1-D13): read it before changing this.

**Owns invariants:** #1 (the Message-ID is validated as one value inside the one `query` element), #2 (each mailbox's trash goes through `_run_write`), #3 (the dry run is the one reviewed read with a destructive verb), #9 (lists capped on screen; the feed is the job's window).

## Files
- `gamgui/core/gam/commands.py`: `MessageId`, `find_message_copies`, `trash_message`, `find_spam_copies`, `trash_messages`, `id_list`.
- `gamgui/core/phishing.py`: pure. Reads GAM's lines (`read_copies`, `read_spam_copies`), `Found`, `trash_steps`, recipients, CSV rows.
- `gamgui/core/gam/runner.py`: `run_any_exit`, for a read whose exit code isn't its answer.
- `gamgui/web/routes/phishing.py`, `templates/phishing.html`, `_phishing_*.html`.
- `tests/test_phishing.py`; the mock's "One email out of every mailbox", `messages.csv`, `message_headers/`.

## What to know
- **An `all users` run's exit code is the last user's**, so never 0 across a domain. Read the lines, and refuse a list that didn't come back whole (`Incomplete`): every user GAM listed accounted for, ` (i/n)` covering 1...n once.
- **A mailbox it couldn't search is unknown, never "none"**, and one found nowhere while something went unsearched is unsure. So is one where GAM said a line neither reader knows: it's shown under "GAM also said", never guessed at.
- **GAM overwrites its page counts on one line**: `\rGot 500 Users...\rGot 742 Users...\r\n` (`writeGotMessage`). Both readers end a line at a CR too, so the last count is the count; split on newlines only, every domain past one page of users failed Find. The mock writes a page first.
- **Suspended accounts aren't searched.** `all users` is `isSuspended=False`, so the screen says "active", and one reinstated later may still hold a copy.
- **Find shows what the ID matched**: one copy's sender, subject and date, from the first mailbox with a copy, repeated on the confirm step, and two different emails are said. A failure to read it blocks nothing. `{`, `}` and `|` are refused in an ID, since the sender writes it and Gmail's search may read them as syntax. `MessageId` checks itself however it's built.
- **One step per mailbox, not `all users ... doit`.** The preview names who, the audit has a record each, Stop works between them, and an account-wide failure stops the rest (`bulk.stop_reason`).
- **`notquick max_to_trash <copies found>`**: without `notquick` a cap quietly leaves copies, and `0` is no limit. A mailbox holding more by then fails with GAM's words.
- **Spam is by ID.** `trash messages query` never lists Spam, so `find_spam_copies` reads it and `trash_messages` names exactly those copies. A failed Spam read is said and doesn't block the rest.
- **The number of steps is always typed** (`typed_count_above=0`), with `confirm` too from ten.
- **Recipients never claim to be everyone**, and a piece naming anything but one address is shown under "check by hand", never cut down. Tenant domains come from `AppState.tenant_domains`, which falls back to the primary if `print domains` fails, so a domain alias then reads as outside: the safe way round.
- **After a run the search is dropped**: its counts are from before it. A preview is bound to its Find's `run`, not `id(found)`, which Python reuses as soon as the old one is freed.

## Testing / live-verification status
Offline only here. Trash by query is argv-identical to SwiftGamGui's live runs (2026-10-19, 2026-10-23); trash in Spam hasn't run live in either app.

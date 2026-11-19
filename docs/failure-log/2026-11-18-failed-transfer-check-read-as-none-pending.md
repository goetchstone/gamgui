# 2026-11-18 — A failed data-transfer check read as "no transfer running" before a delete

- **Symptom:** found by the swallowed-read scan (plan item C), never seen live. When `print datatransfers olduser
  <email>` failed (an expired token, a refused scope, a timeout) or exited without its table, the Users delete zone and
  the Builder's delete previews showed no transfer warning, as if none were running. An operator deleting a leaver
  mid-transfer would lose whatever Drive or Calendar data hadn't moved yet, with nothing on screen to stop them.
- **Cause:** the connector's `incomplete_transfers_for` caught every exception and returned `[]`, and an empty stdout
  parsed to `[]` too; both callers read `[]` as "nothing pending". Its docstring made it deliberate ("never blocks
  deletion on an inability to check — just can't warn"), so the unknown answer was never shown as one.
- **Why not caught:** the mock had only two answers for this read, a pending transfer and none; no test ran the delete
  screens on its failing or partial exit. Invariant 10 landed after this code (b26da7e) and nothing re-read old reads
  against it.
- **Fix:** the read returns `None` on any failure or a table-less exit (logged), and both screens say "Couldn't check for
  a data transfer … check Data transfers in the Admin console first". Still a warning, not a block. GAM prints the CSV
  header even with no transfers (`writeCSVfile` always writes, `doPrintShowDataTransfers` starts with fixed titles: read
  statically from the 7.52.03 build), so an empty stdout is no answer, not "none".
- **Prevention:** mock triggers `*xferfail*` (an error exit) and `*xfernotable*` (exit 0, no output);
  `test_a_failed_transfer_read_is_unknown_not_none_pending`,
  `test_a_transfer_check_that_failed_is_said_never_read_as_none_pending`,
  `test_builder_delete_says_a_transfer_check_that_failed`. The general tripwire is `tests/test_swallowed_reads.py`
  (plan item C), which lists every silent broad catch in `core/` and `web/routes/`.

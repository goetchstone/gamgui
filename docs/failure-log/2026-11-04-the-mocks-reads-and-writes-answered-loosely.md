# 2026-11-04 — The mock's reads and writes answered more loosely than GAM

- **Symptom:** found by SwiftGamGui reading GAM's source (its failure log: 2026-10-12 "duplicate create", 2026-10-13
  "calendar subscribe two mocks" and "offboarding mock lies", 2026-10-19 "mock Gmail settings answered anyone"), and in
  this repo's A1 for the reads. Never seen live. Reproduced on `main`:
  - Most `formatjson` reads printed NDJSON, which GAM never does, and a rule's JSON held key columns GAM keeps beside it.
  - `create user` for a taken address printed a 409 GAM never prints. A taken directory address, alias or group passed.
  - `add calendars` printed a line GAM never prints, for anyone and any calendar.
  - A transfer to the leaver's own alias, and to or from any address not named `*missing*`, passed. It printed a line
    GAM never prints, and a second transfer never conflicted.
  - Deprovisioning a suspended user exited 0.
  - `print forwardingaddresses` answered for anyone. `delete delegate` passed for a delegate the person didn't have.
  - `get_event` answered every event for one ID, and group members matched a substring of the address.
- **Cause:** those handlers were written from GAM's conventions in general and for argv shape only, not from the
  functions that print these. Tests also used addresses (`leaver@`, `mgr@`, a hire who was never created) that GAM
  would refuse.
- **Why not caught:** nothing asserted GAM's words, exit codes or columns for these, and a tolerant parser reads NDJSON
  and CSV alike.
- **Fix:** branch `test/mock-lies`. Each read prints GAM's key columns, re-read from GAM's source at 7.50.00:
  `print groups` `email`, group members `group`, cros `deviceId`, resources `resourceId,resourceName`, a user's
  calendars and rules `primaryEmail,calendarId`, a calendar's rules `calendarId`, events `calendarId,id`, Drive
  `Owner,id,name` and the flattened owners. A read shape not modelled is refused. Writes port SwiftGamGui's refusals:
  duplicate (exit 57), a missing org unit, a name over 60 characters, the indented subscribe line, owner resolution
  (exit 56), the same account (usage error), GAM's transfer words, a second transfer (exit 9), a suspended user's
  backup codes (exit 51).
- **What went red:** 26 tests, and none was a product bug.
  - They were fixture addresses GAM would refuse, moved to directory users or `gam_state`, plus one assertion on the
    mock's invented "409".
  - The parser's sibling columns already read the new shape.
  - The offboarding preview already refuses a manager who is the leaver's alias, and warns about a suspended leaver.
  - A duplicate create's message is GAM's words under the "unknown" kind. It is said plainly, so it is left as it is.
- **Prevention:** `tests/test_mock_gam_shapes.py` holds each column set, refusal, word and exit code above.
  `test_every_formatjson_read_has_its_gam_header` makes a new `formatjson` read state its columns.
  - Only a greener mock: nothing here ran on a tenant. Google's own texts ("Not Found", "Duplicate" on a subscribe) and
    Drive's flattened owners columns are approximate.
  - Left as they were, outside A5's list: the mock's single task-list ID, the default calendar sweep's output
    (SwiftGamGui's M8), and `update user`, `delete user` and `signout` taking any address but the `*missing*` trigger.
  - Found on the way, not fixed (not in Phase A's list): GAM exits 60, `NO_ENTITIES_FOUND_RC`, when a read finds
    nothing, as an event search does (read at 7.50.00; the build sets it in 65 places). No GamGUI code handles 60, and
    the runner raises on any non-zero exit, so an empty result should read as a failure on such a read. Offline only:
    not seen live. SwiftGamGui handles 60 read by read, since `print filelist` also ends a refused token with it. The
    mock models it for events only (a `NOMATCH` query). `get_event` likewise raises for a missing event, so the route's
    "no longer exists" branch is not what a live miss reaches; the operator still gets GAM's "Not Found".

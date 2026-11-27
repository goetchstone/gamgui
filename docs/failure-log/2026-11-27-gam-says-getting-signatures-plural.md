# 2026-11-27 — GAM says "Getting Signatures for", plural; the app and its mock said "Signature", so no live signature read would count

- **Symptom:** found before any live run. SwiftGamGui's port of 5.4 cited GAM's `printGettingEntityItemForWhom`, and its
  mock printed "Getting Signatures for <u>". GamGUI's two signature reads required "Getting Signature for", and so did
  its mock:
  - `GAMConnector.signature_exact`, roadmap 4.1's per-person backup, read before every signature write;
  - `signatures_fast.read_signatures`, 5.4's one-call backup.
  Live, every read would have been Incomplete or unread (invariant 10 held: unknown, never "no signature"). So every
  Signatures run, Put Back and fast run would have written no one, each listed as "Couldn't read their current
  signature". It failed closed, but the feature would not have worked at all.
- **Cause:** `printGettingEntityItemForWhom(Ent.SIGNATURE, user, i, count)` writes
  `f'{Msg.GETTING} {Ent.PluralGetting()} {Msg.FOR} {forWhom}'`, and `PluralGetting()` is `_NAMES[SIGNATURE][0]`, which is
  "Signatures" in glentity.py at the 7.52.05 tag. The 4.1 code and the mock handler were written together from the
  function's name ("Getting <Item> for") without reading the entity table, so they agreed with each other and not with
  GAM.
- **Why not caught:** the mock is the only stderr the suite sees. A check written to match the mock's wording can't fail
  in the suite. 4.1's live check is still owed, which would have caught it on the first run.
- **Fix:** `signatures_fast.said_getting` accepts GAM's "Getting Signatures for <u>", with or without "(i/n)", and the
  singular too, as SwiftGamGui's port does. Both reads use it. The mock prints the plural, as do the test fixtures and the
  runbook. The other "Getting … for" checks were read against the same table and already use GAM's plurals: Calendars,
  SendAs Addresses; Forward Enabled is the same word both ways.
- **Prevention:** `tests/test_signature_getting_line.py` feeds GAM's real line to both reads and holds the mock to it
  (`test_the_mock_says_what_gam_says`). The general lesson, for RULE-FEEDBACK: a stderr wording the app keys a decision on
  should be read from GAM's entity and message tables at the pin, never from a function's name. Mock-proven only; 4.1's
  live check is what proves it.

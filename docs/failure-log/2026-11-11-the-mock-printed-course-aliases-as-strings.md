# 2026-11-11 — The mock printed course aliases as strings

- **Symptom:** found in review of the Classroom roster compare (plan 2026-11-11 C3), before it merged; never run live (the
  operator has no Education domain). On a real domain, no Classroom class would have been recognised as GamGUI's: every SIS
  class would have been planned as a new one, beside the class it already had.
- **Cause:** `print courses … aliases formatjson` writes its `JSON-aliases` cell as the Classroom API's items, raw:
  `[{"alias": "d:gg-C1"}]`. When the handler was written from the 7.51.00 build's bytecode, the formatjson branch's
  `json.dumps(list(aliases))` was read as a list of strings; only the non-JSON branch reads `alias['alias']`, which is what
  shows they are objects. The mock was written from that misreading, and `managed_from` read each item as `str(item)`.
- **Why not caught:** the mock and the code agreed, so every test passed: "the mock lies", of the kind CLAUDE.md names. The
  shapes test held only the header (`id,JSON,JSON-aliases`), not the cell's contents.
- **Fix:** `roster_plan._aliases` reads each item's `alias` (a bare string too); any other item makes the class unread, never
  an alias. `tests/fixtures/course_aliases.tsv` holds objects, as GAM prints them.
- **Also found by the same review:** GAM swallows a failed alias read (notFound, serviceNotAvailable, notImplemented) and
  prints `[]` with exit 0, so a GamGUI class can look hand-made. The planner never creates a class when Classroom already has
  one by its name and section without GamGUI's alias, and says so.
- **Prevention:** `test_the_mock_prints_aliases_as_objects_as_the_build_does` holds the cell's contents, not just its header,
  and `test_aliases_are_read_as_gam_prints_them_objects_and_all` holds the parse. Reading a build: follow what each branch
  does to an item, not just what it dumps.

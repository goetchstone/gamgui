# Plan: enforce the lessons already recorded

**Status: NOT STARTED.** Written at GamGUI `ba5a717` (2026-11-18) after a light review, for a reader with no
session context. `CLAUDE.md` is auto-loaded; do not restate it. Mark each item done here, in the PR that closes it.

## Why

The review found the code sound (3193 tests pass, ruff and mypy clean, invariants 1, 2, 3 and 8 hold on a spot
check) and no false claims in the docs. What it found weak is the gap between *recording* a lesson and *enforcing*
it: the ledger asks for tripwires that were never built, and the suite is slow enough (4m21s) to discourage running it.

This plan closes that gap in six small PRs. The design decisions are made here. The work left is mechanical.

## Rules for whoever executes this

These are on top of `CLAUDE.md`, not instead of it.

1. **No live tenant call of any kind.** Not a read, not `scripts/acceptance.py`. Item F only *prepares* a list.
2. **No subagents, no workflows.** Every item is sized for one session working alone. Use `grep` and short AST
   scripts to find sites; read only the function around each hit, never a whole large file
   (`core/incident/reader.py` is 1928 lines).
3. **One item, one branch, one PR.** Branch from fresh `main`, push the branch, open the PR, merge when the required
   checks are green. Never push to `main`. Commit only when the operator has asked for this plan to be carried out.
4. **Do not touch** `core/guard.py`, `web/previews.py`, `_run_write`, `core/secrets/`, `core/setup.py`, the numbered
   invariants in `CLAUDE.md`, or `tests/fixtures/mock_gam.sh`, except where an item names the file. If an item seems
   to need one of these, stop and report.
5. **Never make a test pass by widening it.** An allowlist entry needs a one-line reason that is true. If you cannot
   write the reason, the site is a bug: fix the site.
6. **A real bug found on the way is its own PR**, through the `post-failure` skill (failure-log entry, reproducing
   test, fix). The hook blocks a `fix:` commit without a fresh failure-log entry; that is intended.
7. **Test economically.** While working, run only the test file you are changing. Run the full suite once, before the
   commit. Run `ruff check .` and `mypy` (no arguments, the config sets the scope) before every push.
8. **Stop and report, do not improvise,** when: a count below is off by more than a few, a site does not fit any of the
   categories given, a fix would change what an operator sees on a write screen, or a required check fails for a
   reason outside the PR.

Line numbers drift. Every site below is given as a grep, not a line.

## Order

| # | Item | Size | Commit type |
|---|---|---|---|
| A | A fast test target | small | `chore:` |
| B | Tripwire: exception text in a route's answer | medium | `test:` + `refactor:` |
| C | Ratchet: a broad catch that swallows a read | medium | `test:` |
| D | Pre-commit skill: the two unwritten checks | small | `docs:` |
| E | `improve-rules` pass | small | `docs:` |
| F | Live-check worklist (prepare only) | small | `docs:` |

Do A first: it makes B and C cheaper. B and C are independent of each other. D, E, F are independent of everything.

---

## A. A fast test target

**Goal:** a sub-minute run for use while editing. CI and the pre-commit run stay on the full suite.

**Decision:** a `slow` marker, no new dependency. (Parallel runs would need `pytest-xdist` in the hash-locked
requirements and would have to be proven safe against the shared mock and temp dirs. Not now.)

Steps:

1. `.venv/bin/python -m pytest -q --durations=40 -p no:cacheprovider | tail -60`. Note the tests over about 2s.
2. Mark those `@pytest.mark.slow`. Where a whole file is slow, use a module-level `pytestmark`. Do not mark any
   test in `tests/test_write_routes_guarded.py`, `tests/test_command_contract.py`, `tests/test_catalog.py`,
   `tests/test_vault.py`, `tests/test_ephemeral.py` or `tests/test_setup.py`: those are the security tripwires and
   stay in the fast run however long they take.
3. Register the marker in `pyproject.toml` under `markers`, with a one-line description in the style of the two there.
4. Add to the `Makefile`, beside `test:`, a `test-fast:` target running
   `-m "not a11y and not slow"`. The `-m` on the command line replaces the one in `addopts`, so `not a11y` must be
   repeated. Add the target to `help:`.
5. Do **not** change `addopts`, `make test`, or `.github/workflows/ci.yml`.

**Done when:** `make test-fast` finishes in under 90s and still collects every test in the six files above; the full
run still reports 3193 passed (plus or minus tests added since); CONTRIBUTING.md's test section names the new target
in one line.

---

## B. Tripwire: exception text in a route's answer

**Background:** ledger entry `2026-11-12 — The same CodeQL finding twice in two days` in `docs/RULE-FEEDBACK.md`.
`py/stack-trace-exposure` blocked two PRs. The rule that came out of it (answer in fixed words, never the exception's
own text) lives only in those two fixes. Nothing local checks it. Read that entry and the two fixes it cites first, so
the new test matches what CodeQL actually objects to.

**Where:** about 59 sites in 14 files:

```bash
grep -rnE 'str\((exc|e|err)\)|\{(exc|e|err)\}' gamgui/web/routes
```

By what is caught (approximate): `ValueError` 18, bare `Exception` 12, `store.Problem` 13, `PackageError` 4,
`IncidentProblem` 4, `StoreError` 3, `phishing.Incomplete` 1, `NeedsMap` 1.

**Decision: three categories, and what each must do.**

| Caught | Whose words are in the message | Rule |
|---|---|---|
| A domain error we define (`store.Problem`, `PackageError`, `IncidentProblem`, `StoreError`, `phishing.Incomplete`, `NeedsMap`, and `GAMError`, which is scrubbed) | Ours | May reach the page. Allowed by class. |
| `ValueError` | Ours **only if** everything in the `try` body is our own validator | Per-site allowlist, with a reason naming the validator. Otherwise narrow it (see below). |
| Bare `Exception`, `OSError`, anything else | Python's or the OS's | Its text never reaches a response, a job's `error`, or app state that a template renders. Fixed words on the page; the exception goes to the log. |

Fixing a site in the third category:

- Replace the message with fixed words that say what did not happen and what to do, in the voice of the neighbours.
  Example shape: `"Couldn't read the file. Check it is a CSV saved as UTF-8 and try again."`
- Log the exception with the module's existing logger (find the convention with `grep -rn 'getLogger' gamgui/web`).
  If a module has none, follow the nearest route module that does. Do not invent a new logging helper.
- Do **not** move the text into `error_partial`'s `details` argument. That is still the page.
- Keep the `# noqa: BLE001` comments that explain why a catch is broad; they are documentation here.

Fixing a `ValueError` site that is not purely our validator (for example a `try` that also parses an int, opens a file
or decodes bytes): split the `try` so the foreign call has its own handler with fixed words, and our validator keeps
its message. Do not introduce a new exception hierarchy for this.

**The test.** New file `tests/test_route_error_text.py`, modelled on `tests/test_write_routes_guarded.py`: it
enumerates from the source, so a new site fails until it is classified.

- Parse each `gamgui/web/routes/*.py` with `ast`. For every `ExceptHandler` that binds a name, find uses of that name
  inside the handler as `str(name)`, inside an f-string, or as an argument or keyword value in a call, an assignment
  or a `return`.
- A use passed only to a logger call (`log.*`, `logger.*`, `logging.*`) is fine.
- Otherwise the handler's caught types must all be in `OURS` (the domain classes above, matched by the last dotted
  name), or the site must be in `VALUE_ERRORS_FROM_OUR_VALIDATORS`, a dict keyed `(filename, function name)` whose
  value is the reason.
- Fail with a message that names the file, function and caught type and says which of the three rules applies.
- Add a second test that every key in the allowlist still exists, so a stale entry fails too.
- The docstring opens the way the guarded-routes one does: what went wrong, twice, and what this file holds.

Work in this order so the test is honest: write the test first and watch it fail on all the uncategorised sites; fix
the bare-`Exception` and `OSError` sites; then classify `ValueError` sites one by one.

**Stop and report if:** fixing a site would remove information an operator needs to act on a failed *write* (those go
through `write_failed`, which already separates remediation from GAM's raw detail; leave it alone), or the allowlist
would exceed about 20 entries.

**Done when:** the new test passes with no bare `Exception` or `OSError` site allowed; every allowlist entry has a
reason; `docs/domains/web-server-security.md` gains a short paragraph stating the rule and naming the test; the
2026-11-12 ledger entry gets a `- **Resolved (<date>):**` line naming the test, in the form the older entries use.

---

## C. Ratchet: a broad catch that swallows a read

**Background:** invariant 10. A handler that catches everything and returns `[]`, `None`, `{}` or `False` turns a
failed read into "none". There are about 145 broad catches; most are fine (they report the failure). The dangerous
ones are the few that say nothing.

**Decision:** find them by script, read only those, and pin the result with a ratchet test so the number can only go
down.

Steps:

1. New file `tests/test_swallowed_reads.py`. With `ast`, over `gamgui/core/**/*.py` and `gamgui/web/routes/*.py`
   (skip `gamgui/resources/`), collect every handler that catches `Exception`, `BaseException` or nothing, and whose
   body does **none** of: `raise`; reference the bound exception name; call a logger; set an attribute or key whose
   name contains `error`, `problem`, `unknown` or `failed`. Those are the silent ones.
2. Expect roughly 8 in `core/` and an unknown, probably similar, number in `web/routes/`. If the script finds more
   than 30, the filter is too loose: tighten it before reading anything, and say how in the PR.
3. For each silent handler, read the enclosing function and its callers (`grep -rn 'function_name('`), and put it in
   one of two lists in the test file, keyed `(filename, function name)`:
   - `HARMLESS`, with a reason: the value is cosmetic or best-effort and nothing decides on it (a label, a cache
     warm-up, a cleanup at exit).
   - Not listable, because a caller branches on the result, shows it as a count or a list, or uses it to plan a write
     or a delete. That is an invariant 10 bug. Do not fix it in this PR. Open it as its own PR through `post-failure`,
     with a test on the failing exit modelled on `test_unknown_owners_with_no_table_never_read_as_owning_none`. Until
     that PR lands, list the site in a third dict, `KNOWN_BUGS`, with the failure-log filename as its value.
4. The test fails on any silent handler in none of the lists, and on any list entry that no longer exists.

**Stop and report if:** a bug found here is in a path that plans a delete, a removal or an offboarding step. Report
it to the operator before doing anything else; that one jumps the queue.

**Done when:** the test passes; `KNOWN_BUGS` is empty or each entry has an open PR; the PR description gives the
three counts (silent, harmless, bugs).

---

## D. Pre-commit skill: the two unwritten checks

Two ledger entries name the `pre-commit` skill as their enforcement home. Check whether each is already there
(`grep -n -i 'alias\|failing exit\|partial' .claude/skills/pre-commit/SKILL.md`) and add what is missing, one
checklist line each, in the file's existing voice:

1. From `2026-11-14 — "Not under this address" read as "no account"`: a compare keyed on addresses treats "not under
   this address" as unknown until every address an account answers to (primary and aliases) has been checked.
2. From the 2026-11-14 improve-rules pass: a read whose answer drives a decision has a test on its failing exit and
   its partial exit, not only its clean one, **in the PR that adds the read**.

If `.claude/hooks/pre-commit-check.sh` selects checklist items by changed path, wire both to the paths that matter
(`core/roster.py`, `core/classroom*`, `core/lifecycle.py`, `core/incident/`). The hooks are local and gitignored: say
in the PR description that the hook half is not in the diff.

Skill text only. No edit to `CLAUDE.md`.

**Done when:** both lines are in the skill, and each ledger entry has a `Resolved` line.

---

## E. `improve-rules` pass

Run the `improve-rules` skill exactly as written. It reads the evidence since 2026-11-14 and proposes **one**
`CLAUDE.md` edit as a PR.

The overdue candidate is the 2026-10-02 entry: invariant 3 calls the 26 curated commands "the only ones that can
change anything", but a promoted read's export to a Sheet also writes. Two passes have named it "next" and moved on.
Unless the evidence in the window clearly outranks it, propose that wording fix, with the tripwire case the entry
asks for.

The operator reviews the wording before merge. Do not enable auto-merge on this PR.

**Done when:** the PR is open and the ledger entry it resolves is marked.

---

## F. Live-check worklist (prepare only)

No live call. The deliverable is a table the operator can work through with the `live-verify` skill.

1. From the README's "Live verification status" section, list every row not marked confirmed, plus the 32 reads
   changed in #132.
2. For each, give: the exact `gam` argv (from the `GAMCommands` builder, not from memory), read or write, whether it
   can run on a throwaway user, event or calendar, and what a pass looks like.
3. Order: reads first, then writes that a throwaway can prove, then writes that cannot be proven safely (state why).
4. Mark Classroom rows "cannot be checked: no Education tenant" and leave them out of the order.
5. Save as `docs/plans/2026-11-18-live-check-worklist.md` with its own Status line.

**Done when:** the worklist is merged. Running it is the operator's call, one action at a time.

---

## Not in this plan

- **Splitting `core/incident/reader.py`.** Do it when the file is next changed for a feature, not as a refactor PR.
- **Sharing the hooks with contributors.** They are local by choice; see `.claude/settings.json`'s comment.
- **Any new feature, any new write.** Roster and Classroom work waits until C and D have landed.

## Status by item

- A — not started
- B — not started
- C — not started
- D — not started
- E — not started
- F — not started

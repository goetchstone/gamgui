# Builder commands — domain reference

The Builder (`/builder`) is GamGUI's catalog of GAM commands — 1,067 of them in the pinned 7.46.11
grammar, split into two tiers by whether they can run:

- **Buildable** (~533): has typed slots and a `build()` returning an injection-safe argv. Two
  sources, and the difference matters:
  - the 26 **hand-curated** commands in `_curated()` (`core/catalog/catalog.py`). argv comes from a
    `GAMCommands` static method; the `RiskLevel` is **authoritative** — hand-set to match the real
    mutation — and the slots are friendly (labels, hints, drag targets).
  - ~507 **auto-promoted reads**. `load_catalog()` calls `_make_reads_buildable()`, which attaches
    the generic grammar-derived builder from `core/catalog/readbuilder.py` to every non-curated
    command that is `RiskLevel.READ_ONLY` *and* not `uncertain`. That gate is the safety property:
    the builder emits no verb of its own, so a promoted command can only ever read.
- **Browse-only** (~534): everything else — every write, and anything the risk inference was unsure
  about (`parser._find_verb` finds no known verb → `uncertain=True` and risk `LOW`, never
  `READ_ONLY`, so it cannot be promoted). Parsed from the vendored grammar
  (`gamgui/resources/gam7/GamCommands.txt`) by `core/catalog/parser.py`, categorized from the `# `
  section headers, with risk *inferred* from the verb. Inert: syntax display and copy only, these
  never run.

## Safety invariants (do not break)

1. **argv only, never a shell.** A command is a `list[str]` handed to
   `asyncio.create_subprocess_exec` — no shell anywhere on the path. Every user/slot value lands as
   exactly one argv element, so `"a@x.com; rm -rf /"` is one harmless argument: never f-string a
   value into an element, never splice one through a shell. A **write**'s argv always comes from a
   `GAMCommands` static method. Literal keywords may come from the vendored grammar (they are not
   user input), but only for `READ_ONLY` commands — see "How reads become buildable".
2. **Only buildable commands run.** Every run path (`/builder/run`, `/builder/sequence/*`) rejects a
   command whose `buildable` is false before touching the connector.
3. **The guard is enforced in the route, not the UI.** A mutation `guard.evaluate` marks
   `requires_confirmation` runs only with a posted `confirmed`; a bulk-destructive sequence needs the
   typed `confirm`. A bare POST that skips the UI must not execute.
4. **Every mutation is audited.** Mutations go through `GAMConnector.apply → _run_write` (serialized +
   audit log). Reads go through `runner.run_authenticated` + `parse_records`.

## How reads become buildable

`core/catalog/readbuilder.py` turns a grammar line into slots + argv, so the whole read surface is
runnable without hand-modeling 500 commands:

1. `_make_reads_buildable(commands)` (`core/catalog/catalog.py`) skips anything already `buildable`,
   anything whose `risk != RiskLevel.READ_ONLY`, and anything `uncertain`. A line that fails to parse
   is left browse-only rather than guessed at.
2. `parse_read_template(raw_syntax)` walks the tokens tracking `[` / `]` depth and returns
   `(slots, template)`. Template parts are `("lit", token)` for a fixed keyword, `("req", key)` /
   `("opt", key)` for a slot value, and `("optpair", key, prefix)` for a prefixed optional.
   - **Slot labels** come from `_humanize` (`<ChatSpace>` → "Chat space"; a trailing `TypeEntity` /
     `Entity` / `List` / `Item` is stripped), repeats disambiguated as "User", "User 2".
   - **Slot kinds** come from `_slot_kind` — substring match on the placeholder: `email` → `EMAIL`,
     `group` → `GROUP`, `user` → `USER`, otherwise `TEXT`. `<UserTypeEntity>` gets `TARGET_USER`, so
     it is a drag target like a curated command's.
   - **Optional `[...]` groups are dropped** — the command runs on its required args only. Two
     exceptions are kept as optional slots: a self-contained `[<UserTypeEntity>]` / `[<CrOSTypeEntity>]`
     (emitted as the `optpair` `user <x>` / `cros <x>`), and a single-token optional positional like
     `info user [<UserItem>]`. A `<UserTypeEntity>` buried in a `[data <…>]` flag group is not touched.
   - **`<UserTypeEntity>` expands to `user <x>`** (`<CrOSTypeEntity>` → `cros <x>`), because that is
     the entity prefix GAM expects; a bare leading `<UserItem>` is treated the same way.
   - Alternations take the first alternative as canonical (`print|show` → `print`); paren-group
     fragments and unrecognized punctuation are skipped rather than guessed.
3. `make_build(template)` returns the `build(values) → argv` closure. Literals are appended as-is;
   each slot value is appended as exactly one element; empty optionals are omitted.

So `gam print addresses [todrive <ToDriveAttribute>*]` becomes zero slots and `["print", "addresses"]`,
and `gam <CrOSTypeEntity>|<UserTypeEntity> list […]` becomes one `TARGET_USER` slot and
`["user", "<value>", "list"]`.

**When to hand-curate a read anyway.** Auto-promotion gives correct but literal slots ("Email
address", "Query"). Curate a read in `_curated()` when it is high-value enough to deserve a friendly
name, clickable query hints (`hints` / `hint_note`), a `CHOICE` slot, or a flag the generic parser
drops as optional — "Search a mailbox" (Gmail-operator chips, `headers all`), "Find users" and "Find
a user's Drive files" are curated for exactly that. Do not curate a read just to make it runnable; it
already is.

## What runs today

The 26 curated commands, by area (several have no other screen in the app, so this is the only way to
reach them). If the count here drifts from `_curated()`, this list is stale:

- **Users — Gmail:** set signature; set / turn off vacation auto-reply; add, remove and list mailbox
  delegates; add a forwarding address; turn forwarding on / off; list forwarding addresses; search a
  mailbox by Gmail query (Message-ID, sender, subject).
- **Users — account:** find users; set title / department; reset password (random); sign out
  everywhere; undelete account; suspend account; delete account.
- **Aliases:** add an alias to a user; remove an alias.
- **Groups:** create a group; add member to group; remove member from group.
- **ChromeOS Devices:** find Chromebooks.
- **Drive:** find a user's Drive files.
- **Data Transfers:** transfer Drive/Calendar ownership.

Everything else that runs is an auto-promoted read (see above).

## Adding a buildable command

This is the curated path — required for any write, optional for a read (which is already runnable).

1. **Verify the syntax** in `gamgui/resources/gam7/GamCommands.txt` (`grep`). Note the entity prefix
   (`gam <UserTypeEntity> …`) and required/optional args. Don't guess — GAM's `remove` vs `delete`
   (and similar) are not interchangeable.
2. **Add the arg-list builder** to `core/gam/commands.py` as a `@staticmethod` returning `list[str]`.
   Validate constrained args by raising `ValueError`, like `add_group_member`'s role check.
3. **Add an arg-shape test** (`tests/test_commands.py`, assert the exact list) and a **contract
   token** — a substring of the command — in `tests/test_command_contract.py`.
4. **Add the `CatalogCommand`** to `_curated()` in `core/catalog/catalog.py`: category/subcategory,
   typed slots (`TARGET_USER`/`USER`/`GROUP` are drag targets; the first becomes the guard's
   `target`), the authoritative `RiskLevel`, and a `build` lambda calling your `GAMCommands` method.
5. **Add a web test** (`tests/test_builder.py`): the preview shows the assembled `gam …`; a
   destructive command requires confirmation; a read renders a table.
6. **Read with a new output shape?** Add a `tests/fixtures/mock_gam.sh` branch so the table renders.

The contract test proves the command's *syntax* exists in the pinned GAM — not that it does what you
expect. Run a destructive command on a throwaway account before trusting it on a real tenant.

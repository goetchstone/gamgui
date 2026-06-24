# Builder commands — domain reference

The Builder (`/builder`) is GamGUI's catalog of GAM commands, in two tiers:

- **Browse-only** (~1,040): every `gam` command, parsed from the vendored grammar
  (`gamgui/resources/gam7/GamCommands.txt`) by `core/catalog/parser.py`, categorized from the `# `
  section headers, with risk *inferred* from the verb. Read/copy only — these never run.
- **Buildable** (the curated overlay in `core/catalog/catalog.py`): the only commands that execute.
  Each has typed slots and a `build()` that returns an injection-safe argv via a `GAMCommands`
  static method, with an **authoritative** `RiskLevel` matching the real mutation.

## Safety invariants (do not break)

1. **argv only, never a shell.** A command is a `list[str]` built in `GAMCommands` and handed to
   `asyncio.create_subprocess_exec`. Slot values are list elements, so `"a@x.com; rm -rf /"` is one
   harmless argument. Never build argv from grammar tokens or f-strings.
2. **Only buildable commands run.** Every run path (`/builder/run`, `/builder/sequence/*`) rejects a
   command whose `buildable` is false before touching the connector.
3. **The guard is enforced in the route, not the UI.** A mutation `guard.evaluate` marks
   `requires_confirmation` runs only with a posted `confirmed`; a bulk-destructive sequence needs the
   typed `confirm`. A bare POST that skips the UI must not execute.
4. **Every mutation is audited.** Mutations go through `GAMConnector.apply → _run_write` (serialized +
   audit log). Reads go through `runner.run_authenticated` + `parse_records`.

## Adding a buildable command

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

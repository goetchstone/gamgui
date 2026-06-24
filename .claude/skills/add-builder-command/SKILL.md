---
name: add-builder-command
description: Add a buildable command to GamGUI's Builder (/builder) — a curated GAM operation with typed slots that runs through the guard. Use when asked to add a command to the builder, expose a GAM operation in the UI, or extend the buildable catalog.
---

# Add a buildable Builder command

Follow `docs/builder-commands.md` and `CONTRIBUTING.md` → Coding standards. KISS; match the
surrounding code so the change is indistinguishable from the rest.

## Steps

1. **Verify syntax** against the pinned grammar — do not guess:
   `grep -niE "<command words>" gamgui/resources/gam7/GamCommands.txt`. Note the entity prefix
   (`gam <UserTypeEntity> …`) and required/optional args; mind `remove` vs `delete` distinctions.
2. **Builder** — add a `@staticmethod` to `gamgui/core/gam/commands.py` returning a `list[str]`.
   Validate enums with `ValueError` (see `add_group_member`).
3. **Builder tests** — exact arg-list assertion in `tests/test_commands.py`; a contract token (a
   substring of the command) in `REQUIRED_TOKENS` (`tests/test_command_contract.py`).
4. **Catalog entry** — add a `CatalogCommand` in `_curated()` (`gamgui/core/catalog/catalog.py`):
   category/subcategory, typed slots (`TARGET_USER`/`USER`/`GROUP` are drag targets; the first is the
   guard's `target`), authoritative `RiskLevel`, and a `build` lambda calling the new method.
5. **Web test** — `tests/test_builder.py`: preview renders the assembled `gam …`; a destructive
   command requires confirm; a read renders a table (add a `tests/fixtures/mock_gam.sh` branch if the
   read's output shape is new).
6. `.venv/bin/python -m pytest -q` green. Regenerate `command_catalog.json` only if the *grammar*
   changed (a curated add does not change it).

## Invariants (never break)

- argv is always a `list` from `GAMCommands` — never shell-spliced, never built from grammar tokens.
- Only `buildable=True` commands run; the guard is enforced in the route, not just the UI.
- Every mutation goes through `apply → _run_write` (audited); reads via `parse_records`.

"""GAM command-contract + version-consistency guards.

These catch GAM *syntax/version drift* — the bug class where a GAM upgrade renames or removes a
sub-command and our builders break only against a live tenant. They need no credentials:

* ``test_required_command_tokens_present`` and ``test_builder_commands_match_a_grammar_line`` call
  every ``GAMCommands`` builder with placeholder values and check what comes out against the vendored
  command reference (``GamCommands.txt``): every keyword is a word in it, and every command's leading
  words match a real ``gam …`` line. Nothing is listed by hand, so a new builder or option is tracked
  the day it lands. They SKIP when the reference isn't vendored (fresh clone / clean-room CI), and RUN
  in the ``gam-compat`` CI job that fetches the real binary — so a renamed/removed command fails the
  build on the next version bump.
* ``test_pinned_version_consistent`` enforces the single source of truth: ``EXPECTED_GAM_VERSION`` must
  match ``scripts/fetch_gam.sh`` (TAG), the mock, and (if vendored) the ``VERSION`` file.
"""

from __future__ import annotations

import functools
import inspect
import itertools
import re
from pathlib import Path

import pytest

from gamgui.core.gam.commands import CALENDAR_ACL_ROLES, EXPECTED_GAM_VERSION, GROUP_ROLES, GAMCommands

ROOT = Path(__file__).resolve().parents[1]
GAM_COMMANDS_REF = ROOT / "gamgui" / "resources" / "gam7" / "GamCommands.txt"
needs_grammar = pytest.mark.skipif(
    not GAM_COMMANDS_REF.exists(),
    reason="vendored GAM command reference not present (clean-room; runs in the gam-compat CI job)",
)

# Arguments a builder validates or branches on: every accepted value is tried, and one that lands in
# the argv is a keyword like any other. A new validated argument fails with ValueError until listed.
ENUM_ARGS = {
    ("add_group_member", "role"): GROUP_ROLES,
    ("add_calendar_acl", "role"): CALENDAR_ACL_ROLES,
    ("add_calendar_acl_cal", "role"): CALENDAR_ACL_ROLES,
    ("set_forward", "action"): GAMCommands.FORWARD_ACTIONS,
    ("search_messages", "detail"): GAMCommands.MESSAGE_DETAIL,
}
# Argv suffixes appended to another command, not commands: keywords checked, leading words not.
SUFFIXES = {"todrive_args"}
# The argv prefixes that name who a command acts on; the command's own words follow them.
ENTITY_PREFIXES = (("user", None), ("all", "users"), ("calendars", None), ("update", "group", None))


def _value(tok: str) -> bool:
    return "<" in tok   # a `<param>` placeholder this test passed in, alone or joined into a list


def builder_argvs():
    """Every argv each builder can emit: a `<param>` placeholder per value, both sides of each flag,
    each accepted enum, and each optional value both given and omitted."""
    for name, attr in vars(GAMCommands).items():
        if not isinstance(attr, staticmethod):
            continue
        fn, choices = getattr(GAMCommands, name), []
        for p in inspect.signature(fn).parameters.values():
            if p.kind is p.VAR_KEYWORD:
                continue
            if (name, p.name) in ENUM_ARGS:
                vals = list(ENUM_ARGS[name, p.name])
            elif "bool" in str(p.annotation):
                vals = [True, False]
            else:
                vals = [[f"<{p.name}>"]] if "Sequence" in str(p.annotation) else [f"<{p.name}>"]
                if p.default is not p.empty and not p.default:
                    vals.append(p.default)   # omitted: exercises the builder's default branch
            choices.append([(p.name, v) for v in vals])
        for combo in itertools.product(*choices):
            yield name, fn(**dict(combo))


@functools.lru_cache(maxsize=None)
def _grammar():
    text = GAM_COMMANDS_REF.read_text(errors="replace")
    words = set(re.findall(r"[\w-]+", text))
    heads = [line.split()[1:] for line in text.splitlines() if line.startswith("gam ")]
    # `<Name> ::= a|b|<Other>` definitions that are only a choice of words, resolved to those words
    # (`<Boolean>` → true|on|…|false|off|…), so `vacation on` can match `vacation [<Boolean>]`.
    rhs, name = {}, None
    for line in text.splitlines():
        m = re.match(r"(<[^>]+>)\s*:*=+(.*)", line)   # the grammar also spells it `=`, `::=`, `:==`
        if m:
            name = m.group(1)
            rhs.setdefault(name, []).append(m.group(2).strip())
        elif name and line[:1].isspace() and line.strip():
            rhs[name][-1] += line.strip()
        else:
            name = None

    def choices(n, seen=()):
        out, seen = set(), (*seen, n)
        for item in "|".join(rhs.get(n, ())).split("|"):
            if re.fullmatch(r"[\w-]+", item):
                out.add(item)
            elif not (re.fullmatch(r"<[^<>]+>", item) and item not in seen and (sub := choices(item, seen))):
                return None
            else:
                out |= sub
        return out or None

    enums = {n: c for n in rhs if (c := choices(n))}
    return words, {w.lower() for w in words}, heads, enums


def _head_accepts(head, argv, enums) -> int:
    """How many leading argv elements a grammar line accepts, reading its tokens up to the first
    multi-token clause (`[todrive …]`, `(…)*`) or the first mismatch."""
    i = 0
    for tok in head:
        opt = tok[:1] == "[" and tok[-1:] == "]"
        body = tok[1:-1] if opt else tok
        if re.search(r"[][()*+{}]", body) or i >= len(argv):
            return i
        if body == "<UserTypeEntity>":
            n = 2 if argv[i:i + 2] == ["all", "users"] or (
                argv[i] == "user" and len(argv) > i + 1 and _value(argv[i + 1])) else 0
        else:
            alts = body.split("|")
            if _value(argv[i]):   # a value fills a slot, never a keyword
                n = int(any(a.startswith("<") for a in alts))
            else:                 # a keyword matches a keyword, or a choice-of-words slot
                n = int(any(argv[i] == a or argv[i] in enums.get(a, ()) for a in alts))
        if n:
            i += n
        elif not opt:
            return i
    return i


def _command_words(argv) -> int:
    """How many leading elements name the command: an entity prefix, then the next two elements
    through the last keyword among them (`user <e> print calendaracls`, `update user`, `vacation on`)."""
    start = next((len(p) for p in ENTITY_PREFIXES if len(argv) >= len(p)
                  and all(w == a or (w is None and _value(a)) for w, a in zip(p, argv))), 0)
    return max((j + 1 for j in range(start, min(start + 2, len(argv))) if not _value(argv[j])),
               default=start)


def _matches_grammar(argv) -> bool:
    _, _, heads, enums = _grammar()
    return any(_head_accepts(h, argv, enums) >= _command_words(argv) for h in heads)


@needs_grammar
def test_required_command_tokens_present():
    # Every keyword any builder can emit is a whole word in the grammar (a comma field list: each
    # field, case-insensitively as GAM reads them). Generated, so an option can't go untracked.
    words, lower, _, _ = _grammar()
    seen, missing = set(), set()
    for name, argv in builder_argvs():
        for tok in argv:
            if not tok or _value(tok) or tok.isdigit():
                continue
            seen.add(tok)
            if not (all(f.lower() in lower for f in tok.split(",")) if "," in tok else tok in words):
                missing.add(f"{name}: {tok}")
    assert {"doit", "eventid", "returnidonly", "notifypassword", "sendupdates"} <= seen
    assert not missing, (
        f"GAM {EXPECTED_GAM_VERSION} command reference lacks keywords our builders emit: "
        f"{sorted(missing)}. A GAM upgrade likely renamed/removed an option — fix commands.py (and the "
        "mock's handler), and re-run the live acceptance pass."
    )


@needs_grammar
def test_builder_commands_match_a_grammar_line():
    # The checker must bite: a renamed verb or noun, or a keyword in a slot's place, matches nothing.
    for wrong in (["user", "<e>", "delete", "calendar", "<c>"], ["calendars", "<c>", "remove", "events"],
                  ["user", "<e>", "vacation", "maybe"], ["update", "group", "<g>", "insert", "<m>"],
                  ["create", "users", "<e>"], ["user", "<e>", "zzz", "delegate", "<d>"]):
        assert not _matches_grammar(wrong), wrong
    unmatched = {f"{name}: {' '.join(argv[:_command_words(argv)])}"
                 for name, argv in builder_argvs() if name not in SUFFIXES and not _matches_grammar(argv)}
    assert not unmatched, (
        f"no `gam …` line in the GAM {EXPECTED_GAM_VERSION} reference starts like these builder "
        f"commands: {sorted(unmatched)}. A GAM upgrade likely renamed/moved a sub-command."
    )


@needs_grammar
def test_catalog_matches_grammar():
    # The committed command catalog (Builder data) must be regenerated when GAM is bumped:
    # its version + command count must equal a fresh parse of the vendored grammar.
    import json

    from gamgui.core.catalog.parser import parse_grammar

    cat_json = ROOT / "gamgui" / "resources" / "gam7" / "command_catalog.json"
    if not cat_json.exists():
        pytest.skip("command_catalog.json not generated")
    data = json.loads(cat_json.read_text())
    fresh = parse_grammar(GAM_COMMANDS_REF.read_text(errors="replace"))
    assert data["version"] == EXPECTED_GAM_VERSION, "regenerate command_catalog.json (scripts/build_command_catalog.py)"
    assert len(data["commands"]) == len(fresh), "command_catalog.json is stale — regenerate it after the GAM bump"


def test_calendar_acl_roles_match_grammar_and_mock():
    # The builders validate against CALENDAR_ACL_ROLES; the strict mock must reject exactly the same
    # set, and (when vendored) the grammar's every <CalendarACLRole> definition must equal it.
    import re

    from gamgui.core.gam.commands import CALENDAR_ACL_ROLES

    mock = (ROOT / "tests" / "fixtures" / "mock_gam.sh").read_text()
    assert f'ACL_ROLES="{"|".join(CALENDAR_ACL_ROLES)}"' in mock
    if GAM_COMMANDS_REF.exists():
        defs = re.findall(r"<CalendarACLRole> ::=\s*(\S+)", GAM_COMMANDS_REF.read_text(errors="replace"))
        assert defs and {tuple(d.split("|")) for d in defs} == {CALENDAR_ACL_ROLES}


def test_pinned_version_consistent():
    # Committed sources of the pin must agree. (The vendored VERSION file is checked in the gam-compat
    # CI step instead — after a real fetch — since locally it may be a placeholder.)
    fetch = (ROOT / "scripts" / "fetch_gam.sh").read_text()
    assert f'TAG="v{EXPECTED_GAM_VERSION}"' in fetch, "scripts/fetch_gam.sh TAG must match EXPECTED_GAM_VERSION"

    mock = (ROOT / "tests" / "fixtures" / "mock_gam.sh").read_text()
    assert EXPECTED_GAM_VERSION in mock, "mock_gam.sh must echo EXPECTED_GAM_VERSION"


# The connector functions that may record() or run a write outside `_run_write`, each with its reason.
AUDITED_OUTSIDE_RUN_WRITE = {
    "create_onboarding_runbook": "a two-step tasklist build; records `onboard_runbook` itself",
}
# Reads that record() — never the output (plan S9, operator decision D3).
AUDITED_READS = {
    "catalog_read": "records `sensitive_read` for a SENSITIVE_READS command",
}
# Functions that run an argv the tripwire can't trace to one builder, each proven a read another way.
READ_BY_CONTRACT = {
    "catalog_read": "refuses a non-READ_ONLY catalog command "
                    "(test_builder.py::test_catalog_read_and_export_refuse_a_write_command)",
}


def test_audit_record_only_in_run_write_or_allowlist():
    """Invariant #2: every ``self.audit.record(`` in the connector is inside ``_run_write`` or a small,
    named allowlist of documented non-chokepoint audited paths. A NEW audited write path outside them
    fails here — surfacing a mutation that skipped ChangePreview -> guard.evaluate."""
    import ast
    src = (ROOT / "gamgui" / "core" / "connectors" / "gam_connector.py").read_text()
    allow = {"_run_write", *AUDITED_OUTSIDE_RUN_WRITE, *AUDITED_READS}
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            seg = ast.get_source_segment(src, node) or ""
            if "self.audit.record(" in seg and node.name not in allow:
                offenders.append(node.name)
    assert not offenders, (
        "audit.record() outside _run_write / the allowlist — a mutation may be skipping the "
        "ChangePreview->guard chokepoint (invariant #2): {}".format(sorted(set(offenders))))


def _own_calls(fn):
    """The calls in ``fn``'s own body — a nested def/lambda is its own scope, counted on its own."""
    import ast
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _builder_of(node, fn):
    """The ``GAMCommands`` builder an argv expression comes from: a direct call, or a local assigned
    exactly once from one. None when it can't be traced."""
    import ast
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "GAMCommands"):
        return node.func.attr
    if isinstance(node, ast.Name):
        values = [n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == node.id for t in n.targets)]
        if len(values) == 1:
            return _builder_of(values[0], fn)
    return None


def test_every_run_authenticated_call_is_the_chokepoint_or_a_read():
    """Invariant #2 (plan Q6): ``runner.run_authenticated`` is how every credentialed GAM call runs, so
    each call site must be one of: ``_run_write``; a named, hand-audited path; a function proven a read
    another way (``READ_BY_CONTRACT``); or a read — its argv traced to a builder classified as a read in
    ``tests/test_mock_gam.py`` and not taking the write lock. The audit.record() tripwire above can't
    see a write that is simply never audited (the Builder's todrive export, reset_password's follow-up
    sign-out); this one can."""
    import ast

    from .test_mock_gam import READS

    allowed = {"_run_write", *AUDITED_OUTSIDE_RUN_WRITE, *READ_BY_CONTRACT}
    offenders, seen, expected = [], 0, 0
    for path in sorted((ROOT / "gamgui").rglob("*.py")):
        src = path.read_text()
        expected += src.count(".run_authenticated(")
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for call in _own_calls(fn):
                if not (isinstance(call.func, ast.Attribute) and call.func.attr == "run_authenticated"):
                    continue
                seen += 1
                if fn.name in allowed:
                    continue
                argv = call.args[1] if len(call.args) > 1 else next(
                    (k.value for k in call.keywords if k.arg == "argv"), None)
                builder = _builder_of(argv, fn)
                serialized = any(k.arg == "serialize" and not (isinstance(k.value, ast.Constant)
                                                               and k.value.value is False)
                                 for k in call.keywords)
                if builder not in READS or serialized:
                    offenders.append(f"{path.relative_to(ROOT)}::{fn.name} ({builder or 'untraced argv'})")
    assert seen == expected, "a run_authenticated( call outside any function body escaped the scan"
    assert not offenders, (
        "run_authenticated() with a write, an untraceable argv or the write lock, outside _run_write and "
        "the named allowlists — route the mutation through _run_write (invariant #2): {}".format(offenders))

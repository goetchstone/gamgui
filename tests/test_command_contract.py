"""GAM command-contract + version-consistency guards.

These catch GAM *syntax/version drift* — the bug class where a GAM upgrade renames or removes a
sub-command and our builders break only against a live tenant. They need no credentials:

* ``test_required_command_tokens_present`` asserts every GAM token our ``GAMCommands`` builders rely on
  still exists in the vendored command reference (``GamCommands.txt``). It SKIPS when the reference
  isn't vendored (fresh clone / clean-room CI), and RUNS in the ``gam-compat`` CI job that fetches the
  real binary — so a renamed/removed command fails the build on the next version bump.
* ``test_pinned_version_consistent`` enforces the single source of truth: ``EXPECTED_GAM_VERSION`` must
  match ``scripts/fetch_gam.sh`` (TAG), the mock, and (if vendored) the ``VERSION`` file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gamgui.core.gam.commands import EXPECTED_GAM_VERSION

ROOT = Path(__file__).resolve().parents[1]
GAM_COMMANDS_REF = ROOT / "gamgui" / "resources" / "gam7" / "GamCommands.txt"

# GAM sub-command tokens our GAMCommands builders depend on today. Add the new token alongside any new
# builder (e.g. add "print cros" when the Chromebook feature lands) so the contract tracks the code.
REQUIRED_TOKENS = [
    "print users",
    "info user",
    "update user",
    "organization",   # update user organization (title/department/store)
    "suspended",      # update user ... suspended on|off
    "signature",
    "delegate",
    "vacation",
    "forwardingaddress",  # gmail forwarding (Builder)
    "print messages",     # mailbox search (Builder: find a message, dump headers)
    "print cros",         # ChromeOS device search (Builder: Find Chromebooks)
    "print filelist",     # Drive file search (Builder: Find a user's Drive files)
    "alias",              # user aliases (Builder)
    "todrive",            # export a print command's CSV to a Google Sheet
    "tduser",             # …in a specific user's Drive
    "print groups",
    "create|add group",  # create a Google Group (grammar reads `create|add group`, not `create group`)
    "update group",   # add/remove members
    "calendaracls",   # calendar access view/add/remove
    "print resources",  # resource/room calendars
    "print calendars",  # a user's calendars
    "add calendars",    # subscribe a recipient so a shared calendar appears in their sidebar
    "print events",     # event search
    "delete events",    # event deletion
    "remove calendars", # PERMANENT secondary-calendar delete (NOT `delete calendars` = unsubscribe)
    "datatransfer",     # offboarding data transfer
    "add event",        # offboarding manager reminder
    "delete user",      # offboarding final delete
    "signout",          # end all of a user's active sessions (Builder + user-detail action)
    "undelete user",    # restore a recently deleted account (Builder)
    "create|add user",  # onboarding can create the new hire's account (grammar reads `create|add user`)
    "changepassword",   # created accounts force a reset at first login (changepassword on)
    "create tasklist",  # onboarding runbook -> Google Tasks list
    "create task",      # onboarding runbook -> a task on the list
    "sendemail",        # onboarding welcome email
    "report users",   # usage report
    "serviceaccount", # check serviceaccount (setup verify)
    "formatjson",     # JSON output mode we parse
]


@pytest.mark.skipif(
    not GAM_COMMANDS_REF.exists(),
    reason="vendored GAM command reference not present (clean-room; runs in the gam-compat CI job)",
)
def test_required_command_tokens_present():
    ref = GAM_COMMANDS_REF.read_text(errors="replace")
    missing = [t for t in REQUIRED_TOKENS if t not in ref]
    assert not missing, (
        f"GAM {EXPECTED_GAM_VERSION} command reference is missing tokens our builders rely on: "
        f"{missing}. A GAM upgrade likely renamed/removed a sub-command — update commands.py + this "
        "list together, and re-run the live acceptance pass."
    )


@pytest.mark.skipif(
    not GAM_COMMANDS_REF.exists(),
    reason="vendored GAM command reference not present (clean-room; runs in the gam-compat CI job)",
)
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

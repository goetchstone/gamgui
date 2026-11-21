"""Tripwire: no route answers with the text of an exception whose words aren't ours.

CodeQL's ``py/stack-trace-exposure`` blocked two PRs in two days: the owned-classes CSV (1214420) and the handover
ZIP (#114) each answered with a caught exception's own text. The rule both fixes made, answer in fixed words and
never in the exception's, lived only in their commit messages, and CodeQL runs only on the PR. This file enumerates
every ``except ... as name`` in ``gamgui/web/routes`` from the source, so a new one that puts its exception's text
where the operator sees it (a page, a job's ``error``, app state a template draws) fails here until it is classified:

- **A domain error we define** (``OURS``): its words were written for the page, so it may reach a template. Never a
  bare ``Response`` body, which is the shape CodeQL blocked, whatever the class.
- **A ValueError**, only where everything its ``try`` body calls is our own validator: listed in
  ``VALUE_ERRORS_FROM_OUR_VALIDATORS`` with the validator named. Python raises ValueError too (``int("x")``, a JSON
  decode), so a site that also parses, opens or decodes splits its ``try`` instead.
- **Anything else** (``Exception``, ``OSError``, ``RuntimeError``): never. Fixed words on the page; the exception goes
  to the module's logger. GAM's own scrubbed error text reaches a run's details through ``gam_detail``.

Handing the exception to a translator (``friendly``, ``gam_detail``, a route's ``_why``) is not a use of its text: the
last test holds each translator to that with an exception carrying a marker.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest

ROUTES = Path(__file__).parent.parent / "gamgui" / "web" / "routes"

# Domain errors whose message is written for the operator, matched by the last dotted name. GAMError's text is
# GAM's, scrubbed of secrets before the error is built (gamgui/core/gam/errors.py).
OURS = frozenset({
    "Problem", "IncidentProblem", "StoreError", "PackageError", "NeedsMap", "Incomplete", "IncompleteList", "BackupProblem",
    "GAMError", "StoreProblem",
})

# (file, function) -> the validator that raises every ValueError its try body can.
VALUE_ERRORS_FROM_OUR_VALIDATORS: Dict[Tuple[str, str], str] = {
    ("builder.py", "_assemble"): "a GAMCommands builder's argument validation (cmd.build)",
    ("calendars.py", "share"): "GAMCommands.add_calendar_acl_cal's argument validation",
    ("calendars.py", "_grant_and_subscribe"): "GAMCommands.add_calendar_acl_cal's validation, via the connector",
    ("incident.py", "secure_preview"): "core/incident/secure.steps' refusals",
    ("lifecycle.py", "template_save"): "OffboardTemplateStore.save's name and field checks",
    ("lifecycle.py", "template_delete"): "OffboardTemplateStore.delete's checks (its load falls back on any error)",
    ("lifecycle.py", "offboard_preview"): "core/lifecycle.build_offboard_steps' refusals",
    ("onboarding.py", "save_role"): "RunbookStore.set_role's checks",
    ("onboarding.py", "save_welcome"): "RunbookStore.set_welcome's checks",
    ("onboarding.py", "delete_welcome"): "RunbookStore.delete_welcome's checks",
    ("phishing.py", "find"): "core/phishing.parse_message_ids, which names the line it refused",
    ("phishing.py", "recipients"): "MessageId.parse's checks",
    ("roster.py", "map_read"): "RosterMapStore.save's name and column checks",
    ("setup.py", "do_import"): "core/setup.import_dir, written to raise only operator-facing ValueError",
    ("setup.py", "remove"): "core/setup.remove_domain's refusals (no such domain, the active one)",
    ("signatures.py", "save_template"): "SignatureStore.save's checks",
    ("users.py", "phone_preview"): "core/phones (phones_from, plan_phone_change) and GAMCommands.update_phones",
    ("users.py", "phone_apply"): "core/phones.phones_from, via the connector's user_phones",
    ("users.py", "_security_read"): "GAMCommands.filter_address, which refuses what wouldn't stay one search value",
    ("users.py", "calendar_add"): "GAMCommands.add_calendar_acl's argument validation",
}
VALIDATOR_CATCHES = frozenset({"ValueError"})

# Calls that turn an exception into fixed words (or GAM's scrubbed text); the last test holds them to it.
TRANSLATORS = frozenset({"friendly", "gam_detail", "_why", "explain", "in_words", "_details", "unread_why"})
NOT_TEXT_CALLS = frozenset({"isinstance", "issubclass", "type"}) | TRANSLATORS
NOT_TEXT_ATTRS = frozenset({"kind", "exit_code", "remediation", "errno", "__class__"})
LOGGERS = frozenset({"log", "logger", "logging"})
RESPONSES = frozenset({"Response", "PlainTextResponse", "JSONResponse", "HTMLResponse", "StreamingResponse"})


def _caught(node) -> List[str]:
    if node is None:
        return ["BaseException"]
    if isinstance(node, ast.Tuple):
        return [name for elt in node.elts for name in _caught(elt)]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Name):
        return [node.id]
    return [ast.unparse(node)]


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _text_uses(handler: ast.ExceptHandler, parent: dict) -> Iterator[Tuple[int, bool]]:
    """(line, reaches a bare Response) for each place the handler uses its exception as text."""
    for node in ast.walk(handler):
        if not (isinstance(node, ast.Name) and node.id == handler.name and isinstance(node.ctx, ast.Load)):
            continue
        up = parent[node]
        if isinstance(up, ast.Attribute) and up.attr in NOT_TEXT_ATTRS:
            continue
        if isinstance(up, ast.Raise) and up.cause is node:
            continue
        if isinstance(up, ast.Call) and node in up.args and _callee(up) in NOT_TEXT_CALLS:
            continue
        if (isinstance(up, ast.Call) and _callee(up) == "getattr" and up.args[0] is node
                and isinstance(up.args[1], ast.Constant) and up.args[1].value in NOT_TEXT_ATTRS):
            continue
        logged, to_response, at = False, False, up
        while at is not handler:
            if isinstance(at, ast.Call):
                func = at.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in LOGGERS:
                    logged = True
                to_response = to_response or _callee(at) in RESPONSES
            at = parent[at]
        if not logged:
            yield node.lineno, to_response


def _handlers() -> Iterator[Tuple[str, str, List[str], List[Tuple[int, bool]]]]:
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for handler in ast.walk(tree):
            if not (isinstance(handler, ast.ExceptHandler) and handler.name):
                continue
            func = parent[handler]
            while not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                func = parent[func]
            uses = list(_text_uses(handler, parent))
            if uses:
                yield path.name, getattr(func, "name", "<module>"), _caught(handler.type), uses


def test_no_route_answers_with_an_exception_s_own_text():
    wrong = []
    for file, function, caught, uses in _handlers():
        lines = ", ".join(str(line) for line, _ in uses)
        where = f"{file}:{lines} {function}() catching {', '.join(caught)}"
        if any(to_response for _, to_response in uses):
            wrong.append(f"{where}: a bare Response built from an exception's text (what CodeQL blocks). Answer "
                         "in fixed words, whatever the class.")
        elif all(name in OURS for name in caught):
            continue
        elif (file, function) in VALUE_ERRORS_FROM_OUR_VALIDATORS and set(caught) <= VALIDATOR_CATCHES:
            continue
        elif set(caught) <= VALIDATOR_CATCHES:
            wrong.append(f"{where}: a ValueError's text reaches the page. If every call in its try body is our own "
                         "validator, list it in VALUE_ERRORS_FROM_OUR_VALIDATORS naming the validator; otherwise split "
                         "the try so the foreign call answers in fixed words.")
        else:
            wrong.append(f"{where}: this text is Python's or the OS's, not ours. Say what didn't happen in fixed words "
                         "and log the exception (logger.warning(..., exc_info=exc)); GAM's own text goes through "
                         "gam_detail.")
    assert not wrong, "Exception text reaching the operator:\n" + "\n".join(wrong)


def test_every_allowlisted_validator_site_still_exists():
    sites = {(file, function) for file, function, caught, _ in _handlers() if "ValueError" in caught}
    stale = sorted(set(VALUE_ERRORS_FROM_OUR_VALIDATORS) - sites)
    assert not stale, f"Allowlisted sites that no longer show a ValueError's text (remove them): {stale}"


def test_the_tripwire_bites():
    source = (
        "async def route(request):\n"
        "    try:\n"
        "        open('x')\n"
        "    except OSError as exc:\n"
        "        return error_partial(request, f'Could not: {exc}')\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        "        logger.warning('failed', exc_info=exc)\n"
        "        return error_partial(request, friendly(exc))\n"
    )
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert [list(_text_uses(h, parent)) for h in handlers] == [[(5, False)], []]


def _translators():
    from gamgui.core.incident.reads import in_words
    from gamgui.web.routes import builder, classroom, lifecycle, phishing
    from gamgui.web.routes._common import friendly, gam_detail, unread_why

    return {"friendly": friendly, "gam_detail": gam_detail, "in_words": in_words, "builder._details": builder._details,
            "classroom._why": classroom._why, "lifecycle._why": lifecycle._why, "phishing._why": phishing._why,
            "unread_why": unread_why}


@pytest.mark.parametrize("exc", [Exception("LEAK-MARKER"), OSError(13, "LEAK-MARKER", "/LEAK-MARKER"),
                                 RuntimeError("LEAK-MARKER"), KeyError("LEAK-MARKER")], ids=type)
@pytest.mark.parametrize("name", ["builder._details", "classroom._why", "friendly", "gam_detail", "in_words",
                                  "lifecycle._why", "phishing._why", "unread_why"])
def test_a_translator_never_passes_on_a_foreign_exception_s_text(name, exc):
    assert "LEAK-MARKER" not in _translators()[name](exc)


def test_every_translator_the_scan_trusts_is_checked():
    assert sorted(_translators()) == ["builder._details", "classroom._why", "friendly", "gam_detail", "in_words",
                                      "lifecycle._why", "phishing._why", "unread_why"]
    checked = {name.rsplit(".", 1)[-1] for name in _translators()} | {"explain"}   # roster's explain is classroom's _why
    assert TRANSLATORS == checked

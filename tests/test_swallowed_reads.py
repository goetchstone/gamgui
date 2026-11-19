"""Ratchet: a broad catch that says nothing never turns a failed read into "none" (invariant 10).

A handler that catches everything and returns ``[]``, ``None``, ``{}`` or ``False`` without a word makes a failed read
look like an empty answer. That shape read a leaver as owning no classes, a failed group lookup as a person, and a
failed data-transfer check as "no transfer running" before a delete. Most broad catches are fine: they re-raise, log,
or put their failure where the screen says it. This file finds the ones that do none of that, in ``gamgui/core`` and
``gamgui/web/routes``, and holds each to a list:

- ``HARMLESS``: nothing decides on the value (a label, a type-ahead, a fallback that fails closed), or the value it
  leaves is itself read as unknown downstream. The reason says which.
- ``KNOWN_BUGS``: a caller branches on it, shows it as a count or a list, or plans a write with it. Each names its
  failure-log entry and is fixed in its own PR, which removes it from here.

A new silent catch fails until it is classified, and an entry whose site no longer exists fails too, so the list can
only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, Tuple

ROOT = Path(__file__).parent.parent
SCANNED = [*sorted((ROOT / "gamgui" / "core").rglob("*.py")), *sorted((ROOT / "gamgui" / "web" / "routes").glob("*.py"))]

HARMLESS: Dict[Tuple[str, str], str] = {
    ("core/catalog/catalog.py", "_load_shallow"): "an unreadable shipped catalog falls back to parsing the grammar",
    ("core/catalog/catalog.py", "_make_reads_buildable"): "an unparseable line stays browse-only: fails closed",
    ("core/connectors/gam_connector.py", "resolve"): "the connector interface's lookup; nothing in the app calls it",
    ("core/connectors/gam_connector.py", "_admin_identity"): "returns why it couldn't confirm the admin, never an answer",
    ("core/connectors/gam_connector.py", "create_onboarding_runbook"): "a failed task is added to `failed` and audited",
    ("core/incident/pull.py", "paged"): "a page that failed marks the pull stopped, which the screen says as capped",
    ("core/incident/pull.py", "complete"): "returns the day it stopped at, which the screen says as unread",
    ("core/onboarding.py", "_apply_groups"): "a group that failed is returned in `failed` and reported",
    ("core/onboarding.py", "provision_hire"): "email_sent False adds 'welcome email: failed to send' to the errors",
    ("core/roster.py", "_refuse_a_huge_directory"): "a malformed ZIP tail: zipfile refuses the file next",
    ("core/setup.py", "engine_version"): "a version label; the drift warning says nothing when it's blank, never blocks",
    ("web/routes/_common.py", "admin_address"): "who did it, in what's kept: a label left unsaid",
    ("web/routes/builder.py", "pick"): "type-ahead suggestions; typing still works",
    ("web/routes/groups.py", "_cached_groups"): "only a heading's display name comes from it",
    ("web/routes/groups.py", "people"): "type-ahead suggestions; typing still works",
    ("web/routes/incident_logs.py", "_context_for"): "{} becomes Context.people=None, read as the directory unread",
    ("web/routes/incident_report.py", "_directory"): "None is the directory unread: nothing is said of 2SV",
    ("web/routes/lifecycle.py", "_resolve_name"): "a display name; the address stands in",
    ("web/routes/roster.py", "upload"): "sets a refusal the page shows",
    ("web/routes/roster.py", "_compare"): "None is the directory unread, which the plan says and acts on",
    ("web/routes/signatures.py", "_test_user"): "a blank default choice instead of the admin's own address",
}

KNOWN_BUGS: Dict[Tuple[str, str], str] = {
    ("core/connectors/gam_connector.py", "scan_all_calendars"): "2026-11-18-calendar-index-silently-missing-rooms.md",
    ("web/routes/signatures.py", "_matched"): "2026-11-18-failed-group-scope-read-as-no-one.md",
}

LOGGERS = frozenset({"log", "logger", "logging"})
SAID = ("error", "problem", "unknown", "failed")


def _broad(node) -> bool:
    if node is None:
        return True
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    return any((n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "")) in ("Exception", "BaseException")
               for n in names)


def _says(name: str) -> bool:
    return any(word in name.lower() for word in SAID)


def _silent(handler: ast.ExceptHandler) -> bool:
    """No raise, no use of the exception, no logger call, and nothing named for a failure set."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return False
        if handler.name and isinstance(node, ast.Name) and node.id == handler.name:
            return False
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id in LOGGERS):
            return False
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for part in ast.walk(target):
                    if isinstance(part, ast.Attribute) and _says(part.attr):
                        return False
                    if isinstance(part, ast.Name) and _says(part.id):
                        return False
                    if isinstance(part, ast.Constant) and isinstance(part.value, str) and _says(part.value):
                        return False
        if isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and isinstance(k.value, str)
                                              and _says(k.value) for k in node.keys):
            return False
        if isinstance(node, ast.keyword) and node.arg and _says(node.arg):
            return False
    return True


def _silent_sites() -> Iterator[Tuple[Tuple[str, str], int]]:
    for path in SCANNED:
        tree = ast.parse(path.read_text(), filename=str(path))
        parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for handler in ast.walk(tree):
            if isinstance(handler, ast.ExceptHandler) and _broad(handler.type) and _silent(handler):
                func = parent[handler]
                while not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    func = parent[func]
                where = str(path.relative_to(ROOT / "gamgui"))
                yield (where, getattr(func, "name", "<module>")), handler.lineno


def test_every_silent_broad_catch_is_classified():
    unlisted = sorted(f"{file}:{line} {function}()" for (file, function), line in _silent_sites()
                      if (file, function) not in HARMLESS and (file, function) not in KNOWN_BUGS)
    assert not unlisted, (
        "A broad catch that says nothing: a failed read here reads as none. Re-raise, log, or say it on the screen; "
        "if nothing decides on the value, list it in HARMLESS with the reason. If a caller branches on it, counts it, "
        "lists it or plans a write with it, it is an invariant 10 bug (post-failure skill):\n" + "\n".join(unlisted))


def test_every_listed_site_is_still_silent():
    silent = {key for key, _ in _silent_sites()}
    stale = sorted((set(HARMLESS) | set(KNOWN_BUGS)) - silent)
    assert not stale, f"Listed sites that no longer have a silent broad catch (remove them): {stale}"


def test_a_site_is_never_both_harmless_and_a_bug():
    assert not set(HARMLESS) & set(KNOWN_BUGS)


def test_the_scan_bites():
    tree = ast.parse(
        "def read():\n"
        "    try:\n"
        "        return go()\n"
        "    except Exception:\n"
        "        return []\n"
        "    try:\n"
        "        return go()\n"
        "    except Exception as exc:\n"
        "        logger.warning('failed', exc_info=exc)\n"
        "        return None\n"
        "    try:\n"
        "        return go()\n"
        "    except Exception:\n"
        "        problem = 'unread'\n"
    )
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert [_silent(h) for h in handlers] == [True, False, False]

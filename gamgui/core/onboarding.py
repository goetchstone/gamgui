"""Onboarding runbooks: editable role → task-list templates + a welcome-email template.

The admin defines, in the app, what setup each ROLE needs — mostly manual vendor steps that have no
API and just need to get *done* by someone. Generating a runbook for a new hire turns those steps
into a Google Tasks list on whoever is doing the setup, so the checklist lives in their Gmail/Tasks
(durable, delegatable, survives app restarts) rather than as fragile local state. Only the
*templates* are stored locally, as plain JSON the admin edits.
"""

from __future__ import annotations

import csv
import io
import re
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .paths import app_data_dir

WELCOME_VARS = ["name", "role", "email", "manager"]   # the {tokens} the welcome email understands

# Unambiguous alphabet for a temp password someone reads off a printed sheet and types once:
# no 0/O, 1/l/I. Grouped for legibility. It is single-use — the account is created with
# `changepassword on`, so Google forces a reset at first login.
_PW_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_temp_password(groups: int = 3, size: int = 4) -> str:
    """A strong, human-transcribable one-time password (e.g. ``Xk7m-Qp9r-2Tzv``, ~69 bits)."""
    return "-".join(
        "".join(secrets.choice(_PW_ALPHABET) for _ in range(size)) for _ in range(groups)
    )


def default_store_path() -> Path:
    return app_data_dir() / "onboarding.json"


# Seeded on first run; the admin edits/replaces this entirely — nothing here is hardcoded into logic.
# A role is stored as {"steps": [...], "signature": "<saved template name>", "org_unit": "/Path",
# "groups": ["team@dom"], "calendars": ["cal-id"]}; an older file's bare list of steps (or a dict
# without the newer keys) is migrated on load (see RunbookStore._load / _as_role).
_DEFAULT = {
    "roles": {
        "Salesperson": {
            "steps": [
                "Set up Brite for the employee",
                "Create Wesley Hall login",
                "Set up POS login (salesperson)",
                "Add to the sales group",
            ],
            "signature": "",
            "org_unit": "",
            "groups": [],
            "calendars": [],
        },
    },
    "welcome": {
        "subject": "Welcome to the team, {name}!",
        "body": ("Hi {name},\n\nWelcome aboard as our new {role}. Your account is {email} — "
                 "your manager {manager} will help you get set up.\n\nGlad to have you here."),
    },
}


def _as_str_list(value) -> List[str]:
    """A list of non-blank strings, tolerating a missing key, a bare string, or None."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.splitlines()
    return [str(s).strip() for s in value if str(s).strip()]


def _as_role(value) -> Dict:
    """Normalise a stored role to {steps, signature, org_unit, groups, calendars} — tolerating the old
    list-of-steps form and dicts written before groups/calendars existed."""
    if isinstance(value, list):
        return {"steps": [str(s) for s in value], "signature": "", "org_unit": "", "groups": [], "calendars": []}
    if isinstance(value, dict):
        return {
            "steps": [str(s) for s in value.get("steps", [])],
            "signature": str(value.get("signature", "") or ""),
            "org_unit": str(value.get("org_unit", "") or ""),
            "groups": _as_str_list(value.get("groups")),
            "calendars": _as_str_list(value.get("calendars")),
        }
    return {"steps": [], "signature": "", "org_unit": "", "groups": [], "calendars": []}


@dataclass
class RoleTemplate:
    name: str
    steps: List[str]
    signature: str = ""   # a saved signature-template name to apply to the new hire (blank = none)
    org_unit: str = ""    # the OU the account is created in (blank = domain default "/")
    groups: List[str] = field(default_factory=list)      # group emails the new hire is added to (as member)
    calendars: List[str] = field(default_factory=list)   # calendar ids the new hire is subscribed to


def render(template: str, ctx: Dict[str, str]) -> str:
    """Substitute {name}/{role}/{email}/{manager}; literal-brace-safe (only known vars replaced)."""
    out = template or ""
    for key in WELCOME_VARS:
        out = out.replace("{" + key + "}", str(ctx.get(key, "")))
    return out


# --- bulk import: a CSV of new hires, one row each, onboarded via the role template ---

# Recognised columns (header names are matched case-insensitively; extra columns are ignored). `role`
# picks the template; a blank `notify` means that hire's temp password lands on the printable sheet,
# while a `notify` email hands sign-in delivery to GAM/Google (see connectors.create_user).
HIRE_COLUMNS = ["role", "name", "email", "manager", "assignee",
                "create_account", "first", "last", "send_welcome", "notify"]

HIRE_CSV_TEMPLATE = (
    "role,name,email,manager,assignee,create_account,first,last,send_welcome,notify\n"
    "Salesperson,Jordan Lee,jordan@example.com,mgr@example.com,it@example.com,yes,Jordan,Lee,yes,jordan.personal@gmail.com\n"
    "Salesperson,Sam Rivers,sam@example.com,mgr@example.com,it@example.com,yes,Sam,Rivers,no,\n"
)


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "x", "on"}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(value: str) -> bool:
    """Pragmatic address check: a non-empty local part, one @, a dotted domain. Rejects GAM keyword
    traps (oauthuser, @domain, a bare name) that would target the authorizing admin instead of the
    intended hire when passed to update-group / add-calendars."""
    return bool(_EMAIL_RE.match((value or "").strip()))


def parse_hire_csv(text: str) -> Tuple[List[Dict], List[str]]:
    """Parse an onboarding CSV into row dicts + human-readable errors (``"Row N: …"``).

    A row needs a ``role`` and at least an ``email`` or ``assignee`` to be actionable; fully-blank lines
    are skipped. This is pure/structural — whether the role actually exists is checked by the caller
    (it owns the template store). ``create_account``/``send_welcome`` parse as booleans."""
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames
    except Exception as exc:  # noqa: BLE001 — malformed CSV
        return [], ["Couldn't read the CSV: {}".format(exc)]
    if not fieldnames:
        return [], ["The CSV has no header row."]
    fieldmap = {(fn or "").strip().lower(): fn for fn in fieldnames}
    if "role" not in fieldmap:
        return [], ["The CSV needs a 'role' column — that's what picks the template."]

    def cell(raw: Dict, key: str) -> str:
        return str(raw.get(fieldmap.get(key, ""), "") or "").strip()

    rows: List[Dict] = []
    errors: List[str] = []
    seen: Dict[str, int] = {}   # email (lowercased) -> first row that used it
    for raw in reader:
        i = reader.line_num  # the row's real line number in the file (DictReader silently skips blanks)
        role, name = cell(raw, "role"), cell(raw, "name")
        email, assignee = cell(raw, "email"), cell(raw, "assignee")
        if not any([role, name, email, assignee]):
            continue  # blank line
        if not role:
            errors.append("Row {}: missing role.".format(i)); continue
        if not email and not assignee:
            errors.append("Row {}: needs an email or an assignee.".format(i)); continue
        bad = next(("{} '{}'".format(lbl, v) for lbl, v in (("email", email), ("assignee", assignee))
                    if v and not looks_like_email(v)), None)
        if bad:
            errors.append("Row {}: {} is not a valid email address.".format(i, bad)); continue
        key = email.lower()
        if key and key in seen:
            errors.append("Row {}: duplicate email {} (first on row {}).".format(i, email, seen[key])); continue
        if key:
            seen[key] = i
        rows.append({
            "role": role, "name": name, "email": email, "manager": cell(raw, "manager"),
            "assignee": assignee, "create_account": _truthy(cell(raw, "create_account")),
            "first": cell(raw, "first"), "last": cell(raw, "last"),
            "send_welcome": _truthy(cell(raw, "send_welcome")), "notify": cell(raw, "notify"),
        })
    return rows, errors


class RunbookStore:
    """Plain-JSON persistence for the role templates + welcome email (0600, created on first save)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_store_path()
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                data.setdefault("roles", {})
                data["roles"] = {n: _as_role(v) for n, v in data["roles"].items()}  # migrate old list form
                data.setdefault("welcome", dict(_DEFAULT["welcome"]))
                return data
            except Exception:  # noqa: BLE001 — corrupt/old file: fall back to the seed
                pass
        return json.loads(json.dumps(_DEFAULT))   # deep copy of the seed

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.path.write_text(json.dumps(self._data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # --- roles ---
    def roles(self) -> List[RoleTemplate]:
        out = []
        for n, v in sorted(self._data["roles"].items()):
            r = _as_role(v)
            out.append(RoleTemplate(n, r["steps"], r["signature"], r["org_unit"], r["groups"], r["calendars"]))
        return out

    def role_names(self) -> List[str]:
        return sorted(self._data["roles"].keys())

    def role(self, name: str) -> Optional[RoleTemplate]:
        v = self._data["roles"].get(name)
        if v is None:
            return None
        r = _as_role(v)
        return RoleTemplate(name, r["steps"], r["signature"], r["org_unit"], r["groups"], r["calendars"])

    def steps_for(self, name: str) -> List[str]:
        return _as_role(self._data["roles"].get(name, {}))["steps"]

    def set_role(self, name: str, steps: List[str], signature: str = "", org_unit: str = "",
                 groups: Optional[List[str]] = None, calendars: Optional[List[str]] = None) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("Role name is required.")
        self._data["roles"][name] = {
            "steps": [s.strip() for s in steps if s.strip()],
            "signature": (signature or "").strip(),
            "org_unit": (org_unit or "").strip(),
            "groups": _as_str_list(groups),
            "calendars": _as_str_list(calendars),
        }
        self._save()

    def delete_role(self, name: str) -> None:
        self._data["roles"].pop(name, None)
        self._save()

    # --- welcome email ---
    def welcome(self) -> Dict[str, str]:
        w = self._data.get("welcome", {})
        return {"subject": w.get("subject", ""), "body": w.get("body", "")}

    def set_welcome(self, subject: str, body: str) -> None:
        self._data["welcome"] = {"subject": (subject or "").strip(), "body": body or ""}
        self._save()

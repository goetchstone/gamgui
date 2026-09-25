"""Onboarding runbooks: editable role → task-list templates + a welcome-email template.

The admin defines, in the app, what setup each ROLE needs — mostly manual vendor steps that have no
API and just need to get *done* by someone. Generating a runbook for a new hire turns those steps
into a Google Tasks list on whoever is doing the setup, so the checklist lives in their Gmail/Tasks
(durable, delegatable, survives app restarts) rather than as fragile local state. Only the
*templates* are stored locally, as plain JSON the admin edits.

``provision_hire`` runs one hire end-to-end through the connector's guarded writes — the single
``/run`` and every row of a bulk CSV share it.
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
from typing import Any, Dict, List, Optional, Tuple

from .gam.models import GAMUser
from .paths import app_data_dir
from .signatures import render_signature

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
_DEFAULT: Dict[str, Any] = {
    "roles": {
        "Salesperson": {
            "steps": [
                "Set up the CRM login",
                "Order a laptop",
                "Add to the team channel",
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
    "Salesperson,Jordan Lee,jordan@example.com,mgr@example.com,it@example.com,yes,Jordan,Lee,yes,jordan.personal@example.net\n"
    "Salesperson,Sam Rivers,sam@example.com,mgr@example.com,it@example.com,yes,Sam,Rivers,no,\n"
)


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "x", "on"}


_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def looks_like_email(value: str) -> bool:
    """Pragmatic address check: a non-empty local part, one @, a dotted domain, no comma. Rejects GAM
    keyword traps (oauthuser, @domain, a bare name) that would target the authorizing admin instead of
    the intended hire when passed to update-group / add-calendars, and a comma — GAM splits a
    <UserList> on it, so ``a,b@example.com`` would target two accounts."""
    return bool(_EMAIL_RE.match((value or "").strip()))


def parse_hire_csv(text: str) -> Tuple[List[Dict], List[str]]:
    """Parse an onboarding CSV into row dicts + human-readable errors (``"Row N: …"``).

    A row needs a ``role`` and at least an ``email`` or ``assignee`` to be actionable; fully-blank lines
    are skipped. This is pure/structural — whether the role actually exists is checked by the caller
    (it owns the template store). ``create_account``/``send_welcome`` parse as booleans."""
    reader = csv.DictReader(io.StringIO(text))
    try:
        fieldnames = reader.fieldnames
        # Read every record up front, inside the try: csv.Error (e.g. a cell over the module's 131,072-char
        # field limit) escaped the old loop and 500'd the preview. The reader can't resume past it, so the
        # whole file is refused rather than half-imported. line_num still points at the previous line then.
        records = [(reader.line_num, raw) for raw in reader]
    except csv.Error as exc:
        return [], ["Row {}: couldn't read the CSV ({}). Nothing was imported — fix the file and upload "
                    "it again.".format(reader.line_num + 1, exc)]
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
    for i, raw in records:  # i = the row's real line number in the file (DictReader silently skips blanks)
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


# --- provisioning: one hire's onboarding, shared by the single /run and the bulk executor ---

def welcome_context(name: str, email: str, role: str, manager: str) -> Dict[str, str]:
    return {"name": name, "email": email, "role": role, "manager": manager}


def split_name(name: str, first: str, last: str) -> Tuple[str, str]:
    """Prefer explicit first/last; otherwise split the display name on the first space."""
    first, last = first.strip(), last.strip()
    if not first and not last and name.strip():
        parts = name.strip().split()
        first = parts[0]
        last = " ".join(parts[1:])
    return first, last


async def _apply_signature(conn, sig_store, cfg, email: str, first: str, last: str) -> Optional[str]:
    """Apply the role's signature template to ``email``; return the template name if applied, else None."""
    if not cfg.signature:
        return None
    body = sig_store.get(cfg.signature)
    if not body:
        return None
    user = GAMUser(primary_email=email, given_name=first, family_name=last)
    try:
        r = await conn.set_signature(email, render_signature(body, user), html=True)
        return cfg.signature if r.ok else None
    except Exception:  # noqa: BLE001 — signature is best-effort
        return None


async def _apply_groups(conn, email: str, groups: List[str]) -> "tuple[int, list]":
    ok, failed = 0, []
    for g in groups:
        try:
            r = await conn.add_group_member(g, email)
            if r.ok:
                ok += 1
            else:
                failed.append(g)
        except Exception:  # noqa: BLE001
            failed.append(g)
    return ok, failed


async def _apply_calendars(conn, email: str, calendars: List[str]) -> "tuple[int, list]":
    ok, failed = 0, []
    for c in calendars:
        try:
            r = await conn.subscribe_calendar_for(email, c)
            if r.ok:
                ok += 1
            else:
                failed.append(c)
        except Exception:  # noqa: BLE001
            failed.append(c)
    return ok, failed


async def provision_hire(conn, sig_store, store, cfg, hire: dict) -> dict:
    """Run one hire's onboarding end-to-end and return a structured result (no HTML, never raises).

    ``hire`` is a parsed CSV row; ``cfg`` its resolved role template. Best-effort per sub-step. When an
    account is created, a blank ``notify`` puts the temp password on ``credential`` (printable sheet);
    a ``notify`` email hands sign-in delivery to GAM/Google and only sets ``notified``."""
    email = (hire.get("email") or "").strip()
    name = (hire.get("name") or "").strip()
    first, last = split_name(name, hire.get("first", ""), hire.get("last", ""))
    res = {"email": email, "name": name or (first + " " + last).strip() or email, "role": hire["role"],
           "ok": True, "account_created": False, "notified": False, "credential": None,
           "signature": None, "groups": None, "calendars": None, "tasklist": None,
           "email_sent": None, "errors": []}

    if hire.get("create_account"):
        if not email:
            res["ok"] = False; res["errors"].append("no email to create the account"); return res
        if not first or not last:
            res["ok"] = False; res["errors"].append("need a first & last name to create the account"); return res
        notify = (hire.get("notify") or "").strip() or None
        pw = generate_temp_password()
        try:
            cr = await conn.create_user(email, first, last, pw, change_password=True,
                                        org_unit=(cfg.org_unit or None), notify=notify)
        except Exception as exc:  # noqa: BLE001
            res["ok"] = False; res["errors"].append("create: " + str(getattr(exc, "remediation", exc)))
            res["stop"] = (getattr(exc, "kind", None), str(getattr(exc, "remediation", exc)))
            return res
        if not cr.ok:
            res["ok"] = False; res["errors"].append("create: " + (cr.detail or "failed"))
            res["stop"] = (cr.kind, cr.remediation)   # the bulk executor stops on an account-wide kind
            return res
        res["account_created"] = True
        if notify:
            res["notified"] = True          # GAM emailed the sign-in info; the pw lives only in that email
        else:
            res["credential"] = {"name": res["name"], "email": email, "password": pw,
                                 "org_unit": cfg.org_unit or "/"}

    if email:
        # Signature only for an account THIS run created — matches the single /run flow and never
        # clobbers an existing user's customized signature (or renders a blank {name} for an
        # existing-account row that has no name in the CSV).
        if res["account_created"]:
            res["signature"] = await _apply_signature(conn, sig_store, cfg, email, first, last)
            if cfg.signature and not res["signature"]:
                res["errors"].append("signature: not applied")
        if cfg.groups:
            g_ok, g_fail = await _apply_groups(conn, email, cfg.groups)
            res["groups"] = {"added": g_ok, "total": len(cfg.groups), "failed": g_fail}
            if g_fail:
                res["errors"].append("groups: couldn't add " + ", ".join(g_fail))
        if cfg.calendars:
            c_ok, c_fail = await _apply_calendars(conn, email, cfg.calendars)
            res["calendars"] = {"added": c_ok, "total": len(cfg.calendars), "failed": c_fail}
            if c_fail:
                res["errors"].append("calendars: couldn't subscribe " + ", ".join(c_fail))

    assignee = (hire.get("assignee") or email).strip()
    if assignee:
        title = "Onboard {} — {}".format(name or email or "new hire", hire["role"])
        try:
            tl = await conn.create_onboarding_runbook(assignee, title, cfg.steps)
            res["tasklist"] = {"assignee": assignee, "tasklist_id": tl.get("tasklist_id", ""),
                               "created": tl.get("created"), "total": tl.get("total"),
                               "failed": tl.get("failed", []), "ok": bool(tl.get("tasklist_id"))}
            if not tl.get("tasklist_id"):
                res["errors"].append("tasks: no tasklist id came back")
        except Exception as exc:  # noqa: BLE001
            res["errors"].append("tasks: " + str(getattr(exc, "remediation", exc)))

    if hire.get("send_welcome") and email:
        # The single flow holds the welcome template its preview rendered; a bulk row uses the store's.
        w = hire.get("welcome") or store.welcome()
        ctx = welcome_context(name, email, hire["role"], hire.get("manager", ""))
        try:
            we = await conn.send_welcome_email(email, render(w["subject"], ctx), render(w["body"], ctx))
            res["email_sent"] = bool(we.ok)
        except Exception:  # noqa: BLE001
            res["email_sent"] = False
    if res["email_sent"] is False:
        res["errors"].append("welcome email: failed to send")
    # A hire is only "ok" if every best-effort sub-step also succeeded (not just the create).
    res["ok"] = not res["errors"]
    return res


# --- bulk: resolve a parsed CSV against the role templates, then tally what a run would do ---

def resolve_hires(rows: List[Dict], store: RunbookStore) -> Tuple[List[Tuple[Dict, RoleTemplate]], List[str]]:
    """Pair each parsed row with its role template. A row whose role is unknown or has no steps
    becomes an error, not a pair."""
    cfgs: Dict[str, Optional[RoleTemplate]] = {}
    pairs: List[Tuple[Dict, RoleTemplate]] = []
    errors: List[str] = []
    for hire in rows:
        role = hire["role"]
        if role not in cfgs:
            cfgs[role] = store.role(role)
        cfg = cfgs[role]
        if cfg is None or not cfg.steps:
            who = hire.get("email") or hire.get("name") or "a row"
            errors.append("{}: unknown role '{}' (or it has no steps).".format(who, role))
            continue
        pairs.append((hire, cfg))
    return pairs, errors


def tally_hires(pairs: List[Tuple[Dict, RoleTemplate]]) -> Dict[str, Any]:
    """What running ``pairs`` would do: hires per role, and the accounts created — their sign-in
    emailed (``notify``) or on the printable sheet."""
    per_role: Dict[str, int] = {}
    creates = notifies = sheets = 0
    for hire, _cfg in pairs:
        per_role[hire["role"]] = per_role.get(hire["role"], 0) + 1
        if hire["create_account"]:
            creates += 1
            if (hire.get("notify") or "").strip():
                notifies += 1
            else:
                sheets += 1
    return {"total": len(pairs), "creates": creates, "notifies": notifies, "sheets": sheets,
            "per_role": sorted(per_role.items())}

"""Onboarding runbooks: editable role → task-list templates + a welcome-email template.

The admin defines, in the app, what setup each ROLE needs — mostly manual vendor steps that have no
API and just need to get *done* by someone. Generating a runbook for a new hire turns those steps
into a Google Tasks list on whoever is doing the setup, so the checklist lives in their Gmail/Tasks
(durable, delegatable, survives app restarts) rather than as fragile local state. Only the
*templates* are stored locally, as plain JSON the admin edits.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

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
# A role is stored as {"steps": [...], "signature": "<saved template name>", "org_unit": "/Path"};
# an older file's bare list of steps is migrated on load (see RunbookStore._load).
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
        },
    },
    "welcome": {
        "subject": "Welcome to the team, {name}!",
        "body": ("Hi {name},\n\nWelcome aboard as our new {role}. Your account is {email} — "
                 "your manager {manager} will help you get set up.\n\nGlad to have you here."),
    },
}


def _as_role(value) -> Dict:
    """Normalise a stored role to {steps, signature, org_unit} — tolerating the old list-of-steps form."""
    if isinstance(value, list):
        return {"steps": [str(s) for s in value], "signature": "", "org_unit": ""}
    if isinstance(value, dict):
        return {
            "steps": [str(s) for s in value.get("steps", [])],
            "signature": str(value.get("signature", "") or ""),
            "org_unit": str(value.get("org_unit", "") or ""),
        }
    return {"steps": [], "signature": "", "org_unit": ""}


@dataclass
class RoleTemplate:
    name: str
    steps: List[str]
    signature: str = ""   # a saved signature-template name to apply to the new hire (blank = none)
    org_unit: str = ""    # the OU the account is created in (blank = domain default "/")


def render(template: str, ctx: Dict[str, str]) -> str:
    """Substitute {name}/{role}/{email}/{manager}; literal-brace-safe (only known vars replaced)."""
    out = template or ""
    for key in WELCOME_VARS:
        out = out.replace("{" + key + "}", str(ctx.get(key, "")))
    return out


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
            out.append(RoleTemplate(n, r["steps"], r["signature"], r["org_unit"]))
        return out

    def role_names(self) -> List[str]:
        return sorted(self._data["roles"].keys())

    def role(self, name: str) -> Optional[RoleTemplate]:
        v = self._data["roles"].get(name)
        if v is None:
            return None
        r = _as_role(v)
        return RoleTemplate(name, r["steps"], r["signature"], r["org_unit"])

    def steps_for(self, name: str) -> List[str]:
        return _as_role(self._data["roles"].get(name, {}))["steps"]

    def set_role(self, name: str, steps: List[str], signature: str = "", org_unit: str = "") -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("Role name is required.")
        self._data["roles"][name] = {
            "steps": [s.strip() for s in steps if s.strip()],
            "signature": (signature or "").strip(),
            "org_unit": (org_unit or "").strip(),
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

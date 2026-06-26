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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .paths import app_data_dir

WELCOME_VARS = ["name", "role", "email", "manager"]   # the {tokens} the welcome email understands


def default_store_path() -> Path:
    return app_data_dir() / "onboarding.json"


# Seeded on first run; the admin edits/replaces this entirely — nothing here is hardcoded into logic.
_DEFAULT = {
    "roles": {
        "Salesperson": [
            "Set up Brite for the employee",
            "Create Wesley Hall login",
            "Set up POS login (salesperson)",
            "Add to the sales group",
        ],
    },
    "welcome": {
        "subject": "Welcome to the team, {name}!",
        "body": ("Hi {name},\n\nWelcome aboard as our new {role}. Your account is {email} — "
                 "your manager {manager} will help you get set up.\n\nGlad to have you here."),
    },
}


@dataclass
class RoleTemplate:
    name: str
    steps: List[str]


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
        return [RoleTemplate(n, list(s)) for n, s in sorted(self._data["roles"].items())]

    def role_names(self) -> List[str]:
        return sorted(self._data["roles"].keys())

    def steps_for(self, name: str) -> List[str]:
        return list(self._data["roles"].get(name, []))

    def set_role(self, name: str, steps: List[str]) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("Role name is required.")
        self._data["roles"][name] = [s.strip() for s in steps if s.strip()]
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

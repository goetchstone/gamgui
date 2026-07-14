"""Scoped signature templates.

Design one signature template with variables, scope it to the whole company / an OU / a department,
preview it rendered for a real person, and apply it to everyone in scope. The render is pure Python
substitution (so the preview is exactly what each user gets); apply sets each user's Gmail signature.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .gam.models import GAMUser
from .paths import app_data_dir

# A ``[[ ... ]]`` block is kept only if every variable inside it resolves to a non-empty value.
_OPTIONAL_RE = re.compile(r"\[\[(.*?)\]\]", re.DOTALL)

# Template variable -> human description (shown in the editor's variable reference).
# `{role}` is an alias for `{title}` since the org uses the job title as the role.
VARIABLES: Dict[str, str] = {
    "{name}": "Full name",
    "{first}": "First name",
    "{last}": "Last name",
    "{email}": "Primary email",
    "{title}": "Job title",
    "{role}": "Job title (alias)",
    "{phone}": "Work phone",
    "{department}": "Department",
    "{location}": "Location / store",
    "{ou}": "Org unit path",
}


def render_signature(template: str, user: GAMUser) -> str:
    """Substitute the variables in ``template`` with ``user``'s values.

    ``[[ ... ]]`` blocks are *optional*: a block is dropped entirely if any variable it references
    is empty for this user. So ``[[ {title} ·]]`` vanishes cleanly for people with no title set,
    letting one template roll out company-wide before every profile is filled in.
    """
    values = {
        "{name}": user.full_name,
        "{first}": user.given_name,
        "{last}": user.family_name,
        "{email}": user.primary_email,
        "{title}": user.title or "",
        "{role}": user.title or "",
        "{phone}": user.phone or "",
        "{department}": user.department or "",
        "{location}": user.location or "",
        "{ou}": user.org_unit_path or "",
    }

    def _resolve_optional(match: "re.Match[str]") -> str:
        inner = match.group(1)
        for var, val in values.items():
            if var in inner and not val:
                return ""  # a referenced variable is empty -> drop the whole block
        return inner

    out = _OPTIONAL_RE.sub(_resolve_optional, template or "")
    for var, val in values.items():
        out = out.replace(var, val)
    return out


def match_scope(users: List[GAMUser], scope_type: str, scope_value: str = "") -> List[GAMUser]:
    """Active users matching a scope: ``user`` (a single email, for testing), ``company`` (all),
    ``ou`` (path + children), or ``department``."""
    active = [u for u in users if not u.suspended]
    value = (scope_value or "").strip()
    if scope_type == "user":
        # Single-user scope: exact (case-insensitive) email match. An empty selection matches
        # nobody — never silently fall through to the whole company.
        return [u for u in active if u.primary_email.lower() == value.lower()] if value else []
    if scope_type == "ou" and value:
        prefix = value.rstrip("/") + "/"
        return [u for u in active if u.org_unit_path == value or u.org_unit_path.startswith(prefix)]
    if scope_type == "department" and value:
        return [u for u in active if (u.department or "").strip().lower() == value.lower()]
    if scope_type == "location" and value:
        return [u for u in active if (u.location or "").strip().lower() == value.lower()]
    return active


def scope_options(users: List[GAMUser]) -> Dict[str, List[str]]:
    """The distinct OUs, departments, and active user emails present, for the scope dropdowns."""
    ous = sorted({u.org_unit_path for u in users if u.org_unit_path})
    departments = sorted({u.department for u in users if u.department})
    locations = sorted({u.location for u in users if u.location})
    user_emails = sorted(u.primary_email for u in users if not u.suspended)
    return {"ous": ous, "departments": departments, "locations": locations, "users": user_emails}


# --- saved template store ------------------------------------------------------------------------

def default_templates_path() -> Path:
    return app_data_dir() / "signatures.json"


# Three tasteful starters seeded on first run. All are email-client-safe: inline styles only, no
# external images or fonts, a system font stack, a table where the accent alignment matters, and
# ``[[ … ]]`` optional segments so title / phone / department degrade cleanly for sparse profiles.
# "Your Company" is placeholder text the admin swaps for their org name.
_DEFAULT_TEMPLATES: Dict[str, str] = {
    "Classic": (
        '<div style="font-family:-apple-system,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'font-size:13px;line-height:1.5;color:#3f4a5a;">\n'
        '  <div style="font-weight:600;color:#1f2733;">{name}</div>\n'
        '  <div>[[{title} · ]]Your Company</div>\n'
        '  <div style="color:#6b7280;">{email}[[ · {phone}]]</div>\n'
        '</div>'
    ),
    "Modern accent": (
        '<table cellpadding="0" cellspacing="0" role="presentation" '
        'style="font-family:-apple-system,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'font-size:13px;color:#3f4a5a;">\n'
        '  <tr>\n'
        '    <td style="border-left:3px solid #52647B;padding:1px 0 1px 12px;line-height:1.5;">\n'
        '      <div style="font-weight:600;font-size:14px;color:#1f2733;">{name}</div>\n'
        '      <div style="color:#52647B;">[[{title} · ]]Your Company</div>\n'
        '      <div style="color:#6b7280;">{email}[[ · {phone}]]</div>\n'
        '      [[<div style="color:#6b7280;">{department}</div>]]\n'
        '    </td>\n'
        '  </tr>\n'
        '</table>'
    ),
    "Minimal": (
        '<div style="font-family:-apple-system,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'font-size:13px;color:#3f4a5a;">{name}[[ · {title}]] · Your Company · {email}</div>'
    ),
}

_MAX_NAME_LEN = 60


class SignatureStore:
    """Plain-JSON persistence for named HTML signature templates (0600, seeded on first run).

    Mirrors ``RunbookStore``: a corrupt or missing file falls back to a deep copy of the seed, so the
    admin always has the three starters to load and tweak.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_templates_path()
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict) and isinstance(data.get("templates"), dict):
                    return data
            except Exception:  # noqa: BLE001 — corrupt/old file: fall back to the seed
                pass
        return json.loads(json.dumps({"templates": _DEFAULT_TEMPLATES}))   # deep copy of the seed

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

    def names(self) -> List[str]:
        return sorted(self._data["templates"].keys())

    def get(self, name: str) -> str:
        return self._data["templates"].get(name, "")

    def save(self, name: str, body: str) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("Template name is required.")
        if len(name) > _MAX_NAME_LEN:
            raise ValueError("Template name must be {} characters or fewer.".format(_MAX_NAME_LEN))
        if not (body or "").strip():
            raise ValueError("Template body is empty — put some HTML in the editor before saving.")
        self._data["templates"][name] = body
        self._save()

    def delete(self, name: str) -> None:
        self._data["templates"].pop(name, None)
        self._save()

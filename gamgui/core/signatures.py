"""Scoped signature templates.

Design one signature template with variables, scope it to the whole company / an OU / a department,
preview it rendered for a real person, and apply it to everyone in scope. The render is pure Python
substitution (so the preview is exactly what each user gets); apply sets each user's Gmail signature.
"""

from __future__ import annotations

import re
from typing import Dict, List

from .gam.models import GAMUser

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

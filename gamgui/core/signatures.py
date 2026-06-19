"""Scoped signature templates.

Design one signature template with variables, scope it to the whole company / an OU / a department,
preview it rendered for a real person, and apply it to everyone in scope. The render is pure Python
substitution (so the preview is exactly what each user gets); apply sets each user's Gmail signature.
"""

from __future__ import annotations

from typing import Dict, List

from .gam.models import GAMUser

# Template variable -> human description (shown in the editor's variable reference).
# `{role}` is an alias for `{title}` since the org uses the job title as the role.
VARIABLES: Dict[str, str] = {
    "{name}": "Full name",
    "{first}": "First name",
    "{last}": "Last name",
    "{email}": "Primary email",
    "{title}": "Job title",
    "{role}": "Job title (alias)",
    "{department}": "Department",
    "{ou}": "Org unit path",
}


def render_signature(template: str, user: GAMUser) -> str:
    """Substitute the variables in ``template`` with ``user``'s values."""
    values = {
        "{name}": user.full_name,
        "{first}": user.given_name,
        "{last}": user.family_name,
        "{email}": user.primary_email,
        "{title}": user.title or "",
        "{role}": user.title or "",
        "{department}": user.department or "",
        "{ou}": user.org_unit_path or "",
    }
    out = template or ""
    for var, val in values.items():
        out = out.replace(var, val)
    return out


def match_scope(users: List[GAMUser], scope_type: str, scope_value: str = "") -> List[GAMUser]:
    """Active users matching a scope: ``company`` (all), ``ou`` (path + children), or ``department``."""
    active = [u for u in users if not u.suspended]
    value = (scope_value or "").strip()
    if scope_type == "ou" and value:
        prefix = value.rstrip("/") + "/"
        return [u for u in active if u.org_unit_path == value or u.org_unit_path.startswith(prefix)]
    if scope_type == "department" and value:
        return [u for u in active if (u.department or "").strip().lower() == value.lower()]
    return active


def scope_options(users: List[GAMUser]) -> Dict[str, List[str]]:
    """The distinct OUs and departments present, for the scope dropdowns."""
    ous = sorted({u.org_unit_path for u in users if u.org_unit_path})
    departments = sorted({u.department for u in users if u.department})
    return {"ous": ous, "departments": departments}

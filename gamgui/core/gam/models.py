"""Lightweight dataclass models decoded from GAM ``formatjson`` output.

We intentionally keep these tolerant: GAM's JSON keys vary by command and version, so we
read the handful of fields the UI needs and stash the rest in ``raw`` for detail views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present key (GAM varies between e.g. ``primaryEmail``/``email``)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


@dataclass
class GAMUser:
    primary_email: str
    given_name: str = ""
    family_name: str = ""
    suspended: bool = False
    org_unit_path: str = "/"
    is_admin: bool = False
    last_login_time: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        name = f"{self.given_name} {self.family_name}".strip()
        return name or self.primary_email

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "GAMUser":
        name = d.get("name") or {}
        if not isinstance(name, dict):
            name = {}
        return cls(
            primary_email=_get(d, "primaryEmail", "email", "User", default=""),
            given_name=_get(name, "givenName") or _get(d, "givenName", "First Name", default=""),
            family_name=_get(name, "familyName") or _get(d, "familyName", "Last Name", default=""),
            suspended=_as_bool(_get(d, "suspended", "Suspended", default=False)),
            org_unit_path=_get(d, "orgUnitPath", "OrgUnitPath", default="/"),
            is_admin=_as_bool(_get(d, "isAdmin", "Is Admin", default=False)),
            last_login_time=_get(d, "lastLoginTime", "Last Login Time"),
            aliases=_as_list(_get(d, "aliases", "Aliases", default=[])),
            raw=d,
        )


@dataclass
class GAMGroup:
    email: str
    name: str = ""
    description: str = ""
    members_count: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "GAMGroup":
        count = _get(d, "directMembersCount", "Members")
        try:
            count = int(count) if count is not None else None
        except (TypeError, ValueError):
            count = None
        return cls(
            email=_get(d, "email", "Email", "Group", default=""),
            name=_get(d, "name", "Name", default=""),
            description=_get(d, "description", "Description", default=""),
            members_count=count,
            raw=d,
        )


@dataclass
class GroupMember:
    email: str
    role: str = "MEMBER"
    member_type: str = "USER"
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "GroupMember":
        return cls(
            email=_get(d, "email", "Email", default=""),
            role=str(_get(d, "role", "Role", default="MEMBER")).upper(),
            member_type=str(_get(d, "type", "Type", default="USER")).upper(),
            status=str(_get(d, "status", "Status", default="")),
            raw=d,
        )


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "on", "1")
    return bool(v)


def _as_list(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v:
        # GAM CSV sometimes joins multi-values with a space or comma.
        return [p for p in re_split(v) if p]
    return []


def re_split(v: str) -> List[str]:
    import re

    return [p.strip() for p in re.split(r"[\s,]+", v.strip())]

"""Lightweight dataclass models decoded from GAM ``formatjson`` output.

We intentionally keep these tolerant: GAM's JSON keys vary by command and version, so we
read the handful of fields the UI needs and stash the rest in ``raw`` for detail views.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present key (GAM varies between e.g. ``primaryEmail``/``email``)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _primary(items: Any) -> Dict[str, Any]:
    """Pick the ``primary`` entry from a Directory list (organizations/locations), else the first."""
    if not isinstance(items, list) or not items:
        return {}
    for it in items:
        if isinstance(it, dict) and it.get("primary"):
            return it
    return items[0] if isinstance(items[0], dict) else {}


@dataclass
class GAMUser:
    primary_email: str
    given_name: str = ""
    family_name: str = ""
    suspended: bool = False
    org_unit_path: str = "/"
    is_admin: bool = False
    is_delegated_admin: bool = False
    enrolled_2sv: bool = False
    title: str = ""
    department: str = ""
    location: str = ""
    phone: str = ""
    recovery_email: str = ""
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
        org = _primary(d.get("organizations"))      # job title / department live here
        loc = _primary(d.get("locations"))           # building / desk
        phone = _primary(d.get("phones"))            # work phone number
        return cls(
            primary_email=_get(d, "primaryEmail", "email", "User", default=""),
            given_name=_get(name, "givenName") or _get(d, "givenName", "First Name", default=""),
            family_name=_get(name, "familyName") or _get(d, "familyName", "Last Name", default=""),
            suspended=_as_bool(_get(d, "suspended", "Suspended", default=False)),
            org_unit_path=_get(d, "orgUnitPath", "OrgUnitPath", default="/"),
            is_admin=_as_bool(_get(d, "isAdmin", "Is Admin", default=False)),
            is_delegated_admin=_as_bool(_get(d, "isDelegatedAdmin", default=False)),
            enrolled_2sv=_as_bool(_get(d, "isEnrolledIn2Sv", default=False)),
            title=str(org.get("title") or _get(d, "Organization Title", default="") or ""),
            department=str(org.get("department") or _get(d, "Organization Department", default="") or ""),
            location=str(loc.get("buildingName") or loc.get("buildingId") or ""),
            phone=str(phone.get("value") or ""),
            recovery_email=str(_get(d, "recoveryEmail", default="") or ""),
            last_login_time=_get(d, "lastLoginTime", "Last Login Time"),
            aliases=_as_list(_get(d, "aliases", "Aliases", default=[])),
            raw=d,
        )


@dataclass
class Vacation:
    enabled: bool = False
    subject: str = ""
    message: str = ""
    contacts_only: bool = False
    domain_only: bool = False

    @classmethod
    def from_show_text(cls, text: str) -> "Vacation":
        """Parse the text output of ``gam user X show vacation`` (formatjson isn't supported)."""
        enabled = contacts = domain = False
        subject = ""
        msg_lines: List[str] = []
        in_message = False
        for line in (text or "").splitlines():
            s = line.strip()
            if in_message:
                # Stop the message at the next field — or at GAM's config-init banner, in case any
                # slipped past the runner's filter (defense in depth so it never lands in the reply).
                if s.startswith(("Enabled:", "Subject:", "Contacts Only:", "Domain Only:", "Created:", "Config File:")):
                    in_message = False
                else:
                    msg_lines.append(s)
                    continue
            if s.startswith("Enabled:"):
                enabled = "true" in s.lower()
            elif s.startswith("Contacts Only:"):
                contacts = "true" in s.lower()
            elif s.startswith("Domain Only:"):
                domain = "true" in s.lower()
            elif s.startswith("Subject:"):
                subject = s[len("Subject:"):].strip()
            elif s.startswith("Message:"):
                in_message = True
        return cls(
            enabled=enabled,
            subject=subject,
            message="\n".join(msg_lines).strip(),
            contacts_only=contacts,
            domain_only=domain,
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


@dataclass
class CalendarACL:
    """One access rule on a calendar: who has access (scope) and at what role."""

    scope_type: str = "user"   # user | group | domain | default
    scope_value: str = ""      # email / domain; empty for whole-domain or default (public)
    role: str = ""             # owner | writer | reader | freebusyreader | editor | ...
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "CalendarACL":
        scope = d.get("scope") if isinstance(d.get("scope"), dict) else {}
        stype = str(scope.get("type") or "").strip()
        svalue = str(scope.get("value") or "").strip()
        if not stype:
            # Fall back to the ACL rule id, e.g. "user:bob@x" / "group:g@x" / "domain:x" / "default".
            rid = str(_get(d, "id", "Id", default="")).strip()
            if rid:
                stype, _, svalue = rid.partition(":")
        return cls(
            scope_type=(stype or "user").lower(),
            scope_value=svalue,
            role=str(_get(d, "role", "Role", default="")).strip(),
            raw=d,
        )

    @property
    def who(self) -> str:
        if self.scope_type == "default":
            return "Public (anyone)"
        if self.scope_type == "domain":
            return f"Everyone at {self.scope_value}" if self.scope_value else "Entire domain"
        return self.scope_value or self.scope_type

    @property
    def role_label(self) -> str:
        return {
            "owner": "Owner",
            "writer": "Make changes & manage sharing",
            "editor": "Make changes & manage sharing",
            "writerwithoutprivateaccess": "Make changes (no private events)",
            "reader": "See all event details",
            "freebusyreader": "See free/busy only",
            "freebusy": "See free/busy only",
            "none": "No access",
        }.get(self.role.lower(), self.role or "—")

    @property
    def scope_token(self) -> str:
        """The token GAM's ``delete calendaracls`` expects for this rule."""
        if self.scope_type == "default":
            return "default"
        if self.scope_type == "domain":
            return f"domain:{self.scope_value}" if self.scope_value else "domain"
        if self.scope_type == "group":
            return f"group:{self.scope_value}"
        return self.scope_value  # user -> bare email


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
        return [p for p in re_split(v) if p]
    return []


def re_split(v: str) -> List[str]:
    return [p.strip() for p in re.split(r"[\s,]+", v.strip())]

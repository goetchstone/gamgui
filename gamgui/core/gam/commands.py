"""GAM7 command builders.

Every ``gam`` invocation in the app is constructed here and nowhere else. Two reasons:

1. **Safety** — builders return an ``argv`` *list* (e.g. ``["print", "users", ...]``), which is
   passed straight to ``exec`` with no shell. User-supplied values (emails, signatures) are never
   interpolated into a shell string, so there is no shell-injection surface.
2. **Maintainability** — GAM occasionally tweaks sub-command syntax between releases. Keeping it in
   one file with arg-shape tests means a version bump is a single-file change.

NOTE: the exact sub-syntax of a few mutating commands (group membership, signature flags) should be
re-verified against the pinned GAM version during the real-tenant acceptance pass. The arg-shape
unit tests pin today's intended form so drift is caught early.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

# Roles accepted by Google Directory for group membership.
GROUP_ROLES = ("member", "manager", "owner")

# `gam print users` returns ONLY primaryEmail unless fields are requested — these populate the list.
USER_LIST_FIELDS = ("primaryEmail", "name", "suspended", "orgUnitPath", "isAdmin")
# Fields for the detail view (adds aliases + last login on top of the list fields).
USER_DETAIL_FIELDS = ("primaryEmail", "name", "suspended", "orgUnitPath", "isAdmin", "lastLoginTime", "aliases")
# `gam print groups` likewise returns only email unless fields are requested.
GROUP_LIST_FIELDS = ("email", "name", "description", "directMembersCount")


class GAMCommands:
    # --- diagnostics / setup ----------------------------------------------------------
    @staticmethod
    def version() -> List[str]:
        return ["version"]

    @staticmethod
    def create_project(admin: str, project_id: Optional[str] = None) -> List[str]:
        argv = ["create", "project", admin]
        if project_id:
            argv += ["project", project_id]
        return argv

    @staticmethod
    def oauth_create(admin: str) -> List[str]:
        return ["oauth", "create", admin]

    @staticmethod
    def create_svcacct(admin: str) -> List[str]:
        return ["create", "svcacct", admin]

    @staticmethod
    def check_svcacct(admin: str) -> List[str]:
        # Verifies domain-wide delegation scopes. NOTE: the noun is `serviceaccount`
        # here (GAM uses `create svcacct` but `check serviceaccount` — not symmetric).
        return ["user", admin, "check", "serviceaccount"]

    # --- users (read) -----------------------------------------------------------------
    @staticmethod
    def print_users(query: Optional[str] = None, fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["print", "users"]
        if query:
            argv += ["query", query]
        argv += ["fields", ",".join(fields or USER_LIST_FIELDS)]
        argv.append("formatjson")
        return argv

    @staticmethod
    def info_user(email: str, fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["info", "user", email, "fields", ",".join(fields or USER_DETAIL_FIELDS)]
        argv.append("formatjson")
        return argv

    # --- users (mutating) -------------------------------------------------------------
    @staticmethod
    def create_user(
        email: str,
        first_name: str,
        last_name: str,
        password: str,
        change_password: bool = True,
        org_unit: Optional[str] = None,
    ) -> List[str]:
        argv = [
            "create", "user", email,
            "firstname", first_name,
            "lastname", last_name,
            "password", password,
            "changepassword", "on" if change_password else "off",
        ]
        if org_unit:
            argv += ["org", org_unit]
        return argv

    @staticmethod
    def update_user(email: str, **fields: str) -> List[str]:
        """Generic user update. ``fields`` are GAM attribute/value pairs, e.g. ``firstname='Jo'``."""
        argv = ["update", "user", email]
        for key, value in fields.items():
            argv += [key, str(value)]
        return argv

    @staticmethod
    def set_suspended(email: str, suspended: bool) -> List[str]:
        # `update user ... suspended on/off` is the canonical, version-stable form.
        return ["update", "user", email, "suspended", "on" if suspended else "off"]

    # --- gmail: signature / delegate / forwarding / vacation --------------------------
    @staticmethod
    def set_signature(email: str, signature: str, html: bool = True) -> List[str]:
        argv = ["user", email, "signature", signature]
        if html:
            argv.append("html")
        return argv

    @staticmethod
    def add_delegate(email: str, delegate: str) -> List[str]:
        return ["user", email, "add", "delegate", delegate]

    @staticmethod
    def remove_delegate(email: str, delegate: str) -> List[str]:
        return ["user", email, "delete", "delegate", delegate]

    @staticmethod
    def print_delegates(email: str) -> List[str]:
        # NOTE: `print delegates` does NOT support `formatjson` (it errors "Invalid argument").
        # Output is plain CSV with a `delegateAddress` column.
        return ["user", email, "print", "delegates"]

    @staticmethod
    def set_vacation(
        email: str,
        subject: str,
        message: str,
        html: bool = False,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[str]:
        argv = ["user", email, "vacation", "on", "subject", subject, "message", message]
        if html:
            argv.append("html")
        if start:
            argv += ["start", start]
        if end:
            argv += ["end", end]
        return argv

    # --- groups -----------------------------------------------------------------------
    @staticmethod
    def print_groups(fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["print", "groups", "fields", ",".join(fields or GROUP_LIST_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def create_group(email: str, name: str = "", description: str = "") -> List[str]:
        argv = ["create", "group", email]
        if name:
            argv += ["name", name]
        if description:
            argv += ["description", description]
        return argv

    @staticmethod
    def print_group_members(group: str) -> List[str]:
        return ["print", "group-members", "group", group, "formatjson"]

    @staticmethod
    def add_group_member(group: str, member: str, role: str = "member") -> List[str]:
        role = _validate_role(role)
        return ["update", "group", group, "add", role, member]

    @staticmethod
    def remove_group_member(group: str, member: str) -> List[str]:
        return ["update", "group", group, "remove", member]


def _validate_role(role: str) -> str:
    role = (role or "member").strip().lower()
    if role not in GROUP_ROLES:
        raise ValueError(f"invalid group role {role!r}; expected one of {GROUP_ROLES}")
    return role


def build_user_query(search: str = "", include_suspended: bool = True) -> Optional[str]:
    """Translate a free-text search box into a Directory API query string.

    Empty search returns ``None`` (list everyone). A bare token matches email/name prefixes.
    """
    clauses: List[str] = []
    search = (search or "").strip()
    if search:
        # Directory API supports prefix matching with '*'. Match common fields.
        token = search.replace("'", "")
        clauses.append(f"email:{token}* givenName:{token}* familyName:{token}*")
    if not include_suspended:
        clauses.append("isSuspended=false")
    return " ".join(clauses) if clauses else None

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

# The GAM7 version GamGUI is pinned to and tested against — the SINGLE SOURCE OF TRUTH.
# `scripts/fetch_gam.sh` (TAG), the mock, and the version tests must all match this; that's enforced by
# tests/test_command_contract.py so they can't drift. Bump deliberately via the "Updating GAM" runbook.
# Compared (as a substring) against the running `gam version` for the fail-soft runtime self-check.
EXPECTED_GAM_VERSION = "7.46.02"

# Roles accepted by Google Directory for group membership.
GROUP_ROLES = ("member", "manager", "owner")

# `gam print users` returns ONLY primaryEmail unless fields are requested — these populate the list.
# `organizations` carries the job title (the practical "role" for automations).
USER_LIST_FIELDS = ("primaryEmail", "name", "suspended", "orgUnitPath", "organizations")
# Fields for the detail view: identity + role/automation signals + security flags.
USER_DETAIL_FIELDS = (
    "primaryEmail", "name", "suspended", "orgUnitPath", "isAdmin", "isDelegatedAdmin",
    "isEnrolledIn2Sv", "lastLoginTime", "aliases", "organizations", "locations", "phones", "recoveryEmail",
)
# `gam print groups` likewise returns only email unless fields are requested.
GROUP_LIST_FIELDS = ("email", "name", "description", "directMembersCount")
# Superset fetched once and cached to serve the users list (needs title), reports, AND the detail
# view (so opening a user is instant + uses the reliable JSON path, not the `info user` text format).
CACHE_FIELDS = (
    "primaryEmail", "name", "suspended", "orgUnitPath", "organizations",
    "isAdmin", "isDelegatedAdmin", "isEnrolledIn2Sv", "lastLoginTime", "recoveryEmail",
    "aliases", "locations", "phones",
)


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
    def report_users(date: str, params: Sequence[str]) -> List[str]:
        # Admin SDK usage report (storage, mail, drive). Data lags ~2-3 days.
        return ["report", "users", "date", date, "parameters", ",".join(params)]

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
    def update_organization(email: str, title: str = "", department: str = "") -> List[str]:
        """Set the primary organization's title + department (here, department holds the store).

        GAM's ``organization`` replaces the primary org, so we always pass both fields together (the
        editor pre-fills the current values) to avoid clearing one while changing the other.
        """
        return ["update", "user", email, "organization", "title", title, "department", department, "primary"]

    @staticmethod
    def set_suspended(email: str, suspended: bool) -> List[str]:
        # `update user ... suspended on/off` is the canonical, version-stable form.
        return ["update", "user", email, "suspended", "on" if suspended else "off"]

    # --- calendar access ---------------------------------------------------------------
    @staticmethod
    def print_calendar_acls(email: str, calendar: str = "primary") -> List[str]:
        return ["user", email, "print", "calendaracls", calendar, "formatjson"]

    @staticmethod
    def add_calendar_acl(email: str, target: str, role: str = "reader", calendar: str = "primary") -> List[str]:
        # `target` is a scope: a bare email = a user; pass "group:<email>"/"domain"/"default" as-is.
        return ["user", email, "add", "calendaracls", calendar, role, target]

    @staticmethod
    def delete_calendar_acl(email: str, scope: str, calendar: str = "primary") -> List[str]:
        return ["user", email, "delete", "calendaracls", calendar, scope]

    # --- calendars / resources / events ------------------------------------------------
    @staticmethod
    def print_resources(query: str = "") -> List[str]:
        argv = ["print", "resources", "fields", "id,name,email,resourcetype,buildingid"]
        if query:
            argv += ["query", query]
        argv.append("formatjson")
        return argv

    @staticmethod
    def print_user_calendars(email: str) -> List[str]:
        return ["user", email, "print", "calendars", "fields", "id,summary,accessrole,primary", "formatjson"]

    @staticmethod
    def print_all_calendars() -> List[str]:
        # Every user's calendar list (incl. secondary calendars) — filtered by name in Python.
        return ["all", "users", "print", "calendars", "fields", "id,summary,accessrole,primary", "formatjson"]

    @staticmethod
    def print_calendar_acls_cal(calendar_id: str) -> List[str]:
        # Standalone form: ACLs for ANY calendar id (room/secondary) via admin access.
        return ["calendars", calendar_id, "print", "calendaracls", "formatjson"]

    @staticmethod
    def remove_calendar(owner: str, calendar_id: str) -> List[str]:
        # PERMANENTLY delete a secondary calendar, acting as an owner (Calendars.delete).
        # GAM footgun: `remove calendars` deletes the calendar for everyone; `delete calendars`
        # would only unsubscribe this user (CalendarList.delete). Must impersonate an owner.
        return ["user", owner, "remove", "calendars", calendar_id]

    @staticmethod
    def unsubscribe_calendar(email: str, calendar_id: str) -> List[str]:
        # Just remove the calendar from one user's list (CalendarList.delete) — the calendar lives on.
        return ["user", email, "delete", "calendars", calendar_id]

    _EVENT_FIELDS = "id,summary,start,end,recurrence,recurringeventid,organizer,creator,status"

    @staticmethod
    def print_events(calendar_id: str, query: str = "", after: str = "", before: str = "") -> List[str]:
        argv = ["calendars", calendar_id, "print", "events"]
        if query:
            argv += ["query", query]
        if after:
            argv += ["after", after]
        if before:
            argv += ["before", before]
        argv += ["fields", GAMCommands._EVENT_FIELDS, "formatjson"]
        return argv

    @staticmethod
    def get_event(calendar_id: str, event_id: str) -> List[str]:
        # Re-read one event by id for the delete preview.
        return ["calendars", calendar_id, "print", "events", "eventid", event_id,
                "fields", GAMCommands._EVENT_FIELDS, "formatjson"]

    @staticmethod
    def delete_event(calendar_id: str, event_id: str, doit: bool = True) -> List[str]:
        # GAM dry-runs `delete events` without `doit`. Deleting a recurring master id drops the series.
        argv = ["calendars", calendar_id, "delete", "events", "eventid", event_id]
        if doit:
            argv.append("doit")
        argv += ["sendupdates", "none"]
        return argv

    # --- lifecycle (offboarding) -------------------------------------------------------
    @staticmethod
    def reset_password(email: str) -> List[str]:
        # Random password + no change-prompt: locks sign-in while the mailbox stays live.
        return ["update", "user", email, "password", "random", "changepassword", "off"]

    @staticmethod
    def signout_user(email: str) -> List[str]:
        return ["user", email, "signout"]

    @staticmethod
    def create_datatransfer(old_owner: str, service: str, new_owner: str) -> List[str]:
        # service: "drive" (Drive & Docs) | "calendar".
        return ["create", "datatransfer", old_owner, service, new_owner]

    @staticmethod
    def print_datatransfers(old_owner: str = "") -> List[str]:
        # Transfers are async; the CSV carries `overallTransferStatusCode` (completed/inProgress/...).
        argv = ["print", "datatransfers"]
        if old_owner:
            argv += ["olduser", old_owner]
        return argv

    @staticmethod
    def remove_all_calendar_acls(email: str) -> List[str]:
        # Remove the departing user from EVERY other user's primary calendar (GAM loops all users).
        return ["all", "users", "delete", "calendaracls", "primary", email]

    @staticmethod
    def add_calendar_event(
        calendar: str, summary: str, start: str, end: str, description: str = "", attendee: str = ""
    ) -> List[str]:
        argv = ["user", calendar, "add", "event", "primary",
                "summary", summary, "start", "allday", start, "end", "allday", end]
        if description:
            argv += ["description", description]
        if attendee:
            argv += ["attendee", attendee]
        return argv

    @staticmethod
    def delete_user(email: str) -> List[str]:
        return ["delete", "user", email]

    @staticmethod
    def undelete_user(email: str) -> List[str]:
        return ["undelete", "user", email]

    # --- gmail: signature / delegate / forwarding / vacation --------------------------
    @staticmethod
    def set_signature(email: str, signature: str, html: bool = True) -> List[str]:
        argv = ["user", email, "signature", signature]
        if html:
            argv.append("html")
        return argv

    @staticmethod
    def show_signature(email: str) -> List[str]:
        # `show signature` returns text (no formatjson).
        return ["user", email, "show", "signature"]

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
        html: bool = True,
        start: Optional[str] = None,
        end: Optional[str] = None,
        contacts_only: bool = False,
        domain_only: bool = False,
    ) -> List[str]:
        argv = ["user", email, "vacation", "on", "subject", subject, "message", message]
        if html:
            argv.append("html")
        if contacts_only:
            argv.append("contactsonly")
        if domain_only:
            argv.append("domainonly")
        if start:
            argv += ["start", start]
        if end:
            argv += ["end", end]
        return argv

    @staticmethod
    def vacation_off(email: str) -> List[str]:
        return ["user", email, "vacation", "off"]

    # --- gmail: forwarding ------------------------------------------------------------
    FORWARD_ACTIONS = ("keep", "archive", "markread", "trash", "delete")

    @staticmethod
    def add_forwarding_address(email: str, address: str) -> List[str]:
        return ["user", email, "add", "forwardingaddress", address]

    @staticmethod
    def delete_forwarding_address(email: str, address: str) -> List[str]:
        return ["user", email, "delete", "forwardingaddress", address]

    @staticmethod
    def print_forwarding_addresses(email: str) -> List[str]:
        return ["user", email, "print", "forwardingaddresses"]

    @staticmethod
    def set_forward(email: str, address: str, action: str = "keep") -> List[str]:
        # Forward to an already-added/verified forwarding address; `action` is what to do with the
        # original copy (keep | archive | markread | trash | delete).
        if action not in GAMCommands.FORWARD_ACTIONS:
            raise ValueError(f"invalid forward action: {action!r}")
        return ["user", email, "forward", "on", action, address]

    @staticmethod
    def forward_off(email: str) -> List[str]:
        return ["user", email, "forward", "off"]

    # --- aliases ----------------------------------------------------------------------
    @staticmethod
    def create_user_alias(alias: str, email: str) -> List[str]:
        return ["create", "alias", alias, "user", email]

    @staticmethod
    def delete_alias(alias: str) -> List[str]:
        return ["delete", "alias", alias]

    @staticmethod
    def todrive_args(user: str = "", title: str = "") -> List[str]:
        # Append to a `print …` command to export its CSV to a Google Sheet. `user` = whose Drive
        # owns the sheet (blank = the admin/oauth account's Drive); `title` names it.
        argv = ["todrive"]
        if user:
            argv += ["tduser", user]
        if title:
            argv += ["tdtitle", title]
        return argv

    @staticmethod
    def show_vacation(email: str) -> List[str]:
        # `show vacation` does NOT support formatjson — returns parseable text.
        return ["user", email, "show", "vacation"]

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
    def print_groups_member(email: str) -> List[str]:
        # Groups that <email> belongs to. Returns CSV with an `email` column.
        return ["print", "groups", "member", email]

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

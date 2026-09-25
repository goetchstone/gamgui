"""The Google Workspace connector — the only one implemented in the MVP.

It translates high-level operations into GAM commands (via :mod:`gamgui.core.gam.commands`), runs
them through the :class:`GAMRunner`, parses the output, and records mutations to the audit log.
The rest of the app talks to this object and never sees GAM syntax.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..audit import AuditLog, redact_secrets
from ..gam.commands import GAMCommands, build_user_query
from ..gam.errors import GAMError, GAMErrorKind
from ..gam.models import (
    CalendarACL,
    CalendarEvent,
    GAMGroup,
    GAMUser,
    GroupMember,
    ResourceCalendar,
    UserCalendar,
    Vacation,
)
from ..calendar_index import IndexedCalendar
from ..gam.parser import parse_one, parse_records
from ..gam.runner import DOMAIN_WIDE_TIMEOUT, GAMRunner
from .base import (
    Capability,
    ChangePreview,
    ChangeResult,
    ConnectionStatus,
    Connector,
    ConnectorID,
    LifecycleAction,
    RiskLevel,
)
from .person import ConnectorAccount, Person

# The remediation for a write that failed before GAM could say why (no binary, a Keychain error).
_WRITE_FAILED = "Something went wrong talking to GAM. See details below."
# The keys of a `print domains` record that name a domain (a flattened CSV column ends in one of them).
_DOMAIN_KEYS = {"domainName", "domainAliasName", "parentDomainName"}

# The audit error for a write cut off by the app quitting (its job task cancelled) mid-call.
# True whether it came during the call (the runner has stopped gam) or while waiting for the write lock.
INTERRUPTED = ("interrupted (the app quit or the job was cancelled) before GAM reported a result; "
               "it may have done part of its work")

# The per-user lines the offboarding calendar sweep expects (remove_from_all_calendars): a user who
# never shared with the leaver, a user without Calendar, the leaver's own calendar. Nothing else.
SWEEP_TOLERATED = (GAMErrorKind.NOT_FOUND, GAMErrorKind.SERVICE_NOT_ENABLED, GAMErrorKind.OWN_ACL)


def _csv_from(out: str) -> str:
    """Drop GAM progress lines before the CSV header (`gam report` prints status text first)."""
    lines = (out or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip().lower().startswith("email,"):
            return "\n".join(lines[i:])
    return out or ""


def _parse_signature(text: str) -> str:
    """Pull the signature body out of ``gam user X show signature`` text output.

    Format is ``Signature:`` followed by indented lines (or ``None`` when empty).
    """
    lines = (text or "").splitlines()
    body: List[str] = []
    capturing = False
    for ln in lines:
        if capturing:
            # stop at the next non-indented line (e.g. another "SendAs Address:")
            if ln.strip() and not ln.startswith(" "):
                break
            body.append(ln.strip())
        elif ln.strip().rstrip(":") == "Signature":
            capturing = True
    sig = "\n".join(body).strip()
    return "" if sig in ("", "None") else sig


def _require_read(cmd) -> None:
    if cmd.risk != RiskLevel.READ_ONLY:
        raise ValueError(f"{cmd.id} is not a read-only command")


class GAMConnector(Connector):
    id = ConnectorID.GOOGLE_WORKSPACE
    capabilities = {Capability.DIRECTORY, Capability.GROUPS, Capability.MAIL}

    def __init__(self, runner: GAMRunner, domain: str, audit: Optional[AuditLog] = None) -> None:
        self.runner = runner
        self.domain = domain
        self.audit = audit or AuditLog()

    # --- connection --------------------------------------------------------------------
    async def test(self) -> ConnectionStatus:
        version = ""
        try:
            version = await self.runner.version()
        except Exception as exc:  # binary missing, etc.
            return ConnectionStatus(ok=False, detail=str(exc))
        if not self.runner.vault.has_credentials(self.domain):
            return ConnectionStatus(ok=False, detail="Not configured — complete setup.", version=version)
        return ConnectionStatus(ok=True, detail="Ready.", version=version)

    # --- reads -------------------------------------------------------------------------
    async def list_users(
        self,
        search: str = "",
        include_suspended: bool = True,
        fields: Optional[Sequence[str]] = None,
    ) -> List[GAMUser]:
        query = build_user_query(search, include_suspended)
        argv = GAMCommands.print_users(query=query, fields=fields)
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return [GAMUser.from_json(r) for r in parse_records(stdout)]

    async def get_user(self, email: str, fields: Optional[Sequence[str]] = None) -> GAMUser:
        argv = GAMCommands.info_user(email, fields=fields)
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return GAMUser.from_json(parse_one(stdout))

    async def primary_address(self, email: str) -> Optional[str]:
        """The primary address GAM resolves ``email`` to — an alias (any domain's) resolves to the
        account that owns it — or None when no user has it. Read-only; other failures raise."""
        try:
            return (await self.get_user(email)).primary_email
        except GAMError as exc:
            if exc.kind == GAMErrorKind.NOT_FOUND:
                return None
            raise

    async def list_groups(self) -> List[GAMGroup]:
        argv = GAMCommands.print_groups()
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return [GAMGroup.from_json(r) for r in parse_records(stdout)]

    async def list_domains(self) -> List[str]:
        """Every domain the tenant's addresses can end in — primary, secondaries and their domain aliases —
        lowercased, from one ``gam print domains``. Raises as any read does."""
        stdout = await self.runner.run_authenticated(self.domain, GAMCommands.print_domains())
        found: List[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, val in node.items():
                    if key.rsplit(".", 1)[-1] in _DOMAIN_KEYS and isinstance(val, str) and val.strip():
                        found.append(val.strip().lower())
                    else:
                        walk(val)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(parse_records(stdout))
        return list(dict.fromkeys(found))

    async def list_group_members(self, group: str) -> List[GroupMember]:
        argv = GAMCommands.print_group_members(group)
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return [GroupMember.from_json(r) for r in parse_records(stdout)]

    async def list_delegates(self, email: str) -> List[str]:
        """Return the email addresses delegated access to ``email``'s mailbox."""
        stdout = await self.runner.run_authenticated(self.domain, GAMCommands.print_delegates(email))
        out: List[str] = []
        for rec in parse_records(stdout):
            addr = rec.get("delegateAddress") or rec.get("delegate") or rec.get("Delegate Address")
            if addr:
                out.append(str(addr))
        return out

    async def resolve(self, person: Person) -> Optional[ConnectorAccount]:
        try:
            user = await self.get_user(person.primary_email)
        except Exception:
            return None
        return ConnectorAccount(connector_id=self.id, native_id=user.primary_email, raw=user.raw)

    # --- low-risk mutations (run directly) ---------------------------------------------
    async def set_signature(self, email: str, signature: str, html: bool = True) -> ChangeResult:
        argv = GAMCommands.set_signature(email, signature, html=html)
        return await self._run_write("set_signature", email, argv, RiskLevel.LOW)

    async def create_user(self, email: str, first_name: str, last_name: str, password: str,
                          change_password: bool = True, org_unit: Optional[str] = None,
                          notify: Optional[str] = None) -> ChangeResult:
        """Create a Google Workspace account. The temp ``password`` is redacted before it is audited or
        surfaced — it only leaves the app on the printable credentials sheet, or (when ``notify`` is set)
        in the sign-in email GAM/Google sends straight to that address."""
        argv = GAMCommands.create_user(email, first_name, last_name, password, change_password, org_unit, notify)
        # "********" lands in both the password and notifypassword positions of the redacted copy.
        redacted = GAMCommands.create_user(email, first_name, last_name, "********", change_password, org_unit, notify)
        return await self._run_write("create_user", email, argv, RiskLevel.LOW, audit_argv=redacted,
                                     secrets=[password])

    async def get_signature(self, email: str) -> str:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.show_signature(email))
        return _parse_signature(out)

    async def list_user_groups(self, email: str) -> List[str]:
        """Group emails that ``email`` is a member of."""
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_groups_member(email))
        return [str(r.get("email")) for r in parse_records(out) if r.get("email") and "@" in str(r.get("email"))]

    async def usage_report(self, params: Sequence[str], max_lookback: int = 6) -> dict:
        """Per-user usage (storage/mail/drive). Usage lags ~2-3 days, so walk back to a date with data."""
        today = datetime.now(timezone.utc).date()
        for back in range(2, 2 + max_lookback):
            date = (today - timedelta(days=back)).isoformat()
            out = await self.runner.run_authenticated(self.domain, GAMCommands.report_users(date, params))
            rows = parse_records(_csv_from(out))
            if rows:
                return {"date": date, "rows": rows}
        return {"date": "", "rows": []}

    async def add_delegate(self, email: str, delegate: str) -> ChangeResult:
        argv = GAMCommands.add_delegate(email, delegate)
        return await self._run_write("add_delegate", email, argv, RiskLevel.LOW)

    async def remove_delegate(self, email: str, delegate: str) -> ChangeResult:
        argv = GAMCommands.remove_delegate(email, delegate)
        return await self._run_write("remove_delegate", email, argv, RiskLevel.LOW)

    async def signout_user(self, email: str) -> ChangeResult:
        return await self._run_write("signout_user", email, GAMCommands.signout_user(email), RiskLevel.LOW)

    # --- vacation / auto-responder -----------------------------------------------------
    async def get_vacation(self, email: str) -> Vacation:
        stdout = await self.runner.run_authenticated(self.domain, GAMCommands.show_vacation(email))
        return Vacation.from_show_text(stdout)

    async def set_vacation(
        self,
        email: str,
        subject: str,
        message: str,
        html: bool = True,
        start: Optional[str] = None,
        end: Optional[str] = None,
        contacts_only: bool = False,
        domain_only: bool = False,
    ) -> ChangeResult:
        argv = GAMCommands.set_vacation(
            email, subject, message, html=html, start=start, end=end,
            contacts_only=contacts_only, domain_only=domain_only,
        )
        return await self._run_write("set_vacation", email, argv, RiskLevel.LOW)

    async def clear_vacation(self, email: str) -> ChangeResult:
        return await self._run_write("clear_vacation", email, GAMCommands.vacation_off(email), RiskLevel.LOW)

    async def add_group_member(self, group: str, member: str, role: str = "member") -> ChangeResult:
        argv = GAMCommands.add_group_member(group, member, role=role)
        return await self._run_write("add_group_member", member, argv, RiskLevel.LOW, about={"group": group})

    async def remove_group_member(self, group: str, member: str) -> ChangeResult:
        argv = GAMCommands.remove_group_member(group, member)
        return await self._run_write("remove_group_member", member, argv, RiskLevel.LOW, about={"group": group})

    # --- directory profile (title = role, department) -----------------------------------
    async def set_organization(self, email: str, title: str = "", department: str = "") -> ChangeResult:
        argv = GAMCommands.update_organization(email, title=title, department=department)
        return await self._run_write("set_organization", email, argv, RiskLevel.LOW)

    # --- calendar access ---------------------------------------------------------------
    async def list_calendar_acls(self, email: str, calendar: str = "primary") -> List[CalendarACL]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_calendar_acls(email, calendar))
        return [CalendarACL.from_json(r) for r in parse_records(out)]

    async def add_calendar_acl(self, email: str, target: str, role: str = "reader", calendar: str = "primary") -> ChangeResult:
        argv = GAMCommands.add_calendar_acl(email, target, role=role, calendar=calendar)
        return await self._run_write("add_calendar_acl", email, argv, RiskLevel.LOW, about={"scope": target})

    async def remove_calendar_acl(self, email: str, scope: str, calendar: str = "primary") -> ChangeResult:
        argv = GAMCommands.delete_calendar_acl(email, scope, calendar=calendar)
        return await self._run_write("remove_calendar_acl", email, argv, RiskLevel.LOW, about={"scope": scope})

    # --- calendars / resources / events ------------------------------------------------
    async def list_resources(self, query: str = "") -> List[ResourceCalendar]:
        # GAM's resource `query` is a structured filter — freeform text like "Training Calendar"
        # fails with "Invalid Input: filter". Rooms are a small set, so fetch all and match locally.
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_resources())
        items = [ResourceCalendar.from_json(r) for r in parse_records(out)]
        q = query.strip().lower()
        if q:
            items = [r for r in items
                     if q in (r.name or "").lower() or q in (r.email or "").lower()
                     or q in (r.resource_id or "").lower()]
        return items

    async def list_user_calendars(self, email: str) -> List[UserCalendar]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_user_calendars(email))
        return [UserCalendar.from_json(r) for r in parse_records(out)]

    async def scan_all_calendars(self) -> List[IndexedCalendar]:
        """The full domain scan that backs the calendar index (slow; run in the background).

        Walks every user's calendar list (one ``all users print calendars`` call) plus the room
        calendars, keeping only the discoverable shared calendars: **secondary** calendars
        (``…@group.calendar.google.com``) and **rooms**. For each secondary calendar it records the
        owner (the user whose row has accessRole=owner) and a subscriber count (how many users carry
        it). Each user's primary, holiday/system calendars are skipped — they aren't shared calendars
        you'd search for, and excluding them keeps the index small on large tenants.
        """
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_all_calendars(),
                                                  timeout=DOMAIN_WIDE_TIMEOUT)
        agg: dict = {}
        for row in parse_records(out):
            cid = str(row.get("id") or "").strip()
            low = cid.lower()
            if not cid or "#" in low or not low.endswith("@group.calendar.google.com"):
                continue  # skip primaries (email ids), holiday/system, imports
            summary = str(row.get("summary") or "")
            role = str(row.get("accessRole") or row.get("accessrole") or "")
            who = str(row.get("primaryEmail") or row.get("User") or row.get("user") or "")
            e = agg.setdefault(cid, {"summary": summary, "owner": "", "subs": 0})
            if summary and not e["summary"]:
                e["summary"] = summary
            e["subs"] += 1
            if role == "owner" and who and not e["owner"]:
                e["owner"] = who
        cals = [IndexedCalendar(id=cid, summary=v["summary"], owner=v["owner"],
                                kind="secondary", subscribers=v["subs"]) for cid, v in agg.items()]
        # Room / resource calendars (one fast admin call). Isolated: a tenant without the Resource
        # Calendar API shouldn't waste the expensive user scan we just completed — index without rooms.
        try:
            for r in await self.list_resources(""):
                if r.email:
                    cals.append(IndexedCalendar(id=r.email, summary=r.name or r.email, owner="",
                                                kind="room", subscribers=0))
        except Exception:  # noqa: BLE001 — rooms are a bonus; keep the secondary-calendar index
            pass
        return cals

    async def list_calendar_acls_for(self, calendar_id: str) -> List[CalendarACL]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_calendar_acls_cal(calendar_id))
        return [CalendarACL.from_json(r) for r in parse_records(out)]

    async def add_calendar_acl_for(self, calendar_id: str, scope: str, role: str = "reader") -> ChangeResult:
        argv = GAMCommands.add_calendar_acl_cal(calendar_id, scope, role=role)
        return await self._run_write("add_calendar_acl_cal", calendar_id, argv, RiskLevel.LOW, about={"scope": scope})

    async def remove_calendar_acl_for(self, calendar_id: str, scope: str) -> ChangeResult:
        argv = GAMCommands.delete_calendar_acl_cal(calendar_id, scope)
        return await self._run_write("remove_calendar_acl_cal", calendar_id, argv, RiskLevel.LOW, about={"scope": scope})

    async def subscribe_calendar_for(self, email: str, calendar_id: str) -> ChangeResult:
        argv = GAMCommands.subscribe_calendar(email, calendar_id)
        return await self._run_write("subscribe_calendar", email, argv, RiskLevel.LOW, about={"calendar": calendar_id})

    async def search_events(
        self, calendar_id: str, query: str = "", after: str = "", before: str = "", cap: int = 200
    ) -> List[CalendarEvent]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_events(calendar_id, query, after, before))
        events = [CalendarEvent.from_json(r) for r in parse_records(out)]
        return events[:cap]

    async def get_event(self, calendar_id: str, event_id: str) -> Optional[CalendarEvent]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.get_event(calendar_id, event_id))
        recs = parse_records(out)
        return CalendarEvent.from_json(recs[0]) if recs else None

    async def delete_event(self, calendar_id: str, event_id: str) -> ChangeResult:
        argv = GAMCommands.delete_event(calendar_id, event_id, doit=True)
        return await self._run_write("delete_event", calendar_id, argv, RiskLevel.DESTRUCTIVE, about={"event": event_id})

    async def delete_calendar(self, owner: str, calendar_id: str) -> ChangeResult:
        """PERMANENTLY delete a secondary calendar (for everyone) by impersonating an owner.

        GAM verb is `remove calendars` (= Calendars.delete); `delete calendars` would only
        unsubscribe. Verified against GAM7 source. Irreversible — no GAM-side undo.
        """
        argv = GAMCommands.remove_calendar(owner, calendar_id)
        return await self._run_write("delete_calendar", calendar_id, argv, RiskLevel.DESTRUCTIVE, about={"owner": owner})

    # --- lifecycle (offboarding) -------------------------------------------------------
    async def reset_password(self, email: str) -> ChangeResult:
        # Only the reset: ending sessions is offboarding's own step (revoke_access), so its failure is
        # shown and can be re-run — as a follow-up here it was swallowed (failure-log 2026-09-23).
        return await self._run_write("reset_password", email, GAMCommands.reset_password(email), RiskLevel.LOW)

    async def revoke_access(self, email: str) -> ChangeResult:
        """Sign ``email`` out everywhere and revoke what outlives a password reset: app passwords,
        backup codes and every connected app's OAuth token (``deprovision signout``)."""
        return await self._run_write("revoke_access", email, GAMCommands.deprovision_user(email), RiskLevel.LOW)

    async def forward_off(self, email: str) -> ChangeResult:
        """Turn off automatic forwarding of ``email``'s incoming mail (harmless if it was off)."""
        return await self._run_write("forward_off", email, GAMCommands.forward_off(email), RiskLevel.LOW)

    async def transfer_data(self, old_owner: str, service: str, new_owner: str, privacy: str = "") -> ChangeResult:
        argv = GAMCommands.create_datatransfer(old_owner, service, new_owner, privacy=privacy)
        return await self._run_write("transfer_data", old_owner, argv, RiskLevel.LOW, about={"new_owner": new_owner})

    async def create_onboarding_runbook(self, assignee: str, title: str, steps: List[str]) -> dict:
        """Create a Google Tasks list on ``assignee`` with one task per step; return a summary.

        Additive/low-risk, serialized + audited. The tasklist id comes back via ``returnidonly``;
        each step then becomes a task on it. A step that fails is reported, not fatal. Quitting
        mid-build (a cancelled job) is audited as interrupted, with the tasks made so far, before the
        cancellation propagates — like ``_run_write`` does for a single write."""
        argv = GAMCommands.create_tasklist(assignee, title)
        try:
            out = await self.runner.run_authenticated(self.domain, argv, serialize=True)
        except asyncio.CancelledError:
            self.audit.record("onboard_runbook", target=assignee, argv=argv, ok=False,
                              extra={"title": title, "error": INTERRUPTED, "tasks": 0})
            raise
        except Exception as exc:  # noqa: BLE001 — record the attempt before it propagates
            self.audit.record("onboard_runbook", target=assignee, argv=argv, ok=False,
                              extra={"title": title, "error": str(exc)})
            raise
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        tasklist_id = lines[-1] if lines else ""
        created, failed = 0, []
        if tasklist_id:
            for step in steps:
                try:
                    await self.runner.run_authenticated(
                        self.domain, GAMCommands.create_task(assignee, tasklist_id, step), serialize=True)
                    created += 1
                except asyncio.CancelledError:
                    self.audit.record("onboard_runbook", target=assignee, argv=argv, ok=False,
                                      extra={"title": title, "error": INTERRUPTED, "tasks": created,
                                             "tasklist_id": tasklist_id})
                    raise
                except Exception:  # noqa: BLE001 — report per-step, keep going
                    failed.append(step)
        self.audit.record("onboard_runbook", target=assignee, argv=argv, ok=bool(tasklist_id),
                          extra={"title": title, "tasks": created, "failed": failed})
        return {"tasklist_id": tasklist_id, "created": created, "failed": failed, "total": len(steps)}

    async def send_welcome_email(self, to: str, subject: str, body: str) -> ChangeResult:
        return await self._run_write(
            "send_welcome_email", to, GAMCommands.send_email(to, subject, body), RiskLevel.LOW)

    async def remove_from_all_calendars(self, email: str) -> ChangeResult:
        # Best-effort sweep across every user. Expected, harmless per-user outcomes: NOT_FOUND (that
        # user never shared with the departing user), SERVICE_NOT_ENABLED (a user without Calendar —
        # `all users` is every active user) and OWN_ACL / cannotChangeOwnAcl (the departing user's OWN
        # primary calendar — you can't delete your own owner ACL, and it's going away with the account
        # anyway). Anything else fails the step: a real 403 for some user (PERMISSION_DENIED), an
        # auth/scope failure, an unrecognized line, a timeout that stopped it partway.
        argv = GAMCommands.remove_all_calendar_acls(email)
        return await self._run_write(
            "remove_from_all_calendars", email, argv, RiskLevel.LOW,
            tolerate_kinds=SWEEP_TOLERATED,
            timeout=DOMAIN_WIDE_TIMEOUT,
        )

    async def incomplete_transfers_for(self, email: str) -> List[dict]:
        """Data transfers FROM ``email`` that haven't reached 'completed' yet.

        Deleting an account before its Drive/calendar transfer completes permanently loses the
        un-transferred data, so the delete flow checks this first. Returns [] on any read error
        (never blocks deletion on an inability to check — just can't warn)."""
        try:
            out = await self.runner.run_authenticated(self.domain, GAMCommands.print_datatransfers(email))
        except Exception:
            return []
        pending = []
        for r in parse_records(out):
            status = str(r.get("overallTransferStatusCode") or r.get("status") or "")
            if status and status.lower() != "completed":
                pending.append({"application": str(r.get("application") or "data"), "status": status})
        return pending

    async def add_calendar_event(
        self, calendar: str, summary: str, start: str, end: str, description: str = "", attendee: str = ""
    ) -> ChangeResult:
        argv = GAMCommands.add_calendar_event(calendar, summary, start, end, description=description, attendee=attendee)
        return await self._run_write("add_calendar_event", calendar, argv, RiskLevel.LOW)

    async def delete_user(self, email: str) -> ChangeResult:
        return await self._run_write("delete_user", email, GAMCommands.delete_user(email), RiskLevel.DESTRUCTIVE)

    # --- the Builder's catalog reads ---------------------------------------------------
    async def catalog_read(self, cmd, argv: List[str], target: str = "") -> str:
        """Run a Builder catalog read and return its output. Refuses anything the catalog doesn't mark
        READ_ONLY (the Builder sends a write to ``apply``), so this can't become a second write path.

        A ``cmd.sensitive`` read (backup codes, browser tokens, any ``get`` download) is audited as
        ``sensitive_read``: which command, on whom, whether it ran — never the output, which is the
        secret itself."""
        _require_read(cmd)
        if not cmd.sensitive:
            return await self.runner.run_authenticated(self.domain, argv)
        extra = {"command": cmd.id}
        try:
            out = await self.runner.run_authenticated(self.domain, argv)
        except Exception as exc:
            self.audit.record("sensitive_read", target=target, argv=argv, ok=False,
                              extra={**extra, "error": str(exc)})
            raise
        self.audit.record("sensitive_read", target=target, argv=argv, ok=True, extra=extra)
        return out

    def audit_sensitive_csv(self, command: str, argv: List[str], target: str, rows: int) -> None:
        """The Builder's CSV download of a sensitive read's result: the secret leaves the app as a file,
        so it is recorded as ``sensitive_csv_export`` — the catalog command, on whom, how many rows.
        Never the rows. No GAM call: the rows are the ones ``catalog_read`` already returned."""
        self.audit.record("sensitive_csv_export", target=target, argv=argv, ok=True,
                          extra={"command": command, "rows": rows})

    async def export_to_sheet(self, cmd, argv: List[str], owner: str = "", title: str = "") -> ChangeResult:
        """Run a Builder read with ``todrive``: GAM writes the result to a new Google Sheet in
        ``owner``'s Drive (blank = the admin's). Creating a file is a write, so it runs through the
        chokepoint; ``output`` carries the Sheet URL GAM prints. A sensitive read's export is audited
        as ``sensitive_export`` so it files next to ``sensitive_read``."""
        _require_read(cmd)
        argv = list(argv) + GAMCommands.todrive_args(owner, title)
        action = "sensitive_export" if cmd.sensitive else "export_to_sheet"
        return await self._run_write(action, owner or "(admin Drive)", argv, RiskLevel.LOW)

    # --- destructive: plan (dry-run) then apply ----------------------------------------
    def plan_suspend(self, emails: Sequence[str], suspend: bool = True) -> List[ChangePreview]:
        """Build dry-run previews for (un)suspending a concrete set of users.

        GAM has no universal ``--dry-run``; the caller resolves the target set first (e.g. by
        expanding a query into emails), and this turns each into a previewable change.
        """
        risk = RiskLevel.DESTRUCTIVE if suspend else RiskLevel.LOW
        verb = "Suspend" if suspend else "Unsuspend"
        return [
            ChangePreview(
                connector_id=self.id,
                target=email,
                summary=f"{verb} {email}",
                risk=risk,
                argv=GAMCommands.set_suspended(email, suspend),
            )
            for email in emails
        ]

    async def plan(self, action: LifecycleAction, person: Person) -> List[ChangePreview]:
        if action == LifecycleAction.SUSPEND:
            return self.plan_suspend([person.primary_email], suspend=True)
        if action == LifecycleAction.UNSUSPEND:
            return self.plan_suspend([person.primary_email], suspend=False)
        # ONBOARD/OFFBOARD/UPDATE land in later phases.
        return []

    async def apply(self, changes: Sequence[ChangePreview]) -> List[ChangeResult]:
        results: List[ChangeResult] = []
        for change in changes:
            if change.connector_id != self.id or not change.argv:
                results.append(ChangeResult(preview=change, ok=False, detail="not applicable to this connector"))
                continue
            # A Builder change carries its catalog command id: the audit names the command, not just `apply`.
            command = str(change.meta.get("cid") or "")
            results.append(await self._run_write("apply", change.target, list(change.argv), change.risk,
                                                 about={"command": command}))
        return results

    # --- internals ---------------------------------------------------------------------
    async def _run_write(
        self,
        action: str,
        target: str,
        argv: List[str],
        risk: RiskLevel,
        about: Optional[Dict[str, str]] = None,
        tolerate_kinds: tuple = (),
        audit_argv: Optional[List[str]] = None,
        secrets: Sequence[str] = (),
        timeout: Optional[float] = None,
    ) -> ChangeResult:
        """Run a mutation; audit it. ``tolerate_kinds`` lists GAMErrorKinds that count as success for
        a *best-effort* bulk op (e.g. an all-users sweep where 'not found' / own-calendar are expected)
        — only when every error line GAM printed is one of them.

        ``audit_argv`` is what gets recorded and surfaced in the preview when the real ``argv`` carries
        a secret that must never touch the audit log or the UI — e.g. a create-user temp password.
        ``secrets`` are those values themselves: every occurrence is masked in the shown argv, the error
        text (GAM echoes the command line on a usage error) and the audit record. By value, so no
        neighbouring token can shift it; the positional masks in audit/errors are the second layer.
        ``timeout`` overrides the runner's default — a domain-wide (``all users``) call needs longer.
        ``about`` is what else the write concerned, recorded in the audit ``extra`` under the key that names
        it (``group``, ``scope``, ``event``, ``command``…) — once filed as ``group`` whatever it was."""
        named = {k: v for k, v in (about or {}).items() if v}
        shown = redact_secrets(audit_argv if audit_argv is not None else argv, secrets)   # never the raw secret
        preview = ChangePreview(connector_id=self.id, target=target, summary=action, risk=risk, argv=shown)
        try:
            out = await self.runner.run_authenticated(self.domain, argv, timeout=timeout, serialize=True)
        except asyncio.CancelledError:
            # Interrupted — quitting cancels an in-flight job (server._lifespan); the runner has
            # already stopped gam and wiped its dir. A BaseException, so `except Exception` below
            # never saw it and the write went unaudited, even an hour-long sweep that may have
            # changed half the domain. Record it, then let the cancellation finish.
            self.audit.record(
                action, target=target, argv=shown, ok=False,
                extra={**named, "error": INTERRUPTED, "tolerated": False},
                secrets=secrets,
            )
            raise
        except Exception as exc:
            # Every error line must be tolerable: a sweep's stderr holds one line per entity, and one
            # real failure among the benign notices is a failure (GAMError.kinds, not just .kind).
            kinds = getattr(exc, "kinds", None)
            tolerated = bool(tolerate_kinds and kinds and kinds <= set(tolerate_kinds))
            error = redact_secrets(str(exc), secrets)
            self.audit.record(
                action, target=target, argv=shown, ok=tolerated,
                extra={**named, "error": error, "tolerated": tolerated},
                secrets=secrets,
            )
            if tolerated:
                return ChangeResult(preview=preview, ok=True,
                                    detail="Completed (best-effort — per-user 'not shared', 'no Calendar' "
                                           "and own-calendar notices are expected and were skipped).")
            remediation = exc.remediation if isinstance(exc, GAMError) else _WRITE_FAILED
            return ChangeResult(preview=preview, ok=False, detail=error, remediation=remediation,
                                kind=exc.kind if isinstance(exc, GAMError) else None)
        self.audit.record(
            action, target=target, argv=shown, ok=True,
            extra=named or None,
            secrets=secrets,
        )
        return ChangeResult(preview=preview, ok=True, output=redact_secrets(out, secrets))

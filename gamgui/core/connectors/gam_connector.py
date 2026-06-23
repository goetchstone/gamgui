"""The Google Workspace connector — the only one implemented in the MVP.

It translates high-level operations into GAM commands (via :mod:`gamgui.core.gam.commands`), runs
them through the :class:`GAMRunner`, parses the output, and records mutations to the audit log.
The rest of the app talks to this object and never sees GAM syntax.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from ..audit import AuditLog
from ..gam.commands import GAMCommands, build_user_query
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
from ..gam.runner import GAMRunner
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

    async def list_groups(self) -> List[GAMGroup]:
        argv = GAMCommands.print_groups()
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return [GAMGroup.from_json(r) for r in parse_records(stdout)]

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
        return await self._run_write("add_group_member", member, argv, RiskLevel.LOW, target_extra=group)

    async def remove_group_member(self, group: str, member: str) -> ChangeResult:
        argv = GAMCommands.remove_group_member(group, member)
        return await self._run_write("remove_group_member", member, argv, RiskLevel.LOW, target_extra=group)

    # --- directory profile (title = role, department = store) --------------------------
    async def set_organization(self, email: str, title: str = "", department: str = "") -> ChangeResult:
        argv = GAMCommands.update_organization(email, title=title, department=department)
        return await self._run_write("set_organization", email, argv, RiskLevel.LOW)

    # --- calendar access ---------------------------------------------------------------
    async def list_calendar_acls(self, email: str, calendar: str = "primary") -> List[CalendarACL]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_calendar_acls(email, calendar))
        return [CalendarACL.from_json(r) for r in parse_records(out)]

    async def add_calendar_acl(self, email: str, target: str, role: str = "reader", calendar: str = "primary") -> ChangeResult:
        argv = GAMCommands.add_calendar_acl(email, target, role=role, calendar=calendar)
        return await self._run_write("add_calendar_acl", email, argv, RiskLevel.LOW, target_extra=target)

    async def remove_calendar_acl(self, email: str, scope: str, calendar: str = "primary") -> ChangeResult:
        argv = GAMCommands.delete_calendar_acl(email, scope, calendar=calendar)
        return await self._run_write("remove_calendar_acl", email, argv, RiskLevel.LOW, target_extra=scope)

    # --- calendars / resources / events ------------------------------------------------
    async def list_resources(self, query: str = "") -> List[ResourceCalendar]:
        # GAM's resource `query` is a structured filter — freeform text like "House Call Calendar"
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
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_all_calendars())
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
        return await self._run_write("delete_event", calendar_id, argv, RiskLevel.DESTRUCTIVE, target_extra=event_id)

    async def delete_calendar(self, owner: str, calendar_id: str) -> ChangeResult:
        """PERMANENTLY delete a secondary calendar (for everyone) by impersonating an owner.

        GAM verb is `remove calendars` (= Calendars.delete); `delete calendars` would only
        unsubscribe. Verified against GAM7 source. Irreversible — no GAM-side undo.
        """
        argv = GAMCommands.remove_calendar(owner, calendar_id)
        return await self._run_write("delete_calendar", calendar_id, argv, RiskLevel.DESTRUCTIVE, target_extra=owner)

    # --- lifecycle (offboarding) -------------------------------------------------------
    async def reset_password(self, email: str) -> ChangeResult:
        res = await self._run_write("reset_password", email, GAMCommands.reset_password(email), RiskLevel.LOW)
        if res.ok:  # best-effort: end existing sessions too
            try:
                await self.runner.run_authenticated(self.domain, GAMCommands.signout_user(email), serialize=True)
            except Exception:
                pass
        return res

    async def transfer_data(self, old_owner: str, service: str, new_owner: str) -> ChangeResult:
        argv = GAMCommands.create_datatransfer(old_owner, service, new_owner)
        return await self._run_write("transfer_data", old_owner, argv, RiskLevel.LOW, target_extra=new_owner)

    async def remove_from_all_calendars(self, email: str) -> ChangeResult:
        argv = GAMCommands.remove_all_calendar_acls(email)
        return await self._run_write("remove_from_all_calendars", email, argv, RiskLevel.LOW)

    async def add_calendar_event(
        self, calendar: str, summary: str, start: str, end: str, description: str = "", attendee: str = ""
    ) -> ChangeResult:
        argv = GAMCommands.add_calendar_event(calendar, summary, start, end, description=description, attendee=attendee)
        return await self._run_write("add_calendar_event", calendar, argv, RiskLevel.LOW)

    async def delete_user(self, email: str) -> ChangeResult:
        return await self._run_write("delete_user", email, GAMCommands.delete_user(email), RiskLevel.DESTRUCTIVE)

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
            results.append(await self._run_write("apply", change.target, list(change.argv), change.risk))
        return results

    # --- internals ---------------------------------------------------------------------
    async def _run_write(
        self,
        action: str,
        target: str,
        argv: List[str],
        risk: RiskLevel,
        target_extra: Optional[str] = None,
    ) -> ChangeResult:
        preview = ChangePreview(connector_id=self.id, target=target, summary=action, risk=risk, argv=argv)
        try:
            await self.runner.run_authenticated(self.domain, argv, serialize=True)
        except Exception as exc:
            self.audit.record(
                action, target=target, argv=argv, ok=False,
                extra={"error": str(exc), "group": target_extra} if target_extra else {"error": str(exc)},
            )
            return ChangeResult(preview=preview, ok=False, detail=str(exc))
        self.audit.record(
            action, target=target, argv=argv, ok=True,
            extra={"group": target_extra} if target_extra else None,
        )
        return ChangeResult(preview=preview, ok=True)

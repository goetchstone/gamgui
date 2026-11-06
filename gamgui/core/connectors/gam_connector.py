"""The Google Workspace connector — the only one implemented in the MVP.

It translates high-level operations into GAM commands (via :mod:`gamgui.core.gam.commands`), runs
them through the :class:`GAMRunner`, parses the output, and records mutations to the audit log.
The rest of the app talks to this object and never sees GAM syntax.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..audit import AuditLog, redact_secrets
from ..gam.commands import GAMCommands, MessageId, Phone, build_user_query, cut_off_account
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
from ..connected_apps import AppsRead, read_from
from ..gam.parser import parse_one, parse_records
from ..phishing import Incomplete, MessageCopies, MessageRecipients, SpamCopies, read_copies, read_recipients, read_spam_copies
from ..incident.check import (
    ACCOUNT_FIELDS,
    DRAFT_CAP,
    SCHEDULED_CAP,
    Account,
    AdminRole,
    Draft,
    Filter,
    Forwarding,
    ForwardingAddress,
    ScheduledSend,
    SendAs,
)
from ..phones import phones_from
from ..person_reads import (
    ChromeProfile,
    Computer,
    MobileDevice,
    chrome_profiles_from,
    computers_from,
    mobile_devices_from,
)
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


# ENTITY_DOES_NOT_EXIST_RC and SERVICE_NOT_APPLICABLE_RC: entityUnknownWarning, a warning in a per-account loop.
_ONE_ACCOUNT_WARNING_RCS = (56, 73)
PER_USER_RCS = (0, 50, 51, 60, 73)   # phishing.PER_USER_EXIT_CODES: read the lines; any other, the read says so


def _csv_from(out: str) -> str:
    """Drop GAM progress lines before the CSV header (`gam report` prints status text first)."""
    lines = (out or "").split("\n")   # not splitlines(): see strip_cfgdir_noise
    for i, ln in enumerate(lines):
        if ln.lstrip().lower().startswith("email,"):
            return "\n".join(lines[i:])
    return out or ""


def _parse_signature(text: str) -> str:
    """Pull the signature body out of ``gam user X show signature`` text output.

    Format is ``Signature:`` followed by indented lines (or ``None`` when empty).
    """
    lines = (text or "").split("\n")   # not splitlines(): a body holding U+2028 or a form feed read as ending there
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


def _usage_day(rows: List[Dict[str, Any]], asked: str) -> str:
    """The day a usage report's rows are from. GAM walks back to the newest day with data on its own
    (doReport, _checkDataRequiredServices, _adjustTryDate), so the day asked for can name a day the data
    isn't from; the rows' ``date`` column says. The day asked for only when no row has one."""
    return max((str(r.get("date") or "").strip() for r in rows), default="") or asked


def _require_read(cmd) -> None:
    if cmd.risk != RiskLevel.READ_ONLY:
        raise ValueError(f"{cmd.id} is not a read-only command")


# What `cut_off_refusal` reads of an account.
logger = logging.getLogger(__name__)

ACCOUNT_ID_FIELDS = ("primaryEmail", "id")
MESSAGES_TIMEOUT = 600.0    # thousands of drafts listed or deleted meet Gmail's per-user quota
UNANSWERED = "GAM returned nothing for this mailbox, so it may not have been read."


def _password_forms(password: str) -> List[str]:
    """Every way an echo might hold a generated password: whole, in another case, with its dashes as spaces, or any one of
    its groups, so a message quoting part of it can't reach the page or the audit log."""
    forms = {password, password.replace("-", " ")}
    forms |= {group for group in password.split("-") if group}
    return sorted(forms | {f.lower() for f in forms} | {f.upper() for f in forms}, key=len, reverse=True)


async def _told(connector: "GAMConnector", action: str, target: str, run, outcome):
    """Tell ``GAMConnector.on_write`` when a write starts and how it ended. It only listens: nothing it does can
    change or refuse the write, and the audit record stays the write's own."""
    ended = connector.on_write(action, target) if connector.on_write else None
    try:
        result = await run()
    except asyncio.CancelledError:
        if ended:
            ended(False, INTERRUPTED)
        raise
    except Exception:
        if ended:
            ended(False, _WRITE_FAILED)
        raise
    if ended:
        ended(*outcome(result))
    return result


def _watched(run_write):
    @functools.wraps(run_write)
    async def wrapper(self, action: str, target: str, *args, **kwargs) -> ChangeResult:
        return await _told(self, action, target, lambda: run_write(self, action, target, *args, **kwargs),
                           lambda result: (result.ok, result.remediation))
    return wrapper


def _runbook_outcome(result: dict) -> Tuple[bool, str]:
    if not result["tasklist_id"]:
        return False, _WRITE_FAILED
    return (False, f"{len(result['failed'])} of its tasks weren't added.") if result["failed"] else (True, "")


def _watched_runbook(build):
    @functools.wraps(build)
    async def wrapper(self, assignee: str, title: str, steps: List[str]) -> dict:
        return await _told(self, "onboard_runbook", assignee, lambda: build(self, assignee, title, steps), _runbook_outcome)
    return wrapper


class GAMConnector(Connector):
    id = ConnectorID.GOOGLE_WORKSPACE
    capabilities = {Capability.DIRECTORY, Capability.GROUPS, Capability.MAIL}

    def __init__(self, runner: GAMRunner, domain: str, audit: Optional[AuditLog] = None) -> None:
        self.runner = runner
        self.domain = domain
        self.audit = audit or AuditLog()
        self._confirmed_admin: Optional[Tuple[str, str]] = None   # (address, ID) GAM has agreed with
        # (action, target) as a write starts; what it returns is told (ok, what to do) as it ends. See _watched.
        self.on_write: Optional[Callable[[str, str], Optional[Callable[[bool, str], None]]]] = None

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

    async def chrome_profiles(self, email: str) -> List[ChromeProfile]:
        return chrome_profiles_from(parse_records(
            await self.runner.run_authenticated(self.domain, GAMCommands.print_chrome_profiles(email))), email)

    async def mobile_devices(self, email: str) -> List[MobileDevice]:
        return mobile_devices_from(parse_records(
            await self.runner.run_authenticated(self.domain, GAMCommands.print_mobile_devices(email))), email)

    async def computers(self, email: str) -> List[Computer]:
        return computers_from(parse_records(
            await self.runner.run_authenticated(self.domain, GAMCommands.print_devices(email))), email)

    async def connected_apps(self) -> AppsRead:
        """Every account's connected apps and AI agents, suspended accounts aside. One Directory API call per
        account, so it runs only when the operator asks, as a background job. GAM warns about an account it can't
        read (one deleted mid-run, say), carries on, writes its CSV and exits with that warning's code: every other
        account is there, so the read is kept and marked incomplete. Worked out off the event loop: on a large
        tenant this is hundreds of thousands of rows, and every page and poll waits on the loop."""
        complete = True
        try:
            out = await self.runner.run_authenticated(self.domain, GAMCommands.print_tokens(),
                                                      timeout=DOMAIN_WIDE_TIMEOUT)
        except GAMError as exc:
            rows = exc.stdout.strip().split("\n")
            # No row at all isn't a list missing one account: "no apps" would be a false all-clear.
            if exc.exit_code not in _ONE_ACCOUNT_WARNING_RCS or len(rows) < 2 or "clientId" not in rows[0]:
                raise
            out, complete = exc.stdout, False
        return await asyncio.to_thread(lambda: read_from(parse_records(out), complete))

    async def usage_report(self, params: Sequence[str], max_lookback: int = 6) -> dict:
        """Per-user usage (storage/mail/drive). Usage lags ~2-3 days, so walk back to a date with data."""
        today = datetime.now(timezone.utc).date()
        for back in range(2, 2 + max_lookback):
            date = (today - timedelta(days=back)).isoformat()
            out = await self.runner.run_authenticated(self.domain, GAMCommands.report_users(date, params))
            rows = parse_records(_csv_from(out))
            if rows:
                return {"date": _usage_day(rows, date), "rows": rows}
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
        except Exception:  # noqa: BLE001, S110 — rooms are a bonus; keep the secondary-calendar index
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

    async def find_message_copies(self, message: MessageId) -> MessageCopies:
        argv = GAMCommands.find_message_copies(message)
        res = await self.runner.run_any_exit(self.domain, argv, timeout=DOMAIN_WIDE_TIMEOUT)
        return await asyncio.to_thread(read_copies, res.returncode, res.stderr, argv)

    async def find_spam_copies(self, message: MessageId) -> SpamCopies:
        argv = GAMCommands.find_spam_copies(message)
        res = await self.runner.run_any_exit(self.domain, argv, timeout=DOMAIN_WIDE_TIMEOUT)

        def read() -> SpamCopies:
            records = parse_records(res.stdout) if res.returncode in PER_USER_RCS else []
            return read_spam_copies(res.returncode, res.stderr, records, argv, message)
        return await asyncio.to_thread(read)

    async def mailbox_copy(self, email: str, message: MessageId) -> Optional[MessageRecipients]:
        """One mailbox's copy of an email, Spam and Sent included, or None when it has none: the sender's for its To, Cc and Bcc,
        anyone's for what the ID matched. Only GAM's "none found" (60) means no copy: a refused token GAM skips without a word
        leaves 0 and no row."""
        argv = GAMCommands.search_messages(email, message.any_copy_query)
        res = await self.runner.run_any_exit(self.domain, argv)
        if res.returncode not in (0, 60):
            raise GAMError.from_run(res.returncode, res.stderr, argv, stdout=res.stdout)
        records = parse_records(res.stdout)
        if not records and res.returncode == 0:
            raise Incomplete("GAM returned nothing for this mailbox, so it may not have been read.")
        return read_recipients(records)

    async def trash_message(self, email: str, message: MessageId, copies: int, about: Dict[str, str]) -> ChangeResult:
        return await self._run_write("trash_message", email, GAMCommands.trash_message(email, message, copies),
                                     RiskLevel.DESTRUCTIVE, about=about)

    async def trash_messages(self, email: str, ids: Sequence[str], about: Dict[str, str]) -> ChangeResult:
        return await self._run_write("trash_spam_messages", email, GAMCommands.trash_messages(email, ids),
                                     RiskLevel.DESTRUCTIVE, about=about)

    async def clear_recovery(self, email: str) -> ChangeResult:
        return await self._run_write("clear_recovery", email, GAMCommands.clear_recovery(email), RiskLevel.DESTRUCTIVE)

    async def hide_from_directory(self, email: str) -> ChangeResult:
        return await self._run_write("hide_from_directory", email, GAMCommands.hide_from_directory(email), RiskLevel.LOW)

    async def imap_off(self, email: str) -> ChangeResult:
        return await self._run_write("imap_off", email, GAMCommands.imap_off(email), RiskLevel.LOW)

    async def pop_off(self, email: str) -> ChangeResult:
        return await self._run_write("pop_off", email, GAMCommands.pop_off(email), RiskLevel.LOW)

    async def transfer_calendar_ownership(self, email: str, calendar_ids: Sequence[str], new_owner: str) -> ChangeResult:
        return await self._run_write("transfer_calendar_ownership", email,
                                     GAMCommands.transfer_calendar_ownership(calendar_ids, new_owner), RiskLevel.DESTRUCTIVE,
                                     about={"new_owner": new_owner})

    async def delete_filters(self, email: str, filter_ids: Sequence[str]) -> ChangeResult:
        return await self._run_write("delete_filters", email, GAMCommands.delete_filters(email, filter_ids), RiskLevel.DESTRUCTIVE)

    async def owned_calendars(self, email: str) -> List[Dict[str, Any]]:
        return parse_records(await self.runner.run_authenticated(self.domain, GAMCommands.print_owned_calendars(email)))

    async def user_filters(self, email: str) -> List[Dict[str, Any]]:
        return parse_records(await self.runner.run_authenticated(self.domain, GAMCommands.print_filters(email)))

    async def vault_holds(self, email: str) -> List[Dict[str, Any]]:
        return parse_records(await self.runner.run_authenticated(self.domain, GAMCommands.print_vault_holds(email)))

    # --- what Check Account reads (core/incident) --------------------------------------
    async def account_to_check(self, email: str) -> Account:
        return Account.from_record(parse_one(await self.runner.run_authenticated(self.domain, GAMCommands.info_user(email, fields=ACCOUNT_FIELDS))))

    async def filters_of(self, email: str) -> List[Filter]:
        return [Filter.from_record(r) for r in await self.user_filters(email)]

    async def forwarding_of(self, email: str) -> Optional[Forwarding]:
        return Forwarding.from_records(parse_records(await self.runner.run_authenticated(self.domain, GAMCommands.print_forward(email))))

    async def forwarding_addresses_of(self, email: str) -> List[ForwardingAddress]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_forwarding_addresses(email))
        return [ForwardingAddress.from_record(r) for r in parse_records(out)]

    async def send_as_of(self, email: str) -> List[SendAs]:
        """Gmail always lists the primary, so an empty list is GAM skipping a refused token without a word, exit 0 (handleOAuthTokenError)."""
        rows = parse_records(await self.runner.run_authenticated(self.domain, GAMCommands.print_send_as(email)))
        if not rows:
            raise Incomplete(UNANSWERED)
        return [SendAs.from_record(r) for r in rows]

    async def auto_reply_of(self, email: str) -> Vacation:
        """As ``get_vacation``, for a check: GAM's silence is unknown, never an auto-reply that's off."""
        out = await self.runner.run_authenticated(self.domain, GAMCommands.show_vacation(email))
        if "Enabled:" not in out:
            raise Incomplete(UNANSWERED)
        return Vacation.from_show_text(out)

    async def admin_roles_of(self, email: str) -> List[AdminRole]:
        return [AdminRole.from_record(r) for r in parse_records(await self.runner.run_authenticated(self.domain, GAMCommands.print_admins(email)))]

    @staticmethod
    async def _listed(read: Awaitable[str]) -> List[Dict[str, Any]]:
        """One mailbox's listing. Only GAM's "none found" (60) means none: a refused token GAM skips without a word leaves 0 and no row."""
        try:
            out = await read
        except GAMError as exc:
            if exc.exit_code == 60:
                return []
            raise
        records = parse_records(out)
        if not records:
            raise Incomplete(UNANSWERED)
        return records

    async def drafts_of(self, email: str) -> List[Draft]:
        read = self.runner.run_authenticated(self.domain, GAMCommands.list_drafts(email, DRAFT_CAP), timeout=MESSAGES_TIMEOUT)
        return [Draft.from_record(r) for r in await self._listed(read)]

    async def scheduled_sends_of(self, email: str) -> List[ScheduledSend]:
        read = self.runner.run_authenticated(self.domain, GAMCommands.search_scheduled(email, SCHEDULED_CAP), timeout=MESSAGES_TIMEOUT)
        return [ScheduledSend.from_record(r) for r in await self._listed(read)]

    async def delete_send_as(self, email: str, address: str) -> ChangeResult:
        return await self._run_write("delete_send_as", email, GAMCommands.delete_send_as(email, address), RiskLevel.DESTRUCTIVE)

    async def delete_messages(self, email: str, ids: Sequence[str]) -> ChangeResult:
        def short_of(out: str) -> str:
            # GAM skips a batch on serviceNotAvailable without a word and exits 0, and a scheduled email in it still goes out.
            deleted = sum(", Deleted" in line for line in out.split("\n"))
            return "" if deleted >= len(ids) else f"GAM deleted {deleted} of {len(ids)} messages and said nothing about the rest: check the account again."

        return await self._run_write("delete_messages", email, GAMCommands.delete_messages(email, ids), RiskLevel.DESTRUCTIVE,
                                     timeout=MESSAGES_TIMEOUT, verify=short_of)

    async def user_phones(self, email: str) -> List[Phone]:
        """The person's phones as Google has them now: a change sends them all back."""
        out = await self.runner.run_authenticated(self.domain, GAMCommands.info_user(email, fields=["primaryEmail", "phones"]))
        return phones_from(parse_one(out))

    async def update_phones(self, email: str, phones: Sequence[Phone], risk: RiskLevel) -> ChangeResult:
        return await self._run_write("update_phones", email, GAMCommands.update_phones(email, phones), risk)

    async def set_temporary_password(self, email: str, password: str) -> ChangeResult:
        """Set a password to hand over, changed at their next sign-in. ``password`` is masked by value in what's shown,
        audited and in any error GAM echoes; only the argv GAM runs holds it. The admin's own is refused in _run_write."""
        return await self._run_write("set_password", email, GAMCommands.set_password(email, password), RiskLevel.DESTRUCTIVE,
                                     audit_argv=GAMCommands.set_password(email, "********"), secrets=_password_forms(password))

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

    @_watched_runbook
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

    # --- the connected admin ------------------------------------------------------------
    async def _admin_identity(self) -> Tuple[str, str, str]:
        """The admin GamGUI signs in as, asked now: (address, account ID, ""), or ("", "", why not). The ID
        is the token's ``sub``, kept only once GAM reads the same ``id`` for that address. A re-import
        changes the claims, and so asks again."""
        try:
            address = self.runner.vault.oauth_admin_email(self.domain)
            account_id = self.runner.vault.oauth_admin_id(self.domain)
        except Exception:  # noqa: BLE001 - a Keychain refusal: unconfirmed, and never its message
            return "", "", "its sign-in token couldn't be read"
        if not address or not account_id:
            return "", "", "its sign-in token doesn't say"
        if self._confirmed_admin == (address, account_id):
            return address, account_id, ""
        try:
            record = parse_one(await self.runner.run_authenticated(
                self.domain, GAMCommands.info_user(address, fields=ACCOUNT_ID_FIELDS)))
        except Exception as exc:  # noqa: BLE001 - any failed read leaves the admin unconfirmed
            return "", "", f"GAM couldn't read {address}: {exc}"
        if record.get("id") != account_id:
            return "", "", f"GAM reads {address} as another account than the sign-in token's"
        self._confirmed_admin = (address, account_id)
        return address, account_id, ""

    async def cut_off_refusal(self, commands: Iterable[Sequence[str]], *, previewing: bool = False) -> str:
        """Why ``commands`` must not run, or "": any that would cut off the admin GamGUI signs in as signs the
        app out with no way back from here. Compared by Google account ID: GAM reads each address as it will
        be sent, and an alias or a renamed address reads as the account behind it. Only an account GAM reads
        now, with an ID, can be cut off; anything else refuses, at the cost of a write GAM would refuse anyway.
        ``_run_write`` asks before every such write, whoever called it. A preview asks too, to say so early
        (``previewing``); there an address no one holds passes, since an earlier step of a sequence may restore
        it, and the run reads it again when that step has run."""
        addresses = list(dict.fromkeys(a for a in map(cut_off_account, commands) if a is not None))
        if not addresses:
            return ""
        admin, admin_id, why_not = await self._admin_identity()
        if not admin:
            return (f"GamGUI can't confirm which account it signs in as ({why_not}), so a change that could sign it "
                    "out isn't made here. Import the credentials again on Setup, or make the change in the Admin console.")
        own = ": this would sign GamGUI out, with no way back from here. Make the change in the Admin console, or from another admin's setup."
        for address in addresses:
            shown = address.strip()
            if shown.lower() == admin:
                return f"{shown} is the admin GamGUI is connected as{own}"
            try:
                records = parse_records(await self.runner.run_authenticated(
                    self.domain, GAMCommands.info_user(address, fields=ACCOUNT_ID_FIELDS)))
            except GAMError as exc:
                if exc.kind == GAMErrorKind.NOT_FOUND:
                    if previewing:
                        continue
                    return f"GAM has no account {shown}, so nothing ran. Check the address and preview again."
                return f"Couldn't check whose account {shown} is ({exc}), so nothing ran. Preview again."
            except Exception as exc:  # noqa: BLE001 - fail closed: an account it can't read can't be ruled out
                return f"Couldn't check whose account {shown} is ({exc}), so nothing ran. Preview again."
            # One record with Google's digits for an ID, or it says nothing about whose account this is.
            account_id = records[0].get("id") if len(records) == 1 else None
            if not (isinstance(account_id, str) and account_id.isascii() and account_id.isdigit()):
                return f"GAM didn't say whose account {shown} is, so nothing ran. Preview again."
            if account_id == admin_id or str(records[0].get("primaryEmail") or "").strip().lower() == admin:
                return f"{shown} is the account GamGUI is connected as ({admin}){own}"
        return ""

    # --- internals ---------------------------------------------------------------------
    @_watched
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
        verify: Optional[Callable[[str], str]] = None,
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
        it (``group``, ``scope``, ``event``, ``command``…) — once filed as ``group`` whatever it was.
        ``verify`` reads the output of a run that exited 0, for a command GAM can leave undone without a word: what it says is the failure."""
        named = {k: v for k, v in (about or {}).items() if v}
        shown = redact_secrets(audit_argv if audit_argv is not None else argv, secrets)   # never the raw secret
        preview = ChangePreview(connector_id=self.id, target=target, summary=action, risk=risk, argv=shown)
        try:
            refusal = await self.cut_off_refusal([argv])
        except Exception as exc:  # noqa: BLE001 - fail closed, and on the record: the check itself broke
            refusal = f"Couldn't check whose account this is ({exc}), so nothing ran."
        if refusal:
            self.audit.record(action, target=target, argv=shown, ok=False,
                              extra={**named, "error": refusal, "tolerated": False}, secrets=secrets)
            refusal = redact_secrets(refusal, secrets)
            return ChangeResult(preview=preview, ok=False, detail=refusal, remediation=refusal)
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
        if verify is not None and (short := redact_secrets(verify(out), secrets)):
            self.audit.record(action, target=target, argv=shown, ok=False, extra={**named, "error": short, "tolerated": False}, secrets=secrets)
            return ChangeResult(preview=preview, ok=False, detail=short, remediation=short)
        unrecorded = ""
        try:
            self.audit.record(
                action, target=target, argv=shown, ok=True,
                extra=named or None,
                secrets=secrets,
            )
        except OSError as exc:
            # The write happened, so it must not read as failed: a temporary password would be lost with nobody told.
            logger.error("audit record failed after %s succeeded: %s", action, type(exc).__name__)
            unrecorded = f"It worked, but the audit log couldn't record it ({exc.strerror or type(exc).__name__})."
        return ChangeResult(preview=preview, ok=True, detail=unrecorded, output=redact_secrets(out, secrets))

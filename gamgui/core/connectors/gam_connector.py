"""The Google Workspace connector — the only one implemented in the MVP.

It translates high-level operations into GAM commands (via :mod:`gamgui.core.gam.commands`), runs
them through the :class:`GAMRunner`, parses the output, and records mutations to the audit log.
The rest of the app talks to this object and never sees GAM syntax.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ..audit import AuditLog
from ..gam.commands import GAMCommands, build_user_query
from ..gam.models import GAMGroup, GAMUser, GroupMember
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

    async def add_delegate(self, email: str, delegate: str) -> ChangeResult:
        argv = GAMCommands.add_delegate(email, delegate)
        return await self._run_write("add_delegate", email, argv, RiskLevel.LOW)

    async def remove_delegate(self, email: str, delegate: str) -> ChangeResult:
        argv = GAMCommands.remove_delegate(email, delegate)
        return await self._run_write("remove_delegate", email, argv, RiskLevel.LOW)

    async def add_group_member(self, group: str, member: str, role: str = "member") -> ChangeResult:
        argv = GAMCommands.add_group_member(group, member, role=role)
        return await self._run_write("add_group_member", member, argv, RiskLevel.LOW, target_extra=group)

    async def remove_group_member(self, group: str, member: str) -> ChangeResult:
        argv = GAMCommands.remove_group_member(group, member)
        return await self._run_write("remove_group_member", member, argv, RiskLevel.LOW, target_extra=group)

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

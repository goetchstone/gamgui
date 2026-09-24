"""A bulk loop stops at a failure every later call would share, instead of repeating it per target.

An expired admin sign-in, a missing API scope or an unconfigured GAM fails each remaining user the
same way, so a domain-wide signature apply once kept looping and buried the one cause under
thousands of identical failures. Each loop that runs one write per target reads
``ChangeResult.kind`` and stops on an ``ACCOUNT_WIDE_KINDS`` kind with the remediation, saying how
many were not attempted; any other failure (one user not found, one refusal) keeps going.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel
from gamgui.core.gam.errors import ACCOUNT_WIDE_KINDS, GAMErrorKind
from gamgui.core.gam.models import GAMUser

EXPIRED = "Your sign-in expired. Re-run setup to refresh authorization."


class _Conn:
    """Every write fails with ``kind``; records who was attempted."""

    def __init__(self, kind: GAMErrorKind, remediation: str = EXPIRED) -> None:
        self.kind, self.remediation, self.attempted = kind, remediation, []

    async def _write(self, target: str) -> ChangeResult:
        self.attempted.append(target)
        preview = ChangePreview(ConnectorID.GOOGLE_WORKSPACE, target, "write", RiskLevel.LOW)
        return ChangeResult(preview=preview, ok=False, detail="ERROR: invalid_grant: Token has been expired",
                            remediation=self.remediation, kind=self.kind)

    async def set_signature(self, email, body, html=True):
        return await self._write(email)

    async def set_organization(self, email, title="", department=""):
        return await self._write(email)

    async def subscribe_calendar_for(self, email, cal):
        return await self._write(email)

    async def create_user(self, email, *a, **k):
        return await self._write(email)


USERS = [GAMUser(primary_email=f"u{i}@example.com", given_name="U", family_name=str(i)) for i in range(3)]
EMAILS = [u.primary_email for u in USERS]


def test_the_account_wide_kinds():
    assert ACCOUNT_WIDE_KINDS == {GAMErrorKind.AUTH_EXPIRED, GAMErrorKind.NOT_AUTHENTICATED,
                                  GAMErrorKind.SCOPE_MISSING}


async def _signatures(conn):
    from gamgui.web.jobs import Job
    from gamgui.web.routes.signatures import _run_apply
    job = Job(id="j", total=len(USERS))
    await _run_apply(job, conn, USERS, "{name}")
    return job


async def _department(conn):
    from gamgui.core.bulk import set_departments
    from gamgui.web.jobs import BatchJob
    job = BatchJob(id="j", total=len(USERS))
    await set_departments(job, conn, USERS, "Sales")
    return job


async def _subscribe(conn):
    from gamgui.web.jobs import BatchJob
    from gamgui.web.routes.calendars import _run_subscribe
    job = BatchJob(id="j", total=len(EMAILS))
    await _run_subscribe(job, conn, "c_1@group.calendar.google.com", EMAILS)
    return job


async def _onboard(conn):
    from gamgui.web.routes.onboarding import OnboardJob, _run_bulk_onboard
    cfg = SimpleNamespace(steps=["Order a laptop"], org_unit="", signature="", groups=[], calendars=[])
    rows = [{"role": "Sales", "email": e, "name": "New Hire", "create_account": True} for e in EMAILS]
    job = OnboardJob(id="j", total=len(rows))
    await _run_bulk_onboard(job, conn, None, None, [(row, cfg) for row in rows])
    return job


LOOPS = {"signatures": _signatures, "department": _department, "subscribe": _subscribe, "onboard": _onboard}


@pytest.mark.parametrize("loop", sorted(LOOPS))
@pytest.mark.parametrize("kind", sorted(ACCOUNT_WIDE_KINDS, key=lambda k: k.value))
async def test_a_bulk_loop_stops_at_an_account_wide_failure(loop, kind):
    conn = _Conn(kind)
    job = await LOOPS[loop](conn)
    assert conn.attempted == EMAILS[:1], "kept going after a failure every later call shares"
    assert job.finished and job.done == 1
    assert EXPIRED in job.error and "remaining 2 were not attempted" in job.error


@pytest.mark.parametrize("loop", sorted(LOOPS))
async def test_a_per_target_failure_does_not_stop_a_bulk_loop(loop):
    conn = _Conn(GAMErrorKind.NOT_FOUND, "The requested user, group, or resource was not found.")
    job = await LOOPS[loop](conn)
    assert conn.attempted == EMAILS and job.done == 3 and job.error is None

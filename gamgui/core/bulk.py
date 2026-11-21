"""Per-user bulk loops: the stop rules every one shares, the bulk department, and the loops over addresses.

A loop writes into the polled progress record the web layer starts it with (``web/jobs.py``: ``record``,
``current``, ``error``, ``finish``) — passed in, since core never imports web.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from .gam.errors import ACCOUNT_WIDE_KINDS, GAMError

# A failure that isn't GAM's own has no remediation to show: the Users screen's line for one.
_TRY_AGAIN = "Something went wrong talking to GAM. Please try again."


def stop_reason(kind, remediation: str, left: int) -> Optional[str]:
    """Why a bulk loop stops here, or None to carry on. A failure of an ``ACCOUNT_WIDE_KINDS`` kind
    (sign-in expired, GAM not set up, a scope not granted) fails every remaining target the same way,
    so running on only buries the one cause under ``left`` identical failures. Every per-user loop
    (signatures, bulk department, calendar fan-out, bulk onboarding) asks this after each write."""
    if kind not in ACCOUNT_WIDE_KINDS:
        return None
    rest = f" The remaining {left} {'was' if left == 1 else 'were'} not attempted." if left > 0 else ""
    return f"Stopped: {remediation}{rest}"


def stop_requested(job) -> bool:
    """True, with ``job.error`` saying so, once the operator pressed Stop (plan U5). Every loop asks
    before its next target, never during one, so the write in flight finishes and is recorded."""
    if not job.cancel_requested:
        return False
    job.error = f"Stopped by you — {job.total - job.done} not attempted."
    return True


def _why(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else _TRY_AGAIN


async def set_departments(job, conn, users, department: str,
                          on_set: Optional[Callable[[Any], None]] = None) -> None:
    """Set ``department`` on each of ``users``, KEEPING each existing title; ``on_set(user)`` after
    each write GAM accepted (the web layer patches its cached record). Stops at a failure every later
    user would share (``stop_reason``), or at Stop."""
    try:
        for u in users:
            if stop_requested(job):
                break
            job.current = u.primary_email
            kind, why, detail = None, "", ""
            try:
                res = await conn.set_organization(u.primary_email, title=u.title or "", department=department)
                ok = bool(getattr(res, "ok", False))
                if not ok:
                    kind, why, detail = getattr(res, "kind", None), getattr(res, "remediation", ""), getattr(res, "detail", "")
            except Exception as exc:  # noqa: BLE001 — one user must not stop the rest
                ok, kind, why, detail = False, getattr(exc, "kind", None), _why(exc), str(exc)
            job.record(u.primary_email, ok, why, detail)
            if ok and on_set is not None:
                on_set(u)
            stop = stop_reason(kind, why, job.total - job.done)
            if stop:
                job.error = stop
                break
    except Exception as exc:
        job.error = _why(exc)
    finally:
        job.finish()


async def _each(job, emails, write: Callable[[str], Awaitable[Any]],
                on_set: Optional[Callable[[str], None]] = None) -> None:
    """Run ``write(email)`` for each of ``emails`` into ``job``; ``on_set(email)`` after each write GAM accepted. Stops at a
    failure every later one would share (``stop_reason``), or at Stop. A failure of one never stops the rest."""
    try:
        for email in emails:
            if stop_requested(job):
                break
            job.current = email
            kind, why, detail = None, "", ""
            try:
                res = await write(email)
                ok = bool(getattr(res, "ok", False))
                if not ok:
                    kind, why, detail = getattr(res, "kind", None), getattr(res, "remediation", ""), getattr(res, "detail", "")
            except Exception as exc:  # noqa: BLE001 — one target must not stop the rest
                ok, kind, why, detail = False, getattr(exc, "kind", None), _why(exc), str(exc) if isinstance(exc, GAMError) else ""
            job.record(email, ok, why, detail)
            if ok and on_set is not None:
                on_set(email)
            stop = stop_reason(kind, why, job.total - job.done)
            if stop:
                job.error = stop
                break
    except Exception as exc:
        job.error = _why(exc)
    finally:
        job.finish()


async def suspend_users(job, conn, emails, on_set: Optional[Callable[[str], None]] = None) -> None:
    """Suspend each of ``emails`` through the chokepoint (``plan_suspend`` → ``apply`` → ``_run_write``, which refuses the
    admin GamGUI signs in as)."""
    async def write(email: str):
        return (await conn.apply(conn.plan_suspend([email], suspend=True)))[0]
    await _each(job, emails, write, on_set)


async def send_reminders(job, conn, emails, subject: str, body: str) -> None:
    """Email each of ``emails`` the same reminder through the chokepoint (``send_reminder_email`` → ``_run_write``)."""
    await _each(job, emails, lambda email: conn.send_reminder_email(email, subject, body))


async def remove_members(job, conn, group: str, emails) -> None:
    """Remove each of ``emails`` from ``group`` through the chokepoint (``remove_group_member`` → ``_run_write``, roadmap 3.5)."""
    await _each(job, emails, lambda email: conn.remove_group_member(group, email))


async def move_users(job, conn, emails, org_unit: str, on_set: Optional[Callable[[str], None]] = None) -> None:
    """Move each of ``emails`` to ``org_unit`` through the chokepoint (``move_user_to_org_unit`` → ``_run_write``, roadmap
    4.2)."""
    await _each(job, emails, lambda email: conn.move_user_to_org_unit(email, org_unit), on_set)

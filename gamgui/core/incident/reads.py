"""The reads behind Check Account. The account comes first, since an alias reads as its owner and a failure there stops the check;
the rest run at once, each on its own, and a failure is said beside its list, never taken as "none". Changes nothing."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from ..gam.commands import GAMCommands
from ..gam.errors import GAMError
from ..person_reads import computer_note
from ..phishing import Incomplete
from .check import Check, Read, findings

READ_COUNT = 14     # the account and the thirteen lists: a job's total
# Gmail always lists the primary send-as address. GAM skips a refused token without a word and exits 0, so when that read gave no answer,
# no filters, delegates or forwarding addresses may just be Gmail never asked.
_SEND_AS_UNANSWERED = "the send-as read gave no answer, so none here can't be told from not read"


def in_words(exc: BaseException) -> str:
    """A failed read, in words, whatever it was: the Keychain refuses without a GAMError."""
    if isinstance(exc, GAMError):
        return exc.remediation
    if isinstance(exc, (Incomplete, ValueError)):
        return str(exc)
    return "Something went wrong talking to GAM."


async def read_check(conn: Any, asked: str, progress: Optional[Callable[[], None]] = None) -> Check:
    """One person's check. Raises when the account itself can't be read."""
    note = progress or (lambda: None)
    account = await conn.account_to_check(asked)
    note()
    email = account.primary_email

    async def one(read: Awaitable[Any]) -> Read:
        try:
            return Read(await read)
        except Exception as exc:  # noqa: BLE001 - said beside its list, whatever it was
            return Read(problem=in_words(exc))
        finally:
            note()

    async def computers_of() -> Read:
        try:
            return Read(await conn.computers(email))
        except GAMError as exc:     # the devices scope, by its cause, never "no computer"
            return Read(problem=" ".join(part for part in (exc.remediation, computer_note(exc)) if part))
        except Exception as exc:  # noqa: BLE001 - said beside its list, whatever it was
            return Read(problem=in_words(exc))
        finally:
            note()

    filters, forwarding, addresses, delegates, send_as, roles, scheduled, drafts, auto_reply, apps, phones, computers, scripts = await asyncio.gather(
        one(conn.filters_of(email)), one(conn.forwarding_of(email)), one(conn.forwarding_addresses_of(email)), one(conn.list_delegates(email)),
        one(conn.send_as_of(email)), one(conn.admin_roles_of(email)), one(conn.scheduled_sends_of(email)), one(conn.drafts_of(email)),
        one(conn.auto_reply_of(email)), one(conn.apps_of(email)), one(conn.mobile_devices(email)), computers_of(), one(conn.script_projects(email)))
    if send_as.problem:
        filters, delegates, addresses = (r if r.value else Read(problem=r.problem or _SEND_AS_UNANSWERED) for r in (filters, delegates, addresses))
    return Check(
        asked=asked, account=account, filters=filters, forwarding=forwarding, forwarding_addresses=addresses, delegates=delegates,
        send_as=send_as, admin_roles=roles, scheduled=scheduled, drafts=drafts, auto_reply=auto_reply, apps=apps, phones=phones, computers=computers, scripts=scripts,
        findings=findings(account, forwarding=forwarding.value, filters=filters.value, delegates=delegates.value, send_as=send_as.value,
                          roles=roles.value, scheduled=scheduled.value, drafts=drafts.value, auto_reply=auto_reply.value, apps=apps.value, phones=phones.value,
                          computers=computers.value, scripts=scripts.value),
        cut_off=await conn.cut_off_refusal([GAMCommands.deprovision_user(email)], previewing=True))

"""One person's license seats, on their page (roadmap 4.3): read them, add one, change one, remove one.

The card reads every seat in the domain (`gam print licenses`, the License usage run's read) and keeps this person's. Each
change is previewed: the preview reads the seats and their account again, refuses what that read doesn't support (a seat
they don't hold, a license nobody in the domain holds, a swap across products), and holds exactly the change under a
single-use token (web/previews.py) the confirm step posts back. A removal is DESTRUCTIVE (Google: "you might lose their
data"), so it needs their address typed, and it is refused while a data transfer from them is unfinished or can't be
checked, at the preview and again at the click.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard, licenses
from ...core.connectors.base import RiskLevel
from ...core.gam.commands import license_sku
from ...core.gam.models import LicenseSeat
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import GAM_TROUBLE, NOT_CONNECTED, error_partial, gam_detail, unread_why, write_failed

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users/licenses")

_FLOW = "license_change"
_CARD = "_license_card.html"
_CONFIRM = "_license_confirm.html"
_DONE = "_license_done.html"
_TRY_AGAIN = f"{GAM_TROUBLE} Please try again."
ACTIONS = ("add", "swap", "remove")
_RISK = {"add": RiskLevel.LOW, "swap": RiskLevel.LOW, "remove": RiskLevel.DESTRUCTIVE}


@router.get("", response_class=HTMLResponse)
async def license_card(request: Request, email: str = "") -> HTMLResponse:
    conn = request.app.state.gamgui.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        seats = await conn.license_seats()
    except Exception as exc:  # noqa: BLE001 — unread: nothing offered, never "holds none"
        return TEMPLATES.TemplateResponse(request, _CARD, {"email": email, "error": "Couldn't read the license seats. "
                                                           + unread_why(exc, _TRY_AGAIN)})
    offer = licenses.choices(seats, email)
    return TEMPLATES.TemplateResponse(request, _CARD, {"email": email, "adds": offer.adds, "held": offer.held})


def _key(email: str, action: str, product_id: str, sku_id: str, new_sku_id: str) -> tuple:
    return (email.strip().lower(), action, product_id, sku_id, new_sku_id if action == "swap" else "")


def _summary(action: str, seat: LicenseSeat, new: Optional[LicenseSeat]) -> str:
    if action == "add":
        return f"Add {seat.sku}"
    if action == "swap":
        return f"Change {seat.sku} to {new.sku if new else ''}"
    return f"Remove {seat.sku}"


def _refusal(action: str, addr: str, seats, product_id: str, sku_id: str, new_sku_id: str):
    """``(refusal, seat, new)`` for the change the form names, judged on the seats just read, and refused here, before
    anything is held, when GAM can't be given its IDs as one seat (`license_sku`): the write would raise at the click."""
    refusal, seat, new = _judged(action, addr, seats, product_id, sku_id, new_sku_id)
    try:
        for s in (x for x in (seat, new) if x is not None):
            license_sku(s.product_id, s.sku_id)
    except ValueError:
        return ("GAM can't be given this license's IDs as one seat (they hold a character GAM splits on), so it can't be "
                "changed here: use the Admin console."), None, None
    return refusal, seat, new


def _judged(action: str, addr: str, seats, product_id: str, sku_id: str, new_sku_id: str):
    offer = licenses.choices(seats, addr)
    mine = {licenses.key(s): (s, swaps) for s, swaps in offer.held}
    if action == "add":
        seat = next((s for s in offer.adds if licenses.key(s) == (product_id, sku_id)), None)
        if seat is not None:
            return "", seat, None
        if (product_id, sku_id) in mine:
            return f"{addr} already holds {mine[product_id, sku_id][0].sku}.", None, None
        if any(s.product_id == product_id for s, _ in offer.held):
            return f"{addr} already holds a license of that product: change it instead.", None, None
        return "That isn't a license anyone in this domain holds, so it isn't offered. Show their licenses again.", None, None
    if (product_id, sku_id) not in mine:
        return f"The seats read just now don't list that license for {addr}, so nothing is changed. Show their licenses again.", None, None
    held, swaps = mine[product_id, sku_id]
    if action == "remove":
        return "", held, None
    new = next((s for s in swaps if s.sku_id == new_sku_id), None)
    if new is None:
        return (f"{held.sku} can only change to another {held.product} license this domain holds that {addr} doesn't: "
                "pick one from the list."), None, None
    return "", held, new


@router.post("/preview", response_class=HTMLResponse)
async def license_preview(request: Request, email: Annotated[str, Form()] = "", action: Annotated[str, Form()] = "",
                          product_id: Annotated[str, Form()] = "", sku_id: Annotated[str, Form()] = "",
                          new_sku_id: Annotated[str, Form()] = "", picked: Annotated[str, Form(alias="seat")] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # before the reads: what the preview is bound to (web/previews.py)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    if action not in ACTIONS:
        return error_partial(request, "Pick add, change or remove.")
    if picked:   # the add list's one value: `productId:skuId`, split as GAM splits `nv:` (a product ID has no `:`)
        product_id, _, sku_id = picked.partition(":")
    if not (product_id and sku_id):
        return error_partial(request, "Pick a license first.")
    try:   # neither read needs the other
        seats, person = await asyncio.gather(conn.license_seats(), conn.get_user(email.strip(), fields=["primaryEmail"]))
    except Exception as exc:  # noqa: BLE001 — either read failing offers nothing
        return error_partial(request, "Couldn't read the license seats or their account again, so nothing is offered. "
                             + unread_why(exc, _TRY_AGAIN), gam_detail(exc))
    # The account's primary address, as GAM gave it: the seats list it, and the change audits under it.
    addr = str(person.raw.get("primaryEmail") or "").strip()
    if not addr:
        return error_partial(request, f"Couldn't read {email}'s account, so nothing is offered. {_TRY_AGAIN}")
    refusal, seat, new = _refusal(action, addr, seats, product_id, sku_id, new_sku_id)
    if refusal:
        return error_partial(request, refusal)
    failed: list = []
    if action == "remove":
        pending = await conn.incomplete_transfers_for(addr)
        if refusal := licenses.transfer_refusal(addr, pending):
            return error_partial(request, refusal)
        failed = licenses.failed_transfers(pending)
    held = (addr, action, seat, new, failed)
    token = st.previews.hold(_FLOW, _key(addr, action, product_id, sku_id, new_sku_id), held, tenant=tenant)
    return _confirm(request, held, token)


def _confirm(request: Request, held: tuple, token: str, error: str = "") -> HTMLResponse:
    addr, action, seat, new, failed = held
    return TEMPLATES.TemplateResponse(request, _CONFIRM, {
        "email": addr, "action": action, "seat": seat, "new": new, "failed": failed, "token": token, "error": error,
        "might_lose": licenses.MIGHT_LOSE_DATA, "wait": licenses.REASSIGN_WAIT, "source": licenses.SOURCE})


@router.post("/apply", response_class=HTMLResponse)
async def license_apply(request: Request, email: Annotated[str, Form()] = "", action: Annotated[str, Form()] = "",
                        product_id: Annotated[str, Form()] = "", sku_id: Annotated[str, Form()] = "",
                        new_sku_id: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # a typo's fresh token is bound to the tenant this request started on
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    key = _key(email, action, product_id, sku_id, new_sku_id)
    held, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), key, again="click Preview again")
    if refusal:
        return error_partial(request, refusal)
    addr, action, seat, new, _failed = held
    removing = action == "remove"
    refusal = guard.enforce(guard.changes([addr], _RISK[action], _summary(action, seat, new)), form, confirm_step=True,
                            typed_address=addr if removing else None)
    if refusal:   # a mistyped address: the same confirm step again, under a fresh token holding the same change
        token = st.previews.hold(_FLOW, key, held, tenant=tenant)
        return _confirm(request, held, token, error=refusal)
    if removing and (refusal := licenses.transfer_refusal(addr, await conn.incomplete_transfers_for(addr))):
        return error_partial(request, refusal)   # read again at the click: a transfer started since the preview
    if action == "add":
        result, done = await conn.add_license(addr, seat), f"Added {seat.sku} to {addr}."
    elif action == "swap":
        result, done = await conn.swap_license(addr, seat, new.sku_id), f"Changed {addr}'s {seat.sku} to {new.sku}."
    else:
        result, done = await conn.remove_license(addr, seat), f"Removed {seat.sku} from {addr}. {licenses.REASSIGN_WAIT}"
    if not result.ok:
        failed = {"add": f"Couldn't add {seat.sku} to {addr}.", "remove": f"Couldn't remove {seat.sku} from {addr}.",
                  "swap": f"Couldn't change {addr}'s {seat.sku}."}[action]
        return write_failed(request, failed, result)
    return TEMPLATES.TemplateResponse(request, _DONE, {"message": done, "email": addr})

"""The small helpers every screen's routes share: the app state and its connector, a failed call in
words, and the amber result partial. One copy, so each screen says the same thing the same way."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

from ...core.gam.errors import GAMError
from ...core.signatures import SignatureStore
from ..server import TEMPLATES

NOT_CONNECTED = "Not connected."
GAM_TROUBLE = "Something went wrong talking to GAM."


def app_state(request: Request):
    return request.app.state.gamgui


def connector(request: Request):
    return request.app.state.gamgui.connector


def friendly(exc: Exception, fallback: str = GAM_TROUBLE) -> str:
    """What to do about a failed call, in words: GAM's remediation for its kind, else ``fallback``."""
    return exc.remediation if isinstance(exc, GAMError) else fallback


def error_partial(request: Request, message: str, details: str = "") -> HTMLResponse:
    """The amber inline result (an HTMX swap target); ``details`` is GAM's raw error, collapsed."""
    return TEMPLATES.TemplateResponse(request, "_action_result.html",
                                      {"ok": False, "message": message, "details": details})


def write_failed(request: Request, what: str, result) -> HTMLResponse:
    """A failed write: what didn't happen and what to do about it in words (``remediation``), with
    GAM's raw error one click away — never GAM's line as the headline (plan U9)."""
    return error_partial(request, f"{what} {result.remediation}".strip(), result.detail)


def signature_store(request: Request) -> SignatureStore:
    """The saved-template store, created on first use (the real ~/Library file unless a test
    pre-seeds ``st.sig_templates`` with a store pointed at a tmp path)."""
    st = app_state(request)
    if st.sig_templates is None:
        st.sig_templates = SignatureStore()
    return st.sig_templates

"""What a confirm step may run: exactly what its preview showed, held under a single-use token.

A confirm button that posts the live form (``hx-include``) runs whatever the form holds when it is
clicked — a scope widened, an email retyped or another Builder command loaded after Preview ran
values nobody had checked (docs/failure-log.md, 2026-09-23). So a preview holds the values it
showed, keyed by the form it was built from, under a fresh token its confirm step posts back as
``preview``. The route then runs the held values only when the live form still matches; an edited
form, a used token and an expired one are refused with a "preview again" message, never run.

Tokens are single use (taken, not read), expire after ``PREVIEW_TTL`` and are capped per flow, so
the store can't grow however often the operator previews (invariant #9).

A preview is also bound to the tenant it was made on (``bind``, the active connector's domain): a
confirm step left open across a tenant switch would otherwise run what it showed — the old tenant's
people, or a bare name GAM completes with the new tenant's domain — through the new tenant's
credentials (review 2, R5). A switch clears the store too (``AppState.activate``); the binding is what
refuses a token no matter how the connector came to change.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, Optional, Tuple

from ..core import clock

TOKEN_FIELD = "preview"   # what a confirm step posts back: the token its preview rendered
PREVIEW_TTL = 15 * 60     # seconds a preview stays runnable
PREVIEWS_KEPT = 8         # per flow; the oldest goes first


@dataclass
class _Held:
    form: Hashable            # the key of the form the preview was built from
    value: Any                # what the confirm step runs
    tenant: str               # the domain that was active when the preview was made
    at: float = field(default_factory=clock.now)       # counts sleep: a lid closed mid-preview expires it


class Previews:
    """Per-flow single-use tokens -> the values a preview showed (``AppState.previews``)."""

    def __init__(self) -> None:
        self._flows: Dict[str, Dict[str, _Held]] = {}
        self._tenant: Callable[[], str] = lambda: ""

    def bind(self, tenant: Callable[[], str]) -> None:
        """Tie every preview to ``tenant()`` (the active domain) as it is when held and when taken."""
        self._tenant = tenant

    def clear(self) -> None:
        """Drop every held preview: nothing previewed before this can run (a tenant switch)."""
        self._flows.clear()

    def hold(self, flow: str, form: Hashable, value: Any) -> str:
        """Keep ``value`` for ``flow`` under a fresh token; drop expired ones and cap the rest."""
        held = self._flows.setdefault(flow, {})
        now = clock.now()
        for token in [t for t, h in held.items() if now - h.at > PREVIEW_TTL]:
            del held[token]
        while len(held) >= PREVIEWS_KEPT:
            del held[next(iter(held))]
        token = secrets.token_urlsafe(16)
        held[token] = _Held(form, value, self._tenant())
        return token

    def take(self, flow: str, token: str, form: Hashable, again: str = "preview again",
             what: str = "form") -> Tuple[Optional[Any], Optional[str]]:
        """``(value, None)`` when ``token`` holds a live preview of this very ``form``, else
        ``(None, why not)``: ``again`` says what to click, ``what`` names the thing that changed.
        Either way the token is spent: a second run needs a new preview."""
        held = self._flows.get(flow, {}).pop(token, None) if token else None
        if held is None or clock.now() - held.at > PREVIEW_TTL:
            return None, f"That preview has expired or was already run — {again}."
        if held.tenant != self._tenant():
            return None, f"The active domain changed after the preview — {again}, so it runs on this one."
        if held.form != form:
            return None, f"The {what} changed after the preview — {again}, so what runs is what you checked."
        return held.value, None

    def count(self, flow: str) -> int:
        return len(self._flows.get(flow, {}))

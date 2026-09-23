"""What a confirm step may run: exactly what its preview showed, held under a single-use token.

A confirm button that posts the live form (``hx-include``) runs whatever the form holds when it is
clicked — a scope widened, an email retyped or another Builder command loaded after Preview ran
values nobody had checked (docs/failure-log.md, 2026-09-23). So a preview holds the values it
showed, keyed by the form it was built from, under a fresh token its confirm step posts back as
``preview``. The route then runs the held values only when the live form still matches; an edited
form, a used token and an expired one are refused with a "preview again" message, never run.

Tokens are single use (taken, not read), expire after ``PREVIEW_TTL`` and are capped per flow, so
the store can't grow however often the operator previews (invariant #9).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Optional, Tuple

TOKEN_FIELD = "preview"   # what a confirm step posts back: the token its preview rendered
PREVIEW_TTL = 15 * 60     # seconds a preview stays runnable
PREVIEWS_KEPT = 8         # per flow; the oldest goes first


@dataclass
class _Held:
    form: Hashable            # the key of the form the preview was built from
    value: Any                # what the confirm step runs
    at: float = field(default_factory=time.monotonic)


class Previews:
    """Per-flow single-use tokens -> the values a preview showed (``AppState.previews``)."""

    def __init__(self) -> None:
        self._flows: Dict[str, Dict[str, _Held]] = {}

    def hold(self, flow: str, form: Hashable, value: Any) -> str:
        """Keep ``value`` for ``flow`` under a fresh token; drop expired ones and cap the rest."""
        held = self._flows.setdefault(flow, {})
        now = time.monotonic()
        for token in [t for t, h in held.items() if now - h.at > PREVIEW_TTL]:
            del held[token]
        while len(held) >= PREVIEWS_KEPT:
            del held[next(iter(held))]
        token = secrets.token_urlsafe(16)
        held[token] = _Held(form, value)
        return token

    def take(self, flow: str, token: str, form: Hashable, again: str = "preview again",
             what: str = "form") -> Tuple[Optional[Any], Optional[str]]:
        """``(value, None)`` when ``token`` holds a live preview of this very ``form``, else
        ``(None, why not)``: ``again`` says what to click, ``what`` names the thing that changed.
        Either way the token is spent: a second run needs a new preview."""
        held = self._flows.get(flow, {}).pop(token, None) if token else None
        if held is None or time.monotonic() - held.at > PREVIEW_TTL:
            return None, f"That preview has expired or was already run — {again}."
        if held.form != form:
            return None, f"The {what} changed after the preview — {again}, so what runs is what you checked."
        return held.value, None

    def count(self, flow: str) -> int:
        return len(self._flows.get(flow, {}))

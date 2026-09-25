"""A tiny TTL cache for the user list.

`gam print users` is the expensive call (subprocess + full Directory API fetch). Caching the parsed
result lets the list, search, and reports all serve from one fetch instead of re-running gam on
every page load / keystroke. Manual refresh (force) and invalidation handle staleness; a write whose
new values are known patches its one record (plan U12) instead of dropping the whole list.

Past the TTL a page that shows the list's age (``stale_ok``) gets the list as it is while one refresh
runs in the background (plan U12b); every other caller — offboarding's address check among them —
waits for a fresh read, and a failed read raises rather than falling back to the old list.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from . import clock

log = logging.getLogger(__name__)


class UserCache:
    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._items: Optional[list] = None
        self._at = 0.0
        self._gen = 0   # bumped by every patch/invalidate: a fetch that raced one isn't kept
        self._lock = asyncio.Lock()
        self._refresh: Optional[asyncio.Task] = None   # the background refresh, one at a time

    def _expired(self) -> bool:
        return clock.now() - self._at > self.ttl   # counts sleep: "fresh" means fresh in real time

    async def get(self, fetch: Callable[[], Awaitable[list]], force: bool = False, stale_ok: bool = False) -> list:
        """The list. Within the TTL it is served as it is. Past it, ``stale_ok`` serves it anyway and starts
        a background refresh (only for a caller that shows the list's age); otherwise it is re-read first,
        and a failed read raises — a safety check never decides on stale data. ``force`` always re-reads."""
        items = self._items
        if not force and items is not None and (stale_ok or not self._expired()):
            if self._expired():
                self._refresh_soon(fetch)
            return items
        async with self._lock:
            if force or self._items is None or self._expired():
                gen, started = self._gen, clock.now()
                items = await fetch()
                if gen != self._gen:   # a write landed mid-fetch: the list may predate it
                    return items
                self._items, self._at = items, started
            return self._items

    def _refresh_soon(self, fetch: Callable[[], Awaitable[list]]) -> None:
        if not self.refreshing:
            self._refresh = asyncio.create_task(self._revalidate(fetch, self._gen))

    async def _revalidate(self, fetch: Callable[[], Awaitable[list]], gen: int) -> None:
        """A background re-read, kept only if nothing changed the list since it was asked for: a patch
        or an invalidate (a tenant switch — ``fetch`` may read the old tenant) voids it."""
        try:
            async with self._lock:
                if gen != self._gen or not self._expired():
                    return
                started = clock.now()
                items = await fetch()
                if gen == self._gen:
                    self._items, self._at = items, started
        except Exception as exc:  # noqa: BLE001 - the old list stays, its age says so; the next page retries
            log.warning("background directory refresh failed: %s", exc)

    @property
    def refreshing(self) -> bool:
        return self._refresh is not None and not self._refresh.done()

    def invalidate(self) -> None:
        self._gen += 1
        self._items = None
        self._at = 0.0

    def patch(self, pick: Callable[[Any], bool], change: Optional[Callable[[Any], Any]] = None) -> None:
        """A write whose result is known: each item ``pick`` matches becomes ``change(item)``, or is
        dropped when ``change`` is None. Nothing matching drops the whole list — the write reached an
        account the cache doesn't show as it is. The list keeps its age: one known record doesn't make
        the rest fresher, so the TTL and ``force`` still re-fetch it whole. A new list, never an edit
        in place, so a caller iterating the one ``get`` returned isn't changed under it."""
        self._gen += 1
        if self._items is None:
            return
        if not any(pick(i) for i in self._items):
            self.invalidate()
            return
        if change is None:
            self._items = [i for i in self._items if not pick(i)]
        else:
            self._items = [change(i) if pick(i) else i for i in self._items]

    @property
    def cached(self) -> Optional[list]:
        """The list as it is, whatever its age, or None when nothing is cached — never a fetch and never
        a refresh (Home, plan U4: its counts cost no ``gam`` call; ``age_seconds`` says how old they are)."""
        return self._items

    @property
    def age_seconds(self) -> Optional[float]:
        return None if self._items is None else clock.now() - self._at

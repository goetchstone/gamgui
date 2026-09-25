"""A tiny TTL cache for the user list.

`gam print users` is the expensive call (subprocess + full Directory API fetch). Caching the parsed
result lets the list, search, and reports all serve from one fetch instead of re-running gam on
every page load / keystroke. Manual refresh (force) and invalidation handle staleness; a write whose
new values are known patches its one record (plan U12) instead of dropping the whole list.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from . import clock


class UserCache:
    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._items: Optional[list] = None
        self._at = 0.0
        self._gen = 0   # bumped by every patch/invalidate: a fetch that raced one isn't kept
        self._lock = asyncio.Lock()

    async def get(self, fetch: Callable[[], Awaitable[list]], force: bool = False) -> list:
        async with self._lock:
            now = clock.now()   # counts sleep: "fresh" means fresh in real time
            if force or self._items is None or (now - self._at) > self.ttl:
                gen = self._gen
                items = await fetch()
                if gen != self._gen:   # a write landed mid-fetch: the list may predate it
                    return items
                self._items, self._at = items, now
            return self._items

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
    def age_seconds(self) -> Optional[float]:
        return None if self._items is None else clock.now() - self._at

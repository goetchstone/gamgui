"""A tiny TTL cache for the user list.

`gam print users` is the expensive call (subprocess + full Directory API fetch). Caching the parsed
result lets the list, search, and reports all serve from one fetch instead of re-running gam on
every page load / keystroke. Manual refresh (force) and invalidation handle staleness.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, List, Optional


class UserCache:
    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._items: Optional[list] = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, fetch: Callable[[], Awaitable[list]], force: bool = False) -> list:
        async with self._lock:
            now = time.monotonic()
            if force or self._items is None or (now - self._at) > self.ttl:
                self._items = await fetch()
                self._at = now
            return self._items

    def invalidate(self) -> None:
        self._items = None
        self._at = 0.0

    @property
    def age_seconds(self) -> Optional[float]:
        return None if self._items is None else time.monotonic() - self._at

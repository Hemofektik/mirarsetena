"""Level-2 tile cache: render memoization with an LRU byte budget.

The budget applies to the project's tiles/ prefix only — level-1 scene
sources stay resident (they are the expensive downloads; tiles re-render).
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable

from mirarsetena.projects.registry import cache_key
from mirarsetena.storage import Storage, enforce_budget


class TileCache:
    def __init__(
        self,
        storage: Storage,
        slug: str,
        *,
        budget_bytes: int,
        render_slots: int = 2,
    ):
        self._storage = storage
        self._slug = slug
        self._budget = budget_bytes
        self._prefix = cache_key(slug, "tiles") + "/"
        # Concurrent renders measured 10x slower than serial (GIL + FS
        # contention): responses all landed at once at the end of a wave.
        # A small slot pool keeps the fan-out at serial speed.
        slots = int(os.environ.get("MIRAR_RENDER_SLOTS", str(render_slots)))
        self._render_slots = threading.BoundedSemaphore(max(slots, 1))
        self._written = 0
        self._account_lock = threading.Lock()

    def tile_key(self, *parts: str) -> str:
        return cache_key(self._slug, "tiles", *parts)

    def get_or_render(self, parts: tuple[str, ...], render_fn: Callable[[], bytes]) -> bytes:
        key = self.tile_key(*parts)
        cached = self._storage.get(key)
        if cached is not None:
            return cached
        with self._render_slots:
            payload = render_fn()
        self._storage.put(key, payload)
        # Amortize the budget walk (250 ms over thousands of tile files):
        # only enforce once real bytes have piled up — one walk per budget
        # eighth instead of one per cold render. Small budgets still cross
        # the threshold on every write, so eviction behaviour is unchanged.
        with self._account_lock:
            self._written += len(payload)
            crossed = self._written >= max(self._budget // 8, 1)
            if crossed:
                self._written = 0
        if crossed:
            self._enforce_budget()
        return payload

    def _enforce_budget(self) -> None:
        enforce_budget(self._storage, (self._prefix,), self._budget)

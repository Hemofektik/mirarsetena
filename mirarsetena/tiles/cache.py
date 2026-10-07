"""Level-2 tile cache: render memoization with an LRU byte budget.

The budget applies to the project's tiles/ prefix only — level-1 scene
sources stay resident (they are the expensive downloads; tiles re-render).
"""
from __future__ import annotations

from collections.abc import Callable

from mirarsetena.projects.registry import cache_key
from mirarsetena.storage import Storage


class TileCache:
    def __init__(self, storage: Storage, slug: str, *, budget_bytes: int):
        self._storage = storage
        self._slug = slug
        self._budget = budget_bytes
        self._prefix = cache_key(slug, "tiles") + "/"

    def tile_key(self, *parts: str) -> str:
        return cache_key(self._slug, "tiles", *parts)

    def get_or_render(self, parts: tuple[str, ...], render_fn: Callable[[], bytes]) -> bytes:
        key = self.tile_key(*parts)
        cached = self._storage.get(key)
        if cached is not None:
            return cached
        payload = render_fn()
        self._storage.put(key, payload)
        self._enforce_budget()
        return payload

    def _enforce_budget(self) -> None:
        while self._storage.total_size(self._prefix) > self._budget:
            keys = self._storage.list(self._prefix)
            if not keys:
                break
            oldest = min(
                keys,
                key=lambda key: self._storage.modified_at(key) or 0.0,
            )
            self._storage.delete(oldest)

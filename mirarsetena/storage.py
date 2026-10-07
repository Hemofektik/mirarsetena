"""Two-level cache storage: interface first, local filesystem now, S3 later.

Keys are flat, project-namespaced paths (see mirarsetena.projects.registry
cache_key) — the whole cache budget (SCOPE R3-Q6) is accounted through this
interface so the later AWS move swaps the implementation, not the callers.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, runtime_checkable

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


class StorageError(ValueError):
    """A key or storage operation violates the storage contract."""


def _components(key: str, *, allow_trailing_slash: bool = False) -> list[str]:
    if "\\" in key:
        raise StorageError(f"backslash not allowed in key: {key!r}")
    parts = key.split("/")
    if allow_trailing_slash and parts and parts[-1] == "":
        parts = parts[:-1]
    for part in parts:
        if part in ("", ".", "..") or not _SAFE_COMPONENT.match(part):
            raise StorageError(f"unsafe key component in {key!r}: {part!r}")
    return parts


@runtime_checkable
class Storage(Protocol):
    """Contract every store implementation must satisfy."""

    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, data: bytes) -> None: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...

    def size(self, key: str) -> int | None: ...

    def modified_at(self, key: str) -> float | None: ...

    def list(self, prefix: str) -> list[str]: ...

    def total_size(self, prefix: str) -> int: ...


class LocalStore:
    """Filesystem-backed store rooted at a directory."""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root.joinpath(*_components(key))

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def size(self, key: str) -> int | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.stat().st_size

    def modified_at(self, key: str) -> float | None:
        """mtime (epoch seconds) — the LRU signal for tile eviction."""
        path = self._path(key)
        if not path.is_file():
            return None
        return path.stat().st_mtime

    def list(self, prefix: str) -> list[str]:
        if not self._root.is_dir():
            return []
        _components(prefix, allow_trailing_slash=True)
        keys = []
        for path in self._root.rglob("*"):
            if path.is_file():
                key = path.relative_to(self._root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def total_size(self, prefix: str) -> int:
        return sum(self.size(key) or 0 for key in self.list(prefix))

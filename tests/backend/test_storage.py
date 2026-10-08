"""C.2 — storage interface (local now, S3-compatible later).

Seam: mirarsetena.storage — the Storage contract that LocalStore implements
today and any future S3Store must satisfy unchanged (interface-contract style
per IMPLEMENTATION.md C.2).
"""
import pytest

from mirarsetena.projects.registry import cache_key
from mirarsetena.storage import LocalStore, Storage, StorageError


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path)


def test_local_store_satisfies_the_storage_contract(store):
    assert isinstance(store, Storage)


def test_round_trip(store):
    store.put("p/demo/tiles/x.png", b"payload")
    assert store.exists("p/demo/tiles/x.png")
    assert store.get("p/demo/tiles/x.png") == b"payload"
    assert store.size("p/demo/tiles/x.png") == 7
    assert store.delete("p/demo/tiles/x.png") is True
    assert store.get("p/demo/tiles/x.png") is None
    assert not store.exists("p/demo/tiles/x.png")


def test_missing_key_returns_none_and_delete_reports_false(store):
    assert store.get("p/nope") is None
    assert store.size("p/nope") is None
    assert store.delete("p/nope") is False


def test_list_returns_sorted_keys_under_prefix(store):
    store.put("p/a/one.txt", b"1")
    store.put("p/a/two.txt", b"2")
    store.put("p/b/other.txt", b"3")
    assert store.list("p/a/") == ["p/a/one.txt", "p/a/two.txt"]
    assert store.list("p/") == ["p/a/one.txt", "p/a/two.txt", "p/b/other.txt"]


def test_project_namespaces_are_isolated(store):
    """Same suffix under two project namespaces must never collide."""
    key_a = cache_key("proj-a", "tiles", "ndvi", "14.png")
    key_b = cache_key("proj-b", "tiles", "ndvi", "14.png")
    store.put(key_a, b"AAAA")
    store.put(key_b, b"BBBB")
    assert store.get(key_a) == b"AAAA"
    assert store.get(key_b) == b"BBBB"
    assert key_a != key_b


def test_size_reporting_per_prefix(store):
    store.put("p/a/1.bin", b"12345")
    store.put("p/a/2.bin", b"123")
    store.put("p/b/3.bin", b"12")
    assert store.total_size("p/a/") == 8
    assert store.total_size("p/") == 10
    assert store.total_size("p/missing/") == 0


def test_modified_at_reports_mtime_and_none_for_missing(store):
    store.put("p/a/x.bin", b"data")
    stamp = store.modified_at("p/a/x.bin")
    assert isinstance(stamp, float) and stamp > 0
    assert store.modified_at("p/missing") is None


def test_unsafe_keys_are_rejected(store):
    for bad in ("../escape", "/absolute/path", "a/../../b", "a\\b", "", "a//b"):
        with pytest.raises(StorageError):
            store.put(bad, b"x")


def test_enforce_budget_evicts_oldest_across_prefixes(store):
    """Source-level guardrail: scene/layer/coherence products share one
    LRU budget (10 GB in the project config); oldest files go first."""
    import os

    from mirarsetena.storage import enforce_budget

    keys = []
    for i, prefix in enumerate(
        ("p/demo/scenes/", "p/demo/layers/", "p/demo/coherence/")
    ):
        key = f"{prefix}item-{i}.tif"
        store.put(key, b"x" * 1000)
        os.utime(store._path(key), (1_700_000_000 + i * 100,) * 2)
        keys.append(key)

    # Budget holds exactly two of the three files -> the oldest is evicted.
    enforce_budget(
        store,
        ("p/demo/scenes/", "p/demo/layers/", "p/demo/coherence/"),
        budget_bytes=2500,
    )
    assert not store.exists(keys[0])
    assert store.exists(keys[1]) and store.exists(keys[2])
    total = sum(store.total_size(p) for p in ("p/demo/scenes/", "p/demo/layers/", "p/demo/coherence/"))
    assert total <= 2500


def test_enforce_budget_is_a_noop_under_budget(store):
    from mirarsetena.storage import enforce_budget

    store.put("p/demo/scenes/only.tif", b"y" * 500)
    enforce_budget(store, ("p/demo/scenes/",), budget_bytes=10_000)
    assert store.exists("p/demo/scenes/only.tif")


def test_enforce_budget_enumerates_each_prefix_exactly_once(store):
    """Budget enforcement runs after EVERY cold tile render and production;
    each call may enumerate its prefixes once — re-listing per eviction
    iteration turned one call into dozens of full tree walks (22 s stalls
    under a tile wave, user report: tiles only appear once all spinners
    are gone)."""
    import os

    from mirarsetena.storage import enforce_budget

    class CountingStore:
        def __init__(self, inner):
            self._inner = inner
            self.list_calls = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def list(self, prefix):
            self.list_calls += 1
            return self._inner.list(prefix)

    spy = CountingStore(store)
    # 12 files of 1000 bytes against a 3000-byte budget -> 9 evictions
    for i in range(12):
        key = f"p/demo/tiles/item-{i}.bin"
        store.put(key, b"x" * 1000)
        ts = 1_700_000_000 + i
        os.utime(store._path(key), (ts, ts))


    enforce_budget(spy, ("p/demo/tiles/",), budget_bytes=3000)

    assert spy.list_calls == 1
    remaining = store.list("p/demo/tiles/")
    assert len(remaining) == 3  # oldest nine evicted


def test_list_enumerates_only_the_prefix_subtree(store, tmp_path, monkeypatch):
    """list(prefix) must not walk the whole cache root: the root holds every
    project's scenes/layers/tiles (thousands of files) and rglob-over-root
    per budget check made each cold tile render pay for the entire cache."""
    from pathlib import Path

    store.put("p/demo/tiles/a.png", b"x")
    store.put("p/demo/scenes/s.tif", b"y")
    store.put("p/other/file.txt", b"z")

    starts = []
    original = Path.rglob

    def spy_rglob(self, pattern, *args, **kwargs):
        starts.append(self)
        return original(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", spy_rglob)
    keys = store.list("p/demo/tiles/")

    assert keys == ["p/demo/tiles/a.png"]
    assert starts, "list() must enumerate something"
    tiles_root = tmp_path / "p" / "demo" / "tiles"
    outside = [s for s in starts if not str(s).startswith(str(tiles_root))]
    assert outside == [], f"enumerated outside the prefix: {outside}"

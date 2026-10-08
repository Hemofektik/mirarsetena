"""F.2 — level-2 tile cache: render memoization + LRU budget eviction.

Eviction scans only the project's tiles/ prefix — level-1 scene sources
under scenes/ are never candidates (SCOPE R2-Q6).
"""

from mirarsetena.storage import LocalStore
from mirarsetena.tiles.cache import TileCache


def test_render_happens_once_per_key(tmp_path):
    storage = LocalStore(tmp_path)
    cache = TileCache(storage, "proj-a", budget_bytes=10_000_000)
    calls = {"n": 0}

    def render():
        calls["n"] += 1
        return b"PNG" * 40

    first = cache.get_or_render(("ndvi", "2026-07-03", "0", "0", "0"), render)
    second = cache.get_or_render(("ndvi", "2026-07-03", "0", "0", "0"), render)
    assert calls["n"] == 1
    assert first == second == b"PNG" * 40


def test_budget_evicts_oldest_tiles_and_never_sources(tmp_path):
    import os

    storage = LocalStore(tmp_path)
    cache = TileCache(storage, "proj-a", budget_bytes=3000)

    # A level-1 source (kept unconditionally)
    source_key = "p/proj-a/scenes/sentinel-2-l2a_2026-07-03_blue.tif"
    storage.put(source_key, b"S" * 5000)

    keys = []
    for i in range(6):
        key = cache.tile_key("ndvi", f"tile-{i}", "0", "0", "0")
        cache.get_or_render(("ndvi", f"tile-{i}", "0", "0", "0"),
                            lambda: b"x" * 1000)
        keys.append(key)
        path = tmp_path / key
        stamp = 1_700_000_000 + i * 100
        os.utime(path, (stamp, stamp))  # deterministic mtimes

    # Force one more write so eviction runs with the deterministic mtimes:
    final_key = cache.tile_key("ndvi", "tile-final", "0", "0", "0")
    cache.get_or_render(("ndvi", "tile-final", "0", "0", "0"), lambda: b"y" * 1000)
    path = tmp_path / final_key
    os.utime(path, (1_700_000_000 + 600, 1_700_000_000 + 600))
    cache.get_or_render(("ndvi", "tile-trigger", "0", "0", "0"), lambda: b"z" * 1000)

    tile_keys = storage.list("p/proj-a/tiles/")
    total = sum(storage.size(key) for key in tile_keys)
    assert total <= 3000  # budget respected for the tiles prefix

    newest = cache.tile_key("ndvi", "tile-trigger", "0", "0", "0")
    assert storage.exists(newest)  # freshest tile survives
    assert not storage.exists(keys[0])  # oldest evicted first
    assert storage.get(source_key) == b"S" * 5000  # source untouched


def test_budget_enforcement_is_amortized_for_large_budgets(tmp_path, monkeypatch):
    """A cold tile wave used to run the full budget walk (250 ms over ~4.6k
    files) after EVERY render — 12 concurrent renders thrashed it into 27 s.
    With a real 10 GB budget the walk is pure overhead until real bytes have
    accumulated, so it must run only once writes cross an eighth of the
    budget (small budgets keep enforcing every write: eviction tests pin it).
    """
    import mirarsetena.tiles.cache as cache_module

    calls = {"n": 0}
    real = cache_module.enforce_budget

    def counting_enforce(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cache_module, "enforce_budget", counting_enforce)

    storage = LocalStore(tmp_path)
    budget = 80_000_000  # -> enforce only after 10 MB written
    cache = TileCache(storage, "proj-a", budget_bytes=budget)

    for i in range(4):
        cache.get_or_render(("ndvi", f"big-{i}", "0", "0", "0"),
                            lambda: b"x" * 6_000_000)

    # 6 MB -> nothing; 12 MB -> one walk; 6 MB -> nothing; 12 MB -> one walk
    assert calls["n"] == 2


def test_concurrent_renders_are_bounded(tmp_path):
    """Four parallel renders measured 10x slower than serial (GIL + FS
    contention): each request got slower the more arrived, and all
    responses landed at once. A small render slot pool keeps the fan-out
    working at serial speed while responses trickle out."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    storage = LocalStore(tmp_path)
    cache = TileCache(storage, "proj-a", budget_bytes=10_000_000, render_slots=2)

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def slow_render():
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.15)
        with lock:
            state["active"] -= 1
        return b"PNG"

    keys = [("ndvi", f"t{i}", "0", "0", "0") for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda k: cache.get_or_render(k, slow_render), keys))

    assert state["peak"] <= 2

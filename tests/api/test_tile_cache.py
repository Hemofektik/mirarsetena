"""F.2 — level-2 tile cache: render memoization + LRU budget eviction.

Eviction scans only the project's tiles/ prefix — level-1 scene sources
under scenes/ are never candidates (SCOPE R2-Q6).
"""
from pathlib import Path

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

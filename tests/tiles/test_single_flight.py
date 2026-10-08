"""Responsiveness: one (mission, date) is produced once, not once per tile.

A layer switch fires ~20 tile requests at the same missing product. Without
single-flight each request downloads the scene, saturating the server
threadpool and starving the /api/dates request the UI is waiting on
(user report: "the background requests for the requested layer would block
everything else").
"""
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.storage import LocalStore
from mirarsetena.tiles.service import make_processor

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"


def test_concurrent_requests_produce_each_mission_once(tmp_path):
    config = ProjectRegistry(CONFIG_DIR).get("cdp-rio-general")
    storage = LocalStore(tmp_path)
    calls: dict[str, int] = {}

    def slow_search(collection, bbox, start, end):
        calls[collection] = calls.get(collection, 0) + 1
        time.sleep(0.4)
        return []

    process = make_processor(storage, config, slow_search)
    # Four S2 layers twice (20 tiles would collapse the same way) + two S1,
    # all racing one acquisition date: one search per mission, not per tile.
    layers = ("ndvi", "rgb", "mndwi", "bsi", "ndvi", "rgb", "sigma0", "sigma0")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda layer: process(layer, "2026-07-03"), layers))

    assert calls == {"sentinel-2-l2a": 1, "sentinel-1-grd": 1}


def test_stale_index_refreshes_once_under_concurrency(tmp_path):
    """A layer switch fans out (fetchDates + ~20 tiles) in the same instant;
    if the 6h index TTL expired they must share ONE catalog refresh."""
    import json
    from datetime import datetime, timezone

    from mirarsetena.pipeline.dates import index_key, list_dates

    slug = "cdp-rio-general"
    config = ProjectRegistry(CONFIG_DIR).get(slug)
    storage = LocalStore(tmp_path)
    calls: dict[str, int] = {}

    def slow_search(collection, bbox, start, end):
        calls[collection] = calls.get(collection, 0) + 1
        time.sleep(0.4)
        return []

    storage.put(
        index_key(slug),
        json.dumps(
            {"updated_at": "2020-01-01T00:00:00+00:00", "missions": {}}
        ).encode(),
    )
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda _: list_dates(
                    storage, slug, config, "ndvi",
                    search_fn=slow_search, now=now,
                ),
                range(8),
            )
        )

    assert calls == {
        "sentinel-2-l2a": 1,
        "sentinel-1-grd": 1,
        "sentinel-1-slc": 1,
    }

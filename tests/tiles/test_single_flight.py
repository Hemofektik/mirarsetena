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
        time.sleep(1.5)
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
        time.sleep(1.5)
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


def test_app_shares_one_processor_across_concurrent_tile_requests(tmp_path):
    """The APP must hand every tile request the SAME make_processor closure.

    tile_service() used to build a fresh closure per request, so each
    concurrent tile got its own single-flight: N duplicate productions AND
    N concurrent budget walks (measured: 22 s scene_enforce + 8.4 s tile
    enforce per call under a 6-tile wave — responses trickled out in clumps
    and the UI showed nothing until the last spinner cleared).

    Seam: create_app + the XYZ tile route — production wiring, no override.
    """
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from mirarsetena.app import create_app

    produce_calls: dict[str, int] = {}

    def slow_search(collection, bbox, start, end):
        if start == end:  # produce()'s narrow call
            produce_calls[collection] = produce_calls.get(collection, 0) + 1
            time.sleep(1.5)  # wide overlap window: both tile requests must share one flight
            return []  # no assets: production ends without writing a product
        time.sleep(1.5)
        # catalog query (dates index): offer the requested date
        from mirarsetena.pipeline.catalog import Scene

        return [
            Scene(
                id="fake-2026-10-06",
                collection=collection,
                datetime="2026-10-06T15:00:00Z",
                date="2026-10-06",
                cloud=1.0,
            )
        ]

    app = create_app(
        config_dir=CONFIG_DIR,
        storage_root=tmp_path,
        search_fn=slow_search,
    )
    url = "/p/cdp-rio-general/tiles/mndwi/2026-10-06/17/35073/62103.png"
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: client.get(url), range(6)))

    # the product does not exist (empty search) — status is irrelevant here
    assert all(r.status_code in (200, 404) for r in responses)
    # ONE production shared by the whole wave, not one per request
    assert produce_calls.get("sentinel-2-l2a", 0) == 1

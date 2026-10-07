"""G.4 — /status endpoint + page (SCOPE R3-Q7): cache, catalog, jobs, ETA.

Seams: mirarsetena.status.status_payload (service) and the two routes.
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.jobs.runner import JobQueue
from mirarsetena.pipeline.dates import index_key, refresh_index
from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.status import status_payload
from mirarsetena.storage import LocalStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"
SLUG = "cdp-rio-general"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _seeded(tmp_path, fake_search):
    storage = LocalStore(tmp_path / "cache")
    registry = ProjectRegistry(CONFIG_DIR)
    config = registry.get(SLUG)
    refresh_index(storage, config, search_fn=fake_search, now=NOW)
    storage.put("p/cdp-rio-general/scenes/demo.tif", b"1" * 5)
    storage.put("p/cdp-rio-general/tiles/ndvi/0/0/0.png", b"p" * 100)
    queue = JobQueue(tmp_path / "jobs.db")
    queue.enqueue("catalog_poll", {"slug": SLUG}, dedupe="demo")
    return storage, config, queue


def test_status_payload_contains_all_sections(tmp_path, fake_search):
    storage, config, queue = _seeded(tmp_path, fake_search)
    payload = status_payload(storage, config, queue, now=NOW)

    assert payload["project"] == SLUG
    assert payload["cache"]["by_prefix"]["tiles"] == 100
    assert payload["cache"]["by_prefix"]["scenes"] == 5
    assert payload["cache"]["total_bytes"] >= 105

    assert payload["catalog"]["updated_at"] == "2026-10-07T12:00:00+00:00"
    assert payload["jobs"] == {"pending": 1}

    missions = payload["missions"]
    assert missions["sentinel-2-l2a"] == {
        "last_scene": "2026-10-06",
        "next_acquisition": "2026-10-11",
        "repeat_days": 5,
    }
    assert missions["sentinel-1-grd"]["last_scene"] == "2026-10-03"
    assert missions["sentinel-1-grd"]["next_acquisition"] == "2026-10-15"
    assert missions["sentinel-1-slc"]["last_scene"] is None
    assert missions["sentinel-1-slc"]["next_acquisition"] is None


def test_status_routes(tmp_path, fake_search):
    app = create_app(
        config_dir=CONFIG_DIR,
        storage_root=tmp_path / "cache",
        search_fn=fake_search,
    )
    client = TestClient(app)

    response = client.get(f"/p/{SLUG}/status")
    assert response.status_code == 200
    body = response.json()
    for section in ("project", "cache", "catalog", "jobs", "missions"):
        assert section in body

    assert client.get("/p/unknown/status").status_code == 404

    page = client.get(f"/p/{SLUG}/status/page")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Mirar Setena" in page.text
    assert SLUG in page.text

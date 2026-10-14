"""G.4 — /status endpoint + page (SCOPE R3-Q7): cache, catalog, jobs, ETA.

Seams: mirarsetena.status.status_payload (service) and the two routes.
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.jobs.runner import JobQueue
from mirarsetena.pipeline.dates import refresh_index
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
    assert payload["jobs"]["pending"] == 1
    assert payload["jobs"]["by_type"] == {
        "catalog_poll": {"pending": 1}
    }

    missions = payload["missions"]
    assert missions["sentinel-2-l2a"] == {
        "last_scene": "2026-10-06",
        "next_acquisition": "2026-10-11",
        "repeat_days": 5,
    }
    # radar magnitude layers left the config (2026-10-12); the `insar`
    # layer (INSAR-PLAN) brings sentinel-1-slc back — pair-derived, so its
    # index entries are empty (last_scene None) but the mission is real.
    assert set(missions) == {"sentinel-2-l2a", "sentinel-1-slc"}
    assert missions["sentinel-1-slc"]["last_scene"] is None


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


def test_status_insar_mission_is_pair_aware_with_search_fn(tmp_path, fake_search):
    # The pair-derived mission used to read "0 / null" (outdated: pairs are
    # never catalog-indexed). With the search fn it reports the REAL state:
    # planned pairs, the last pair's end date, the predicted next pass and
    # how many products are actually stored.
    from mirarsetena.pipeline.indices import layer_key

    storage, config, queue = _seeded(tmp_path, fake_search)
    payload = status_payload(
        storage, config, queue, now=NOW, search_fn=fake_search
    )
    slc = payload["missions"]["sentinel-1-slc"]
    # fixture: 6 planned pairs, newest ending 2026-10-03
    assert slc["pairs_planned"] == 6
    assert slc["last_scene"] == "2026-10-03"
    assert slc["next_acquisition"] == "2026-10-15"  # +12 d past 10-07
    assert slc["products_processed"] == 0

    # a stored product flips the count (storage-only truth, no search needed)
    from mirarsetena.pipeline.insar import insar_series_key

    storage.put(layer_key(SLUG, "sentinel-1-slc", "2026-10-03", "insar"), b"t")
    storage.put(insar_series_key(SLUG, "2026-10-03"), b"{}")
    again = status_payload(
        storage, config, queue, now=NOW, search_fn=fake_search
    )
    assert again["missions"]["sentinel-1-slc"]["products_processed"] == 1

    # without the search fn the pair fields degrade honestly to None
    offline = status_payload(storage, config, queue, now=NOW)
    assert offline["missions"]["sentinel-1-slc"]["pairs_planned"] is None
    assert offline["missions"]["sentinel-1-slc"]["products_processed"] == 1


def test_status_jobs_split_parked_retired_types_and_show_errors(tmp_path, fake_search):
    # pending:1 was a coherence_eager job of the RETIRED GRD coherence
    # feature — no handler will ever run it. With runnable_types the
    # payload separates parked jobs from runnable pending, and by_type
    # explains what the failed pile actually is (last error included).
    storage, config, queue = _seeded(tmp_path, fake_search)
    queue.enqueue("coherence_eager", {"slug": SLUG}, dedupe="retired")
    queue.enqueue("insar_pair", {"slug": SLUG}, dedupe="boom1")

    def exploding(_payload):
        raise RuntimeError("CDSE credentials not configured — demo")

    for _ in range(3):  # MAX_ATTEMPTS: the 3rd failure parks it as failed
        queue.run_once({"insar_pair": exploding})

    payload = status_payload(
        storage, config, queue, now=NOW,
        runnable_types=("catalog_poll", "insar_eager", "insar_pair"),
    )
    jobs = payload["jobs"]
    # catalog_poll (the seeded one) is runnable-pending; coherence_eager parks
    assert jobs["pending"] == 1
    assert jobs["parked"] == 1
    assert jobs["by_type"]["coherence_eager"] == {"pending": 1}
    pair = jobs["by_type"]["insar_pair"]
    assert pair["failed"] == 1
    assert "CDSE credentials" in pair["last_error"]

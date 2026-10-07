"""D.4 — layer-driven date list API.

Seams: mirarsetena.pipeline.dates.list_dates (service, injectable clock and
catalog) and the FastAPI route GET /p/{slug}/api/dates.
Golden values from the committed STAC fixtures (25 S2 dates / 8 S1 dates).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.pipeline.catalog import parse_scenes
from mirarsetena.pipeline.dates import list_dates, mission_for_layer
from mirarsetena.pipeline.window import scene_meta_key
from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.storage import LocalStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def fake_search(collection, bbox, start, end):
    name = {
        "sentinel-2-l2a": "stac_s2_l2a.json",
        "sentinel-1-grd": "stac_s1_grd.json",
    }.get(collection)
    if name is None:
        return []
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return parse_scenes(collection, payload)


@pytest.fixture
def storage(tmp_path):
    return LocalStore(tmp_path / "cache")


@pytest.fixture
def registry():
    return ProjectRegistry(CONFIG_DIR)


def test_layer_implies_mission():
    assert mission_for_layer("ndvi") == "sentinel-2-l2a"
    assert mission_for_layer("rgb") == "sentinel-2-l2a"
    assert mission_for_layer("sigma0") == "sentinel-1-grd"
    assert mission_for_layer("coherence") == "sentinel-1-slc"


def test_s2_layer_returns_sorted_dates_with_cloud(storage, registry):
    result = list_dates(
        storage, "cdp-rio-general", registry.get("cdp-rio-general"),
        "ndvi", search_fn=fake_search, now=NOW,
    )
    dates = result["dates"]
    assert len(dates) == 25
    assert [d["date"] for d in dates] == sorted(d["date"] for d in dates)
    assert dates[0]["date"] == "2026-07-03"
    assert all(isinstance(d["cloud"], float) for d in dates)
    assert result["mission"] == "sentinel-2-l2a"
    assert result["timeline"]["start"] == "2026-07-01"
    assert result["timeline"]["works_start"] == "2026-08-01"


def test_s1_layer_returns_eight_dates(storage, registry):
    result = list_dates(
        storage, "cdp-rio-general", registry.get("cdp-rio-general"),
        "sigma0", search_fn=fake_search, now=NOW,
    )
    assert len(result["dates"]) == 8
    assert all(d["cloud"] is None for d in result["dates"])


def test_coherence_layer_is_empty_before_slc_catalog_exists(storage, registry):
    result = list_dates(
        storage, "cdp-rio-general", registry.get("cdp-rio-general"),
        "coherence", search_fn=fake_search, now=NOW,
    )
    assert result["dates"] == []
    assert result["mission"] == "sentinel-1-slc"


def test_max_cloud_filters_dates(storage, registry):
    config = registry.get("cdp-rio-general")
    result = list_dates(
        storage, "cdp-rio-general", config, "ndvi",
        max_cloud=5, search_fn=fake_search, now=NOW,
    )
    assert all(d["cloud"] <= 5 for d in result["dates"])
    full = list_dates(
        storage, "cdp-rio-general", config, "ndvi", search_fn=fake_search, now=NOW
    )
    assert len(result["dates"]) < len(full["dates"])


def test_processed_cloud_overrides_catalog_cloud(storage, registry):
    config = registry.get("cdp-rio-general")
    meta_key = scene_meta_key("cdp-rio-general", "sentinel-2-l2a", "2026-07-03")
    storage.put(meta_key, json.dumps({"cloud": 12.34, "date": "2026-07-03"}).encode())
    result = list_dates(
        storage, "cdp-rio-general", config, "ndvi", search_fn=fake_search, now=NOW
    )
    first = result["dates"][0]
    assert first["date"] == "2026-07-03"
    assert first["cloud"] == pytest.approx(12.34)
    assert first["processed"] is True


def test_index_is_reused_within_ttl_then_refreshed(storage, registry):
    config = registry.get("cdp-rio-general")
    calls = {"n": 0}

    def counting_search(collection, bbox, start, end):
        calls["n"] += 1
        return fake_search(collection, bbox, start, end)

    list_dates(storage, "cdp-rio-general", config, "ndvi",
               search_fn=counting_search, now=NOW)
    after_first = calls["n"]
    assert after_first >= 1

    list_dates(storage, "cdp-rio-general", config, "ndvi",
               search_fn=counting_search, now=NOW + timedelta(hours=5))
    assert calls["n"] == after_first  # fresh index, no re-query

    list_dates(storage, "cdp-rio-general", config, "ndvi",
               search_fn=counting_search, now=NOW + timedelta(hours=7))
    assert calls["n"] > after_first  # stale -> refresh


def test_route_serves_dates_and_maps_errors():
    app = create_app(
        config_dir=CONFIG_DIR, search_fn=fake_search
    )
    client = TestClient(app)
    ok = client.get("/p/cdp-rio-general/api/dates", params={"layer": "ndvi"})
    assert ok.status_code == 200
    body = ok.json()
    assert len(body["dates"]) == 25

    bad_layer = client.get("/p/cdp-rio-general/api/dates", params={"layer": "nope"})
    assert bad_layer.status_code == 400

    missing = client.get("/p/unknown/api/dates", params={"layer": "ndvi"})
    assert missing.status_code == 404

    filtered = client.get(
        "/p/cdp-rio-general/api/dates",
        params={"layer": "ndvi", "max_cloud": 5},
    )
    assert filtered.status_code == 200
    assert all(d["cloud"] <= 5 for d in filtered.json()["dates"])

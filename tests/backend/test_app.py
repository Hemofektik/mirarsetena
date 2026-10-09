"""C.3 — FastAPI app skeleton: app factory, healthz, project-scoped routes.

Seam: mirarsetena.app.create_app (mounted routes + state wiring).
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mirarsetena.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"


# Lifespan runs an immediate catalog poll; keep it offline and instant.
def _offline_search(collection, bbox, start, end):
    return []


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    return create_app(
        config_dir=CONFIG_DIR,
        storage_root=tmp_path_factory.mktemp("cache"),
        search_fn=_offline_search,
    )


@pytest.fixture(scope="module")
def client(app):
    with TestClient(app) as client_:
        yield client_


def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Cold tile requests block worker threads for their whole production;
    # the pool must stay deep enough that cache hits are never queued
    # behind abandoned waves (user report: scrub-back never renders).
    assert body["threadpool_tokens"] >= 128


def test_project_config_route_serves_the_loaded_project(client):
    response = client.get("/p/cdp-rio-general/config")
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "cdp-rio-general"
    assert body["timeline"]["works_start"] == "2026-06-01"
    assert body["cache"]["max_zoom"] == 18


def test_unknown_project_returns_404(client):
    response = client.get("/p/does-not-exist/config")
    assert response.status_code == 404


def test_state_wiring_exposes_registry_and_storage(client):
    app = client.app
    assert "cdp-rio-general" in app.state.registry.all()
    assert app.state.storage is not None


def test_rio_route_serves_the_work_area_line(client):
    """The Río General between the project start/end POIs is the plan's
    work-area boundary: served as a LineString overlay."""
    response = client.get("/p/cdp-rio-general/rio.geojson")
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    feature = payload["features"][0]
    assert feature["geometry"]["type"] == "LineString"
    coords = feature["geometry"]["coordinates"]
    assert len(coords) >= 2
    # ends near the project-start / project-end POIs (the POIs sit on the river)
    assert feature["properties"]["name"] == "Río General"

"""Project geometry routes: the canonical GeoJSON the frontend layers use."""
from pathlib import Path

from fastapi.testclient import TestClient

from mirarsetena.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"
SLUG = "cdp-rio-general"


def test_geo_routes_serve_canonical_geometry():
    app = create_app(config_dir=CONFIG_DIR, storage_root=REPO_ROOT / "data" / "cache")
    client = TestClient(app)

    aoi = client.get(f"/p/{SLUG}/aoi.geojson")
    assert aoi.status_code == 200
    body = aoi.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 2

    pois = client.get(f"/p/{SLUG}/pois.geojson")
    assert pois.status_code == 200
    assert len(pois.json()["features"]) == 10


def test_geo_routes_404_for_unknown_project():
    app = create_app(config_dir=CONFIG_DIR, storage_root=REPO_ROOT / "data" / "cache")
    client = TestClient(app)
    assert client.get("/p/unknown/aoi.geojson").status_code == 404


def test_geo_routes_are_never_cached():
    """Geometry is re-anchored server-side; a stale browser cache would keep
    drawing the old perimeter after a data fix. These responses must always
    be revalidated."""
    app = create_app(config_dir=CONFIG_DIR, storage_root=REPO_ROOT / "data" / "cache")
    client = TestClient(app)
    for route in ("aoi.geojson", "pois.geojson"):
        response = client.get(f"/p/{SLUG}/{route}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store", route

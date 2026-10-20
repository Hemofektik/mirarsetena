"""Project geometry routes: the canonical GeoJSON the frontend layers use."""
from pathlib import Path

from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.storage import LocalStore

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


def test_icesat_tracks_endpoint_serves_clipped_features(tmp_path):
    # ICESat-2 ground tracks over the AOI (user 2026-10-20): open CMR
    # metadata, cached in storage; the fixture has 24 real passes.
    import json as _json

    from mirarsetena.app import create_app

    calls = []

    def fixture_fetch(bbox):
        calls.append(bbox)
        from mirarsetena.icesat import granules_to_features

        payload = _json.loads(
            (REPO_ROOT / "tests" / "fixtures" / "cmr_atl06.json").read_text(
                encoding="utf-8"
            )
        )
        return {"type": "FeatureCollection",
                "features": granules_to_features(payload, bbox)}

    app = create_app(
        config_dir=CONFIG_DIR, storage_root=tmp_path / "cache",
        icesat_fetch=fixture_fetch,
    )
    client = TestClient(app)
    res = client.get("/p/cdp-rio-general/icesat.geojson")
    assert res.status_code == 200
    body = res.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) >= 20  # the fixture's 24 passes
    dates = [f["properties"]["date"] for f in body["features"]]
    assert dates == sorted(dates)
    assert "2026-05-09" in dates
    # second call is served from storage (the fetch ran exactly once)
    res2 = client.get("/p/cdp-rio-general/icesat.geojson")
    assert res2.json() == body
    assert len(calls) == 1
    assert client.get("/p/unknown/icesat.geojson").status_code == 404


def test_icesat_elevations_endpoint_serves_cached_points(tmp_path):
    # The elevation layer: whatever tools/icesat_elevations stored is
    # served; an empty store yields an empty (honest) collection — the
    # layer lights up the moment the tool runs with Earthdata credentials.
    import json as _json

    from mirarsetena.app import create_app
    from mirarsetena.projects.registry import cache_key

    app = create_app(config_dir=CONFIG_DIR, storage_root=tmp_path / "cache")
    client = TestClient(app)

    empty = client.get("/p/cdp-rio-general/icesat-elevations.geojson")
    assert empty.status_code == 200
    assert empty.json() == {"type": "FeatureCollection", "features": []}

    store = LocalStore(tmp_path / "cache")
    store.put(
        cache_key("cdp-rio-general", "catalog", "icesat-elevations.json"),
        _json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"date": "2026-05-09", "elev_m": 1123.4},
                "geometry": {"type": "Point", "coordinates": [-83.6675, 9.3875]},
            }],
        }).encode(),
    )
    served = client.get("/p/cdp-rio-general/icesat-elevations.geojson")
    assert served.status_code == 200
    assert served.json()["features"][0]["properties"]["elev_m"] == 1123.4
    assert client.get("/p/unknown/icesat-elevations.geojson").status_code == 404

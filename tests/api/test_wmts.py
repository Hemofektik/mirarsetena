"""F.3 — OGC WMTS 1.0.0 (KVP) + XYZ tiles over one handler.

Seams: GET /p/{slug}/wmts (GetCapabilities + GetTile) and
GET /p/{slug}/tiles/{layer}/{date}/{z}/{x}/{y}.png.
"""
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.pipeline.indices import layer_key

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"
SLUG = "cdp-rio-general"
WMTS_NS = "{http://www.opengis.net/wmts/1.0}"
OWS_NS = "{http://www.opengis.net/ows/1.1}"


@pytest.fixture
def client(tmp_path, fake_search, world_geotiff):
    app = create_app(
        config_dir=CONFIG_DIR,
        storage_root=tmp_path / "cache",
        search_fn=fake_search,
        processor=lambda layer, date: pytest.fail(
            "processor must not run when products are pre-seeded"
        ),
    )
    storage = app.state.storage
    # Seed layer products for the tested dates (level-2 products).
    storage.put(
        layer_key(SLUG, "sentinel-2-l2a", "2026-07-03", "ndvi"),
        world_geotiff([0.5], nodata=-9999.0),
    )
    return TestClient(app)


def test_getcapabilities_lists_one_layer_per_date(client):
    response = client.get(
        f"/p/{SLUG}/wmts", params={"SERVICE": "WMTS", "REQUEST": "GetCapabilities"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(response.content)
    assert root.tag == f"{WMTS_NS}Capabilities"
    assert root.get("version") == "1.0.0"

    names = [
        layer.find(f"{OWS_NS}Identifier").text
        for layer in root.findall(f".//{WMTS_NS}Layer")
    ]
    # 4 optical layers x 25 S2 dates + the InSAR layer's 6 pair end-dates
    # (INSAR-PLAN L.5: radar layers left the config 2026-10-12; insar returns)
    assert len(names) == 4 * 25 + 6
    assert "ndvi_2026-07-03" in names
    assert "rgb_2026-10-06" in names
    assert "insar_2026-10-03" in names  # a InSAR pair end-date
    assert all(name.rsplit("_", 1)[0] in {"rgb", "ndvi", "mndwi", "bsi", "insar"}
               for name in names)


def test_getcapabilities_carries_wgs84_bounding_box(client):
    """QGIS uses it to place the layer (otherwise: world-extent artifact)."""
    response = client.get(
        f"/p/{SLUG}/wmts", params={"SERVICE": "WMTS", "REQUEST": "GetCapabilities"}
    )
    root = ElementTree.fromstring(response.content)
    layer = next(
        l for l in root.findall(f".//{WMTS_NS}Layer")
        if l.find(f"{OWS_NS}Identifier").text == "ndvi_2026-07-03"
    )
    bbox = layer.find(f"{OWS_NS}WGS84BoundingBox")
    assert bbox is not None
    lower = bbox.find(f"{OWS_NS}LowerCorner").text.split()
    upper = bbox.find(f"{OWS_NS}UpperCorner").text.split()
    lon_w, lat_s = map(float, lower)
    lon_e, lat_n = map(float, upper)
    assert lon_w < lon_e and lat_s < lat_n
    # project bbox from config: [-83.675, 9.378, -83.660, 9.397]
    assert lon_w == pytest.approx(-83.675, abs=1e-3)
    assert lat_n == pytest.approx(9.397, abs=1e-3)


def test_gettile_returns_png_for_seeded_date(client):
    response = client.get(
        f"/p/{SLUG}/wmts",
        params={
            "SERVICE": "WMTS", "REQUEST": "GetTile",
            "LAYER": "ndvi_2026-07-03", "TILEMATRIXSET": "GoogleMapsCompatible",
            "TILEMATRIX": "0", "TILEROW": "0", "TILECOL": "0",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_gettile_unknown_date_or_layer_404(client):
    params = {
        "SERVICE": "WMTS", "REQUEST": "GetTile",
        "TILEMATRIXSET": "GoogleMapsCompatible",
        "TILEMATRIX": "0", "TILEROW": "0", "TILECOL": "0",
    }
    assert client.get(f"/p/{SLUG}/wmts",
                      params={**params, "LAYER": "ndvi_2099-01-01"}).status_code == 404
    assert client.get(f"/p/{SLUG}/wmts",
                      params={**params, "LAYER": "nonsense_2026-07-03"}).status_code == 404


def test_gettile_rejects_zoom_beyond_cap(client):
    response = client.get(
        f"/p/{SLUG}/wmts",
        params={
            "SERVICE": "WMTS", "REQUEST": "GetTile",
            "LAYER": "ndvi_2026-07-03", "TILEMATRIXSET": "GoogleMapsCompatible",
            "TILEMATRIX": "19", "TILEROW": "0", "TILECOL": "0",
        },
    )
    assert response.status_code == 400


def test_xyz_and_wmts_return_byte_identical_tiles(client):
    wmts = client.get(
        f"/p/{SLUG}/wmts",
        params={
            "SERVICE": "WMTS", "REQUEST": "GetTile",
            "LAYER": "ndvi_2026-07-03", "TILEMATRIXSET": "GoogleMapsCompatible",
            "TILEMATRIX": "0", "TILEROW": "0", "TILECOL": "0",
        },
    )
    xyz = client.get(f"/p/{SLUG}/tiles/ndvi/2026-07-03/0/0/0.png")
    assert wmts.status_code == 200 and xyz.status_code == 200
    assert wmts.content == xyz.content


def _radar_config_dir(tmp_path):
    """Config dir with sigma0 re-added: the display config dropped the
    radar layers (2026-10-12) but this dormant machinery is kept covered
    for the InSAR return. The config now also carries the `insar` layer."""
    src = (CONFIG_DIR / f"{SLUG}.yaml").read_text(encoding="utf-8")
    patched = src.replace(
        "layers: [rgb, ndvi, mndwi, bsi, insar]",
        "layers: [rgb, ndvi, mndwi, bsi, sigma0]",
    )
    assert patched != src, "layers line not found"
    out = tmp_path / "config"
    out.mkdir()
    (out / f"{SLUG}.yaml").write_text(patched, encoding="utf-8")
    return out


def test_composite_baseline_uses_all_preworks_products(tmp_path, fake_search, world_geotiff):
    """User (2026-10-12): aggregate ALL pre-works Sentinel data
    (timeline.start .. works_start) as the baseline — validated offline
    at 5x the works signal of a single-date baseline (quarry strong
    15.5% vs control 0.0%). baseline=composite renders the per-pixel
    median of every processed pre-works product; with fewer than two it
    falls back to the date-based default."""
    from fastapi.testclient import TestClient

    from mirarsetena.app import create_app
    from mirarsetena.pipeline.catalog import Scene

    def spanning_search(collection, bbox, start, end):
        """Catalog spanning pre-works (before 2026-06-01) and post-works
        dates so the composite has products to aggregate over."""
        if collection != "sentinel-1-grd":
            return []
        days = ["2026-02-10", "2026-03-06", "2026-04-11", "2026-05-05", "2026-07-11"]
        return [
            Scene(
                id=f"fake-{d}", collection=collection,
                datetime=f"{d}T12:00:00Z", date=d, cloud=1.0,
            )
            for d in days
        ]

    app = create_app(
        config_dir=_radar_config_dir(tmp_path),
        storage_root=tmp_path / "cache",
        search_fn=spanning_search,
        processor=lambda layer, date: pytest.fail("no processing for pre-seeded products"),
    )
    storage = app.state.storage
    # four pre-works products (before works_start 2026-06-01) + one post
    for day in ("2026-02-10", "2026-03-06", "2026-04-11", "2026-05-05"):
        storage.put(
            layer_key(SLUG, "sentinel-1-grd", day, "sigma0"),
            world_geotiff([-10.0], nodata=-9999.0),
        )
    storage.put(
        layer_key(SLUG, "sentinel-1-grd", "2026-07-11", "sigma0"),
        world_geotiff([-10.0], nodata=-9999.0),
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/p/{SLUG}/tiles/sigma0/2026-07-11/0/0/0.png",
            params={"baseline": "composite", "mode": "change"},
        )
        assert resp.status_code == 200
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        # all sources -10 dB: composite -10, delta 0 -> neutral (class 3)
        assert img.getpixel((128, 128)) == (240, 240, 240, 255)



def test_composite_baseline_falls_back_below_two_products(tmp_path, world_geotiff):
    """Fewer than two processed pre-works products -> nothing to
    aggregate over -> the date-based default (whose product is not
    seeded here -> 404)."""
    from fastapi.testclient import TestClient

    from mirarsetena.app import create_app
    from mirarsetena.pipeline.catalog import Scene

    def spanning_search(collection, bbox, start, end):
        if collection != "sentinel-1-grd":
            return []
        days = ["2026-02-10", "2026-03-06", "2026-04-11", "2026-05-05", "2026-07-11"]
        return [
            Scene(id=f"fake-{d}", collection=collection,
                  datetime=f"{d}T12:00:00Z", date=d, cloud=1.0)
            for d in days
        ]

    processed: list[tuple[str, str]] = []

    def noop_processor(layer, date):
        # the fallback default runs the on-demand processor (it would
        # download/process that date in production); here it writes
        # nothing, so the request resolves to 404 once the product is
        # still missing after processing
        processed.append((layer, date))

    app = create_app(
        config_dir=_radar_config_dir(tmp_path),
        storage_root=tmp_path / "cache",
        search_fn=spanning_search,
        processor=noop_processor,
    )
    app.state.storage.put(
        layer_key(SLUG, "sentinel-1-grd", "2026-02-10", "sigma0"),
        world_geotiff([-10.0], nodata=-9999.0),
    )
    app.state.storage.put(
        layer_key(SLUG, "sentinel-1-grd", "2026-07-11", "sigma0"),
        world_geotiff([-10.0], nodata=-9999.0),
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/p/{SLUG}/tiles/sigma0/2026-07-11/0/0/0.png",
            params={"baseline": "composite", "mode": "change"},
        )
        assert resp.status_code == 404
        # the date-based default was resolved and produced on demand
        assert processed == [("sigma0", "2026-05-05")]


def test_xyz_known_date_without_product_is_blank_200_not_404(tmp_path):
    """Route contract for the map client: a scrubber date whose product is
    not computed yet returns 200 + a transparent PNG. A 404 here sends
    MapLibre down its tile-error path (ancestor retries z-1..z0 + loader
    veils), which is what made 09-09 and 10-03 look identical."""
    from fastapi.testclient import TestClient

    from mirarsetena.app import create_app
    from mirarsetena.pipeline.catalog import Scene

    def search_with_dates(collection, bbox, start, end):
        if collection != "sentinel-1-grd":
            return []
        days = ["2026-05-05", "2026-07-11"]
        return [
            Scene(id=f"fake-{d}", collection=collection,
                  datetime=f"{d}T12:00:00Z", date=d, cloud=1.0)
            for d in days
        ]

    app = create_app(
        config_dir=_radar_config_dir(tmp_path),
        storage_root=tmp_path / "cache",
        search_fn=search_with_dates,
        processor=lambda layer, date: None,  # writes nothing: no product
    )
    with TestClient(app) as client:
        # known date, no product -> blank tile
        resp = client.get(f"/p/{SLUG}/tiles/sigma0/2026-07-11/0/0/0.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        import numpy as np

        assert (np.asarray(img)[..., 3] == 0).all()
        # unknown date (not in the layer's date list) -> still 404
        assert client.get(
            f"/p/{SLUG}/tiles/sigma0/2099-01-01/0/0/0.png"
        ).status_code == 404

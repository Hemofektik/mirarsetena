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
    storage.put(
        layer_key(SLUG, "sentinel-1-grd", "2026-07-11", "sigma0"),
        world_geotiff([-10.0], nodata=-9999.0),
    )
    storage.put(
        layer_key(SLUG, "sentinel-1-grd", "2026-07-23", "sigma0"),
        world_geotiff([-10.0], nodata=-9999.0),
    )
    # works start 2026-06-01 leaves no pre-works fixture date, so the
    # geometry-matched default baseline for the asc end 2026-07-11 is the
    # latest asc date: 2026-09-09 (seeded so the default-baseline tests
    # never trigger the processor)
    storage.put(
        layer_key(SLUG, "sentinel-1-grd", "2026-09-09", "sigma0"),
        world_geotiff([-10.0], nodata=-9999.0),
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
    # 4 optical layers x 25 S2 dates + sigma0 x 8 S1 dates (+0 coherence)
    assert len(names) == 4 * 25 + 8
    assert "ndvi_2026-07-03" in names
    assert "rgb_2026-10-06" in names
    assert "sigma0_2026-07-11" in names
    assert all(name.rsplit("_", 1)[0] in
               {"rgb", "ndvi", "mndwi", "bsi", "sigma0", "coherence"}
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


def test_xyz_and_wmts_return_byte_identical_change_tiles(client):
    wmts = client.get(
        f"/p/{SLUG}/wmts",
        params={
            "SERVICE": "WMTS", "REQUEST": "GetTile",
            "LAYER": "sigma0_2026-07-11", "TILEMATRIXSET": "GoogleMapsCompatible",
            "TILEMATRIX": "0", "TILEROW": "0", "TILECOL": "0",
        },
    )
    xyz = client.get(f"/p/{SLUG}/tiles/sigma0/2026-07-11/0/0/0.png")
    assert wmts.status_code == 200 and xyz.status_code == 200
    assert wmts.content == xyz.content


def test_default_baseline_change_renders_neutral_class(client):
    """Scene and pre-works baseline both -10 dB -> delta 0 -> class 3 gray."""
    response = client.get(f"/p/{SLUG}/tiles/sigma0/2026-07-11/0/0/0.png")
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(response.content)).convert("RGBA")
    assert img.getpixel((128, 128)) == (240, 240, 240, 255)


def test_raw_mode_renders_grayscale_ramp(client):
    response = client.get(
        f"/p/{SLUG}/tiles/sigma0/2026-07-11/0/0/0.png", params={"mode": "raw"}
    )
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(response.content)).convert("RGBA")
    pixel = img.getpixel((128, 128))
    # -10 dB sits between ramp stops (-14,120) and (-5,240): ~173 gray
    assert pixel[0] == pytest.approx(173, abs=3)
    assert pixel[0] == pixel[1] == pixel[2]
    assert pixel[3] == 255

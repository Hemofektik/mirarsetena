"""INSAR-PLAN L.1 spike support — local DEM for topsApp from AWS terrarium.

ISCE2's default DEM source (USGS e4ftl01) needs Earthdata login and is not
reachable here; the AWS elevation-tiles-prod terrarium tiles ARE reachable
(they already power the app's 3D view). This builds a small geographic
GeoTIFF DEM over the AOI bbox that topsApp's `demfilename` accepts.

Seam: mirarsetena.coherence.dem (pure decode + tile assembly; the HTTP fetch
is injectable so tests run offline).
"""
import numpy as np
import pytest
import rasterio

from mirarsetena.coherence.dem import (
    aoi_tiles,
    build_dem,
    decode_terrarium,
    terrarium_url,
)

BBOX = (-83.675, 9.378, -83.660, 9.397)  # west, south, east, north


def test_decode_terrarium_golden():
    # terrarium encoding: elevation = R*256 + G + B/256 - 32768
    rgb = np.array([[[128, 128, 0]]], dtype=np.uint8)  # 128*256+128 = 32896
    assert decode_terrarium(rgb)[0, 0] == pytest.approx(32896 - 32768)
    rgb0 = np.array([[[127, 128, 128]]], dtype=np.uint8)  # 127*256+128+0.5
    assert decode_terrarium(rgb0)[0, 0] == pytest.approx(
        127 * 256 + 128 + 128 / 256 - 32768
    )


def test_terrarium_url_golden():
    url = terrarium_url(13, 2191, 3881)
    assert url == (
        "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/13/2191/3881.png"
    )


def test_aoi_tiles_covers_bbox():
    tiles = aoi_tiles(BBOX, zoom=13)
    assert len(tiles) >= 1
    # every tile is a valid slippy triple at the requested zoom
    assert all(t[0] == 13 for t in tiles)
    # the AOI spans ~1.6 km: one z13 tile (≈4.8 km) must cover it
    xs = {t[1] for t in tiles}
    ys = {t[2] for t in tiles}
    assert len(xs) <= 2 and len(ys) <= 2


def test_build_dem_writes_geographic_geotiff(tmp_path):
    """A single-tile DEM assembles into a north-up EPSG:4326 GeoTIFF whose
    bounds cover the requested bbox (what topsApp's verifyDEM checks)."""
    # one 256x256 terrarium tile of constant 1169 m (AOI centre elevation)
    elev = 1169.0
    raw = np.full((256, 256, 3), 0, dtype=np.uint8)
    value = elev + 32768
    raw[..., 0] = int(value) // 256
    raw[..., 1] = int(value) % 256
    raw[..., 2] = 0
    z, x, y = 13, 2191, 3881  # the AOI's tile at z13

    def fetch(tile_z, tile_x, tile_y):
        assert tile_z == z and tile_y == y
        assert tile_x in (x, x + 1)  # AOI spans two tiles in x at z13
        return raw

    out = tmp_path / "aoi.dem.tif"
    build_dem(BBOX, out, zoom=13, fetch=fetch)

    with rasterio.open(out) as src:
        assert src.crs.to_string() == "EPSG:4326"
        west, south, east, north = src.bounds
        # DEM covers the AOI (tile granularity)
        assert west <= BBOX[0] + 1e-6
        assert east >= BBOX[2] - 1e-6
        assert south <= BBOX[1] + 1e-6
        assert north >= BBOX[3] - 1e-6
        # north-up, no rotation
        assert src.transform.b == 0 and src.transform.d == 0
        data = src.read(1)
        assert data.dtype == np.float32
        assert float(np.median(data)) == pytest.approx(elev, abs=1.0)

def test_build_dem_is_raw_binary_isce_can_memmap(tmp_path):
    """ISCE reads the DEM through its Image class, which memmaps the file as
    raw BSQ float32 using the .xml sidecar's width/length — it never decodes
    GeoTIFF (gdal2isce_xml only writes the sidecar; it does not re-encode).
    Image.createImage hard-checks `os.path.getsize(file) == width*length*4`
    and sys.exit(1s otherwise, so a deflate-compressed GeoTIFF is unreadable:
    the 15 MB compressed file only "covers" 1047 of 5376 rows, which is why
    burst-1 geometry was void north of lat 9.3542 (spike run 2026-10-13).

    So the DEM must be stored as uncompressed raw float32 whose byte size
    matches its declared raster dimensions exactly."""
    raw = np.full((256, 256, 3), 0, dtype=np.uint8)
    v = 1169.0 + 32768
    raw[..., 0] = int(v) // 256
    raw[..., 1] = int(v) % 256

    out = tmp_path / "aoi.dem.tif"
    build_dem(BBOX, out, zoom=13, fetch=lambda z, x, y: raw)

    with rasterio.open(out) as src:
        expected = src.width * src.height * 4  # single-band float32
    assert out.stat().st_size == expected, (
        f"DEM must be raw float32 for ISCE's memmap: {out.stat().st_size} "
        f"on disk vs {expected} expected for the declared raster"
    )


def test_build_dem_sanitizes_ocean_bathymetry(tmp_path):
    """Terrarium decodes ocean floors as bathymetry (down to -14 km here),
    but ISCE's geoid corrector turns out-of-range heights into FLT_MAX
    garbage (3.4e38), which overflows topo into the NaNs that abort snaphu
    (spike run 2026-10-13). The radar sees the SEA SURFACE over water, so
    sea-floor depths are clamped to 0 m — SRTM-like land values only."""

    def fetch(tz, tx, ty):
        raw = np.full((256, 256, 3), 0, dtype=np.uint8)
        # encode -14651 m: value + 32768 = 18117 -> R=70, G=197, B=85
        v = -14651 + 32768
        raw[..., 0] = v // 256
        raw[..., 1] = v % 256
        raw[..., 2] = 85
        return raw

    out = tmp_path / "ocean.dem.tif"
    build_dem(BBOX, out, zoom=13, fetch=fetch)
    with rasterio.open(out) as src:
        data = src.read(1)
    assert float(data.min()) >= 0.0  # bathymetry clamped to sea surface
    assert not np.isnan(data).any()

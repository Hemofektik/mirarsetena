"""D.2 — AOI window extraction, mosaicking, level-1 cache.

Seam: mirarsetena.pipeline.window (read_window / mosaic / cached_window /
window_key). Synthetic GeoTIFFs provide the georeferencing ground truth.
"""
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from mirarsetena.pipeline.window import (
    cached_window,
    mosaic,
    read_window,
    window_key,
)
from mirarsetena.storage import LocalStore

BBOX = (-83.672, 9.380, -83.664, 9.392)  # west, south, east, north


def _write_geotiff(path: Path, west, south, east, north, value=100, size=60):
    transform = from_origin(west, north, (east - west) / size, (north - south) / size)
    data = np.full((size, size), value, dtype=np.uint16)
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1,
        dtype="uint16", crs="EPSG:4326", transform=transform, nodata=0,
    ) as dst:
        dst.write(data, 1)
    return path


@pytest.fixture
def full_tile(tmp_path):
    return _write_geotiff(tmp_path / "full.tif", -83.68, 9.37, -83.66, 9.40)


def test_read_window_clips_to_bbox(full_tile):
    data = read_window(str(full_tile), BBOX)
    with MemoryFile(data) as memfile, memfile.open() as src:
        west, south, east, north = src.bounds
        pixel = abs(src.transform.a)
        assert west == pytest.approx(BBOX[0], abs=pixel)
        assert south == pytest.approx(BBOX[1], abs=pixel)
        assert east == pytest.approx(BBOX[2], abs=pixel)
        assert north == pytest.approx(BBOX[3], abs=pixel)
        assert src.crs.to_epsg() == 4326
        assert src.width > 0 and src.height > 0
        assert src.read(1).shape == (src.height, src.width)


def test_mosaic_covers_bbox_across_two_tiles(tmp_path):
    west_tile = _write_geotiff(tmp_path / "w.tif", -83.68, 9.37, -83.668, 9.40, value=1)
    east_tile = _write_geotiff(tmp_path / "e.tif", -83.668, 9.37, -83.66, 9.40, value=2)
    combined = mosaic(
        read_window(str(west_tile), BBOX),
        read_window(str(east_tile), BBOX),
    )
    with MemoryFile(combined) as memfile, memfile.open() as src:
        west, south, east, north = src.bounds
        assert west <= BBOX[0] and east >= BBOX[2]
        assert south <= BBOX[1] and north >= BBOX[3]
        values = set(np.unique(src.read(1)).tolist())
        assert {1, 2} <= values  # both tiles contributed


def test_second_fetch_is_a_cache_hit(full_tile):
    storage = LocalStore(Path(full_tile).parent / "cache")
    calls = {"n": 0}

    def counting_opener(uri, bbox):
        calls["n"] += 1
        return read_window(uri, bbox)

    key = window_key("cdp-rio-general", "sentinel-2-l2a", "2026-07-03")
    first = cached_window(storage, key, [str(full_tile)], BBOX, opener=counting_opener)
    second = cached_window(storage, key, [str(full_tile)], BBOX, opener=counting_opener)
    assert calls["n"] == 1  # second call never touched the source
    assert first == second == storage.get(key)


def test_window_keys_are_project_namespaced():
    key_a = window_key("proj-a", "sentinel-1-grd", "2026-07-11")
    key_b = window_key("proj-b", "sentinel-1-grd", "2026-07-11")
    assert key_a.startswith("p/proj-a/")
    assert key_b.startswith("p/proj-b/")
    assert key_a != key_b


def test_read_window_from_gcp_georeferenced_source(tmp_path):
    """Sentinel-1 measurement COGs georeference via GCPs, not a
    geotransform — the window reader must derive the affine from them."""
    from rasterio.control import GroundControlPoint as GCP
    from rasterio.crs import CRS

    size = 60
    place = from_origin(-83.68, 9.40, 0.001, 0.001)
    path = tmp_path / "gcp.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1,
        dtype="uint16",
    ) as dst:
        dst.write(np.full((size, size), 42, dtype=np.uint16), 1)
        gcps = []
        for row in (0, size // 2, size - 1):
            for col in (0, size // 2, size - 1):
                x, y = place * (col, row)
                gcps.append(GCP(row=row, col=col, x=x, y=y))
        dst.gcps = (gcps, CRS.from_epsg(4326))

    data = read_window(str(path), BBOX)
    with MemoryFile(data) as memfile, memfile.open() as src:
        assert src.crs is not None and src.crs.to_epsg() == 4326
        west, south, east, north = src.bounds
        pixel = 0.001
        assert west == pytest.approx(BBOX[0], abs=pixel)
        assert south == pytest.approx(BBOX[1], abs=pixel)
        assert east == pytest.approx(BBOX[2], abs=pixel)
        assert north == pytest.approx(BBOX[3], abs=pixel)
        assert src.read(1).shape == (src.height, src.width)


def test_mosaic_reprojects_mixed_crs_tiles(tmp_path):
    """Live regression: the AOI straddles MGRS zones 16/17, so dual-tile
    days arrive in EPSG:32616 AND EPSG:32617 (fixture: 16PHR + 17PKL)."""
    from rasterio.transform import from_bounds
    from rasterio.warp import transform_bounds

    west_tile = _write_geotiff(tmp_path / "w4326.tif", -83.68, 9.37, -83.668, 9.40, value=1)

    # East tile authored natively in UTM 17N (like the real S2 zone-17 tile).
    corners = transform_bounds(
        "EPSG:4326", "EPSG:32617", -83.668, 9.37, -83.66, 9.40, densify_pts=21
    )
    transform = from_bounds(*corners, 60, 60)
    data = np.full((60, 60), 2, dtype=np.uint16)
    east_tile = tmp_path / "e32617.tif"
    with rasterio.open(
        east_tile, "w", driver="GTiff", height=60, width=60, count=1,
        dtype="uint16", crs="EPSG:32617", transform=transform, nodata=0,
    ) as dst:
        dst.write(data, 1)

    combined = mosaic(
        read_window(str(west_tile), BBOX),
        read_window(str(east_tile), BBOX),
    )
    with MemoryFile(combined) as memfile, memfile.open() as src:
        west, south, east, north = src.bounds
        assert west <= BBOX[0] and east >= BBOX[2]
        assert south <= BBOX[1] and north >= BBOX[3]
        values = set(np.unique(src.read(1)).tolist())
        assert {1, 2} <= values  # both zones contributed

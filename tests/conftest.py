"""Shared test fixtures: synthetic world-covering GeoTIFFs and fake catalogs."""
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mirarsetena.pipeline.catalog import parse_scenes

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_search():
    """Catalog search backed by the committed STAC fixtures (offline)."""

    def _search(collection, bbox, start, end):
        name = {
            "sentinel-2-l2a": "stac_s2_l2a.json",
            "sentinel-1-grd": "stac_s1_grd.json",
        }.get(collection)
        if name is None:
            return []
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        return parse_scenes(collection, payload)

    return _search


@pytest.fixture
def world_geotiff():
    """Factory: constant-valued GeoTIFF covering the whole Web Mercator world."""

    def _make(values, dtype="float32", nodata=None, count=None):
        values = list(values)
        count = count or len(values)
        height, width = 18, 36
        transform = from_origin(-180, 85.051129, 360 / width, 170.102258 / height)
        profile = {
            "driver": "GTiff", "dtype": dtype, "count": count,
            "height": height, "width": width, "crs": "EPSG:4326",
            "transform": transform, "nodata": nodata, "compress": "deflate",
        }
        with rasterio.io.MemoryFile() as mem:
            with mem.open(**profile) as dst:
                for band in range(count):
                    dst.write(
                        np.full((height, width), values[band], dtype=dtype),
                        band + 1,
                    )
            return mem.read()

    return _make

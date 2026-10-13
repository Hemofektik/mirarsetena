"""Crop-chain product build: radar phase+coherence → referenced LOS GeoTIFF.

Seam: mirarsetena.coherence.crop_chain.

The production path for a pair whose merged ISCE products already exist
(local data prep — INSAR-PLAN L.1 fast path): geocode the AOI crop, apply
the height delta, and re-zero against a stable reference area. Measured on
the spike (2026-10-03): unreferenced the product sat at −6.20 cm median
(100% saturated at the ±5 cm ramp); referenced to coherent pixels outside
the official perimeter it reads median 0.00, p1 −0.40, p99 +0.56, 0%
saturation — so referencing is part of the product, not an optional polish.
"""
from __future__ import annotations

import numpy as np
from rasterio.io import MemoryFile

from mirarsetena.coherence.crop_chain import (
    build_crop_product,
    reference_mask_outside,
)
from mirarsetena.pipeline.indices import NODATA


def _radar_fields(rows=6, cols=8):
    r = np.arange(rows, dtype=np.float64)[:, None]
    c = np.arange(cols, dtype=np.float64)[None, :]
    lat = np.broadcast_to(9.380 + r * 0.002 + c * 0.0003, (rows, cols)).copy()
    lon = np.broadcast_to(-83.670 + c * 0.002 + r * 0.0004, (rows, cols)).copy()
    return lat, lon


def _read(product):
    with MemoryFile(product) as mem, mem.open() as src:
        return src.read(1), src.transform, src.nodata


def test_build_crop_product_geocodes_and_references():
    lat, lon = _radar_fields()
    bbox = (lon.min(), lat.min(), lon.max(), lat.max())
    # constant phase -> constant LOS; the reference row re-zeroes it to 0
    phase = np.full(lat.shape, np.pi, dtype=np.float32)
    coherence = np.full(lat.shape, 0.9, dtype=np.float32)

    product = build_crop_product(
        phase, coherence, lat, lon, bbox,
        resolution=0.0005, reference=None,
    )
    data, _transform, nodata = _read(product)
    assert nodata == NODATA
    valid = data != NODATA
    assert valid.any()
    # unreferenced: every coherent pixel carries +lambda/4 cm
    assert np.allclose(data[valid], 1.3866441, atol=1e-4)

    # reference = the northernmost output row (all coherent)
    reference = np.zeros(data.shape, dtype=bool)
    reference[0, :] = True
    referenced = build_crop_product(
        phase, coherence, lat, lon, bbox,
        resolution=0.0005, reference=reference,
    )
    data2, _, _ = _read(referenced)
    valid2 = data2 != NODATA
    assert np.allclose(data2[valid2], 0.0, atol=1e-4)


def test_build_crop_product_masks_low_coherence():
    lat, lon = _radar_fields()
    bbox = (lon.min(), lat.min(), lon.max(), lat.max())
    phase = np.full(lat.shape, np.pi, dtype=np.float32)
    coherence = np.full(lat.shape, 0.9, dtype=np.float32)
    coherence[2, 3] = 0.1  # below the 0.35 threshold

    product = build_crop_product(
        phase, coherence, lat, lon, bbox, resolution=0.0005
    )
    data, transform, _ = _read(product)
    # the incoherent radar pixel's footprint reads nodata on the product grid
    lon0, lat0 = lon[2, 3], lat[2, 3]
    col = int((lon0 - transform.c) / transform.a)
    row = int((lat0 - transform.f) / transform.e)
    assert data[row, col] == NODATA


def test_reference_mask_outside_marks_only_area_outside_the_polygon():
    # product grid: 4x4 over a 0.004 deg box; polygon covers the EAST half
    from rasterio.transform import from_origin

    transform = from_origin(-83.670, 9.390, 0.001, 0.001)
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": 4, "width": 4, "crs": "EPSG:4326",
        "transform": transform, "nodata": NODATA,
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(np.zeros((4, 4), dtype=np.float32), 1)
        product = mem.read()

    polygon = {
        "type": "Polygon",
        "coordinates": [[
            [-83.668, 9.386], [-83.666, 9.386],
            [-83.666, 9.390], [-83.668, 9.390], [-83.668, 9.386],
        ]],
    }
    mask = reference_mask_outside(product, [polygon])
    assert mask.shape == (4, 4)
    # columns 0-1 are west of the polygon -> reference; 2-3 inside -> not
    assert mask[:, 0].all() and mask[:, 1].all()
    assert not mask[:, 2].any() and not mask[:, 3].any()


def test_reference_mask_outside_without_polygon_is_all_false():
    from rasterio.transform import from_origin

    transform = from_origin(-83.670, 9.390, 0.001, 0.001)
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": 4, "width": 4, "crs": "EPSG:4326",
        "transform": transform, "nodata": NODATA,
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(np.zeros((4, 4), dtype=np.float32), 1)
        product = mem.read()
    # no official perimeter configured -> nothing to reference against
    assert not reference_mask_outside(product, []).any()
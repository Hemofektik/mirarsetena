"""Crop-chain product build: radar phase+coherence → referenced LOS GeoTIFF.

The production path for a pair whose merged ISCE products already exist
(local data prep — INSAR-PLAN L.1 fast path): geocode the AOI crop to the
project product grid, apply the height delta (phase → LOS cm, coherence
masked), and re-zero against a stable reference area.

Measured on the spike (2026-10-03): unreferenced the product sat at
−6.20 cm median (100% saturated at the ±5 cm ramp); referenced to coherent
pixels outside the official perimeter it reads median 0.00, p1 −0.40,
p99 +0.56, 0% saturation — so referencing is part of the product, not an
optional polish.
"""
from __future__ import annotations

import numpy as np
from rasterio.features import geometry_mask
from rasterio.io import MemoryFile

from mirarsetena.coherence.geocode import geocode_radar_band
from mirarsetena.pipeline.insar import (
    DEFAULT_COHERENCE_THRESHOLD,
    build_los_product,
    reference_los,
)

Bbox = tuple[float, float, float, float]  # west, south, east, north


def reference_mask_outside(
    product_bytes: bytes, polygons: list[dict]
) -> np.ndarray:
    """Boolean mask of product pixels OUTSIDE every polygon (WGS84 GeoJSON).

    Empty polygon list → all-False (no reference area configured).
    """
    if not polygons:
        with MemoryFile(product_bytes) as mem, mem.open() as src:
            return np.zeros((src.height, src.width), dtype=bool)
    with MemoryFile(product_bytes) as mem, mem.open() as src:
        return geometry_mask(
            polygons,
            out_shape=(src.height, src.width),
            transform=src.transform,
            invert=False,  # True OUTSIDE the geometries
        )


def build_crop_product(
    phase: np.ndarray,
    coherence: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    bbox: Bbox,
    *,
    resolution: float,
    reference: np.ndarray | None = None,
    threshold: float = DEFAULT_COHERENCE_THRESHOLD,
) -> bytes:
    """Radar-window phase + coherence → referenced LOS-cm GeoTIFF bytes.

    `lat`/`lon` are the per-pixel WGS84 coordinates of the SAME radar window
    as `phase`/`coherence`. `reference` is an optional boolean mask on the
    PRODUCT grid (see `reference_mask_outside`) whose coherent-pixel median
    is subtracted; None leaves the product unreferenced.
    """
    phase_g = geocode_radar_band(phase, lat, lon, bbox, resolution=resolution)
    coherence_g = geocode_radar_band(
        coherence, lat, lon, bbox, resolution=resolution
    )
    product = build_los_product(phase_g, coherence_g, threshold=threshold)
    if reference is None:
        return product
    return reference_los(product, reference)
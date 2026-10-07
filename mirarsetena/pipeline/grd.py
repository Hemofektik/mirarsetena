"""Sentinel-1 GRD backscatter: linear power to dB, per-date sigma0 layer.

The level-1 window (fetched through the shared window cache) holds linear
backscatter; the served layer stores dB float32 with NODATA masking.
"""
from __future__ import annotations

import numpy as np

from mirarsetena.pipeline import PipelineError
from mirarsetena.pipeline.catalog import DailyScene
from mirarsetena.pipeline.indices import NODATA, _encode, _read_band, layer_key
from mirarsetena.pipeline.window import (
    Bbox,
    cached_window,
    window_key,
)
from mirarsetena.storage import Storage

S1_COLLECTION = "sentinel-1-grd"
POLARIZATION = "vv"


def to_db(linear) -> np.ndarray:
    """Convert linear backscatter to decibels; non-positive -> NODATA."""
    linear = np.asarray(linear, dtype=np.float32)
    out = np.full(linear.shape, NODATA, dtype=np.float32)
    valid = linear > 0
    out[valid] = 10.0 * np.log10(linear[valid])
    return out


def process_s1_daily(
    storage: Storage,
    slug: str,
    daily: DailyScene,
    bbox: Bbox,
    *,
    opener=None,
) -> dict[str, str]:
    """Produce the sigma0 (dB) layer for one Sentinel-1 acquisition date."""
    uris = [
        scene.assets[POLARIZATION]
        for scene in daily.scenes
        if scene.assets.get(POLARIZATION)
    ]
    if not uris:
        raise PipelineError(f"no {POLARIZATION.upper()} asset for {daily.date}")

    window = cached_window(
        storage,
        window_key(slug, S1_COLLECTION, daily.date, band=POLARIZATION),
        uris,
        bbox,
        opener=opener,
    )
    data, profile = _read_band(window)
    key = layer_key(slug, S1_COLLECTION, daily.date, "sigma0")
    storage.put(key, _encode(to_db(data), profile, NODATA))
    return {"sigma0": key}

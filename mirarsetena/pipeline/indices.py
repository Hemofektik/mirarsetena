"""Spectral index computation and per-scene layer production.

Band math is scale-invariant (ratios), so Sentinel-2 BOA reflectance
scaling needs no adjustment. Mixed-resolution bands (10 m visible/NIR vs
20 m SWIR/SCL) are resampled nearest-neighbour onto the 10 m grid.
"""
from __future__ import annotations

import json

import numpy as np
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.warp import reproject as warp_reproject

from mirarsetena.pipeline import PipelineError
from mirarsetena.pipeline.catalog import DailyScene
from mirarsetena.pipeline.window import (
    Bbox,
    cached_window,
    scene_meta_key,
    window_key,
)
from mirarsetena.projects.registry import cache_key
from mirarsetena.storage import Storage

NODATA = -9999.0
S2_COLLECTION = "sentinel-2-l2a"


def _f32(values) -> np.ndarray:
    return np.asarray(values, dtype=np.float32)


def _ratio(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    out = np.full(numerator.shape, NODATA, dtype=np.float32)
    np.divide(numerator, denominator, out=out, where=denominator != 0)
    return out


def ndvi(nir, red) -> np.ndarray:
    nir, red = _f32(nir), _f32(red)
    return _ratio(nir - red, nir + red)


def mndwi(green, swir) -> np.ndarray:
    green, swir = _f32(green), _f32(swir)
    return _ratio(green - swir, green + swir)


def bsi(swir, red, nir, blue) -> np.ndarray:
    swir, red = _f32(swir), _f32(red)
    nir, blue = _f32(nir), _f32(blue)
    return _ratio((swir + red) - (nir + blue), (swir + red) + (nir + blue))


def stretch_band(values, low_pct: float = 2, high_pct: float = 98) -> np.ndarray:
    """Percentile stretch to uint8; constant inputs map to zeros."""
    values = np.asarray(values)
    p_low, p_high = np.percentile(values.astype(np.float64), (low_pct, high_pct))
    if p_high <= p_low:
        return np.zeros(values.shape, dtype=np.uint8)
    scaled = (values.astype(np.float64) - p_low) * 255.0 / (p_high - p_low)
    return np.clip(scaled, 0, 255).astype(np.uint8)


def stretch_rgb(red, green, blue) -> np.ndarray:
    return np.stack([stretch_band(red), stretch_band(green), stretch_band(blue)])


def cloud_pct(scl, classes: tuple[int, ...] = (8, 9, 10)) -> float:
    """Percentage of cloudy pixels in an SCL classification array."""
    scl = np.asarray(scl)
    if scl.size == 0:
        return 0.0
    return float(np.isin(scl, classes).sum()) / scl.size * 100.0


def layer_key(slug: str, collection: str, date: str, layer: str) -> str:
    return cache_key(slug, "layers", f"{collection}_{date}_{layer}.tif")


def _read_band(window_bytes: bytes) -> tuple[np.ndarray, dict]:
    with MemoryFile(window_bytes) as memfile, memfile.open() as src:
        return src.read(1), {
            "crs": src.crs,
            "transform": src.transform,
            "height": src.height,
            "width": src.width,
        }


def _align_to_grid(data: np.ndarray, profile: dict, like: dict) -> np.ndarray:
    if data.shape == (like["height"], like["width"]):
        return data
    destination = np.empty((like["height"], like["width"]), dtype=data.dtype)
    warp_reproject(
        source=data,
        destination=destination,
        src_transform=profile["transform"],
        src_crs=profile["crs"],
        dst_transform=like["transform"],
        dst_crs=like["crs"],
        resampling=Resampling.nearest,
    )
    return destination


def _encode(data: np.ndarray, like: dict, nodata) -> bytes:
    profile = {
        "driver": "GTiff",
        "dtype": data.dtype.name,
        "count": data.shape[0] if data.ndim == 3 else 1,
        "height": data.shape[-2],
        "width": data.shape[-1],
        "crs": like["crs"],
        "transform": like["transform"],
        "nodata": nodata,
        "compress": "deflate",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            if data.ndim == 3:
                dst.write(data)
            else:
                dst.write(data, 1)
        return mem.read()


def process_s2_daily(
    storage: Storage,
    slug: str,
    daily: DailyScene,
    bbox: Bbox,
    *,
    opener=None,
) -> dict[str, str]:
    """Produce the four S2 layers + cloud metadata for one acquisition date.

    Windows (level 1) are fetched/cached per band; layer products (level 2)
    are written under p/{slug}/layers/. Returns {layer: storage key}.
    """

    def band(name: str) -> bytes:
        uris = [
            scene.assets[name]
            for scene in daily.scenes
            if scene.assets.get(name)
        ]
        if not uris:
            raise PipelineError(f"no asset {name!r} for {daily.date}")
        return cached_window(
            storage,
            window_key(slug, S2_COLLECTION, daily.date, band=name),
            uris,
            bbox,
            opener=opener,
        )

    blue, _ = _read_band(band("blue"))
    green, _ = _read_band(band("green"))
    red, red_prof = _read_band(band("red"))
    nir, _ = _read_band(band("nir"))
    swir, swir_prof = _read_band(band("swir16"))
    scl, _ = _read_band(band("scl"))

    swir = _align_to_grid(swir, swir_prof, red_prof)

    outputs: dict[str, bytes] = {
        "rgb": _encode(stretch_rgb(red, green, blue), red_prof, nodata=None),
        "ndvi": _encode(ndvi(nir, red), red_prof, NODATA),
        "mndwi": _encode(mndwi(green, swir), red_prof, NODATA),
        "bsi": _encode(bsi(swir, red, nir, blue), red_prof, NODATA),
    }

    keys: dict[str, str] = {}
    for layer, payload in outputs.items():
        key = layer_key(slug, S2_COLLECTION, daily.date, layer)
        storage.put(key, payload)
        keys[layer] = key

    meta_key = scene_meta_key(slug, S2_COLLECTION, daily.date)
    storage.put(
        meta_key,
        json.dumps(
            {
                "collection": S2_COLLECTION,
                "date": daily.date,
                "cloud": round(cloud_pct(scl), 2),
                "scenes": [scene.id for scene in daily.scenes],
            }
        ).encode("utf-8"),
    )
    keys["cloud_meta"] = meta_key
    return keys

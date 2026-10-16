"""InSAR: unwrap phase → line-of-sight (LOS) displacement in centimetres.

The Sentinel-1 C-band carrier measures range along the satellite's
line-of-sight. After a pair's interferometric phase φ is unwrapped (SNAP +
snaphu, the injectable processor), the LOS displacement is

    Δr = −λ·φ / (4π)          (λ = 0.055465763 m, 5.405 GHz)

with the sign convention that a POSITIVE phase (range increasing) reads as
POSITIVE displacement *away from* the satellite — for near-vertical motion
that means the ground is sinking (see the legend's sign note). A full fringe
(φ = 2π) is λ/2 = 2.77 cm.

Only coherent pixels are trustworthy: the coherence band from the Goldstein
filter gates the product (coherence < threshold → NODATA), so the excavation
pit and vegetated slopes read as honest gaps rather than fabricated motion.

SNAP itself is NOT run here — Phase H's injected `Processor` produces the
unwrapped-phase and coherence GeoTIFFs; this module turns them into the
colour-renderable LOS-centimetre product the tile service serves.
"""
from __future__ import annotations

import json

import numpy as np
from rasterio.io import MemoryFile

from mirarsetena.pipeline.indices import NODATA
from mirarsetena.projects.registry import cache_key

C_BAND_WAVELENGTH_M = 0.055465763  # Sentinel-1, 5.405 GHz

DEFAULT_COHERENCE_THRESHOLD = 0.35


def phase_to_los_cm(phase_rad) -> np.ndarray:
    """Unwrapped interferometric phase (radians) → LOS displacement (cm).

    Positive phase = range increase = away from the satellite. Scalar or
    array in, float32 out.
    """
    values = np.asarray(phase_rad, dtype=np.float32)
    return (C_BAND_WAVELENGTH_M * values / (4.0 * np.pi) * 100.0).astype(
        np.float32
    )


def mask_by_coherence(
    los_cm: np.ndarray, coherence: np.ndarray, *, threshold: float
) -> np.ndarray:
    """Null (NODATA) every pixel whose coherence is below the threshold.

    Threshold is inclusive at the boundary (coherence == threshold is kept).
    Pixels already NODATA stay NODATA.
    """
    out = np.asarray(los_cm, dtype=np.float32).copy()
    coh = np.asarray(coherence, dtype=np.float32)
    out[(coh < threshold) | (out == NODATA)] = NODATA
    return out


def accumulate_los(products: list[np.ndarray]) -> list[np.ndarray]:
    """Running LOS sum over a temporal series (products[0] is the reference).

    Returns n-1 accumulated maps: series[i] = sum(products[0..i+1]). NODATA
    is skipped per pixel (a single missing date does not zero the pixel), and
    a pixel that is NODATA in every summed product stays NODATA.
    """
    if not products:
        return []
    running = np.asarray(products[0], dtype=np.float32).copy()
    series: list[np.ndarray] = []
    for nxt in products[1:]:
        nxt_arr = np.asarray(nxt, dtype=np.float32)
        valid = (running != NODATA) | (nxt_arr != NODATA)
        summed = np.where(
            valid,
            np.where(running == NODATA, 0.0, running)
            + np.where(nxt_arr == NODATA, 0.0, nxt_arr),
            NODATA,
        ).astype(np.float32)
        series.append(summed.copy())
        running = summed
    return series


def _read_band1(band_bytes: bytes) -> tuple[np.ndarray, dict]:
    with MemoryFile(band_bytes) as memfile, memfile.open() as src:
        return src.read(1), {
            "crs": src.crs,
            "transform": src.transform,
            "height": src.height,
            "width": src.width,
        }


def los_range(product_bytes: bytes) -> float | None:
    """Symmetric ±cm limit for the diverging ramp, from the product's data.

    The fixed ±5 cm MVP ramp rendered the real product all-white (the
    referenced product spans only ±0.56 cm, so every pixel landed on the
    ramp's white centre) — INSAR-PLAN decision 5's gate: "adaptive percentile
    range only after real data says ±5 is wrong". The limit is the 99th
    percentile of |valid values|, floored at 0.5 cm so a near-zero product
    still shows structure, and None when no pixel is valid (nothing to map).
    """
    with MemoryFile(product_bytes) as memfile, memfile.open() as src:
        data = src.read(1)
    valid = data[data != NODATA]
    if valid.size == 0:
        return None
    limit = float(np.percentile(np.abs(valid), 99))
    return max(limit, 0.5)


def sample_pois(product_bytes: bytes, pois: list[dict]) -> list[dict]:
    """Sample LOS cm at POI coordinates from a product (L.9 plan-vs-actual).

    Each poi is `{"id": ..., "coordinates": [lon, lat]}` (GeoJSON-style
    WGS84). The value is rounded to 0.001 cm; a coherence-masked pixel or a
    coordinate outside the footprint yields `los_cm: null` — an honest gap
    the chart must not interpolate across, never a fabricated 0.
    """
    out: list[dict] = []
    with MemoryFile(product_bytes) as memfile, memfile.open() as src:
        data = src.read(1)
        for poi in pois:
            lon, lat = poi["coordinates"][:2]
            row, col = src.index(lon, lat)
            if not (0 <= row < src.height and 0 <= col < src.width):
                out.append({"id": poi["id"], "los_cm": None})
                continue
            value = float(data[row, col])
            out.append(
                {
                    "id": poi["id"],
                    "los_cm": None if value == NODATA else round(value, 3),
                }
            )
    return out


def insar_series_key(slug: str, date: str) -> str:
    """Storage key of the per-date series entry, beside the product .tif."""
    return cache_key(slug, "layers", f"sentinel-1-slc_{date}_insar_series.json")


def edge_key(slug: str, first: str, second: str) -> str:
    """Storage key of an SBAS edge product (24-day closer).

    Closers share END DATES with the served 12-day products, so their
    geocoded edges live in the sbas/ namespace — never under the layer
    key the tiles and series read.
    """
    return cache_key(slug, "sbas", "edges", f"{first}_{second}.tif")


def write_insar_series(
    store, slug: str, date: str, product_bytes: bytes, pois: list[dict],
    *,
    chain: str | None = None,
) -> str:
    """Persist per-POI LOS values for one date (L.9 plan-vs-actual series).

    Written at product-build time so the API serves a pre-sampled series
    instead of re-opening rasters per request. `chain` identifies the
    satellite+track network the pair belongs to (e.g. "S1A-asc"): SBAS
    aggregation groups by chain because cross-sensor pairs cannot be
    phase-tied. Returns the storage key.
    """
    payload = {
        "date": date,
        "los_range": los_range(product_bytes),
        "pois": sample_pois(product_bytes, pois),
    }
    if chain is not None:
        payload["chain"] = chain
    key = insar_series_key(slug, date)
    store.put(key, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return key


def reference_los(product_bytes: bytes, reference: np.ndarray) -> bytes:
    """Re-zero an LOS product against a stable reference area.

    An unwrap's absolute offset is arbitrary (the spike's raw product sat at
    −6.20 cm median — 100% saturated at the ±5 cm ramp), so the median of the
    COHERENT pixels inside `reference` is subtracted from every valid pixel.
    Nodata pixels stay nodata; a reference area with no coherent pixel leaves
    the product untouched (honestly unreferenced rather than shifted by a
    fabricated offset). `reference` is a boolean mask on the product grid.
    """
    with MemoryFile(product_bytes) as memfile, memfile.open() as src:
        data = src.read(1)
        profile = src.profile.copy()

    valid = data != NODATA
    in_ref = np.asarray(reference, dtype=bool) & valid
    if not in_ref.any():
        return product_bytes
    offset = float(np.median(data[in_ref]))
    out = np.where(valid, data - offset, NODATA).astype(np.float32)

    profile.update(dtype="float32", count=1, nodata=NODATA, compress="deflate")
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(out, 1)
        return mem.read()


def build_los_product(
    phase_bytes: bytes, coherence_bytes: bytes, *, threshold: float
) -> bytes:
    """Unwrapped-phase + coherence GeoTIFF bytes → LOS-cm product GeoTIFF.

    The product is float32, EPSG:4326 (the project product grid), NODATA
    = -9999, coherence-masked — ready for the tile renderer's ramp.
    """
    phase, like = _read_band1(phase_bytes)
    coherence, _ = _read_band1(coherence_bytes)
    los = mask_by_coherence(
        phase_to_los_cm(phase), coherence, threshold=threshold
    )
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": like["height"],
        "width": like["width"],
        "crs": like["crs"],
        "transform": like["transform"],
        "nodata": NODATA,
        "compress": "deflate",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(los, 1)
        return mem.read()

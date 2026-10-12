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

import numpy as np
from rasterio.io import MemoryFile

from mirarsetena.pipeline.indices import NODATA

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

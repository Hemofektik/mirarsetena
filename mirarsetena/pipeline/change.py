"""Change-vs-baseline rendering for radar layers (sigma0, coherence).

Change mode classifies per-pixel deltas into seven signed classes with a
255 unclassified/nodata code; raw mode returns percentile-stretched
grayscale. Baseline default: the latest pre-works acquisition (SCOPE R3-Q3).

Raw GRD pairs differ by speckle (~1.5 dB per date) plus season/orbit
offsets, so plain differencing scattered pixels over every class — the map
looked like noise whatever period was picked. The change branch therefore
preprocesses the pair before classifying:

1. speckle reduction — a NODATA-aware 7x7 focal mean in the *linear power*
   domain for sigma0 (intensity multilooking) and linearly for coherence;
2. bias removal — the median delta over valid pixels is subtracted, so a
   uniform season/orbit shift renders neutral instead of "everything
   changed";
3. noise-adaptive class edges — ``adaptive_thresholds`` widens the neutral/
   weak bands to the pair's residual noise (never tighter than the base
   0.5/1.5/3.0 dB scheme, 0.05/0.15/0.3 for coherence).
"""
from __future__ import annotations

from datetime import date

import numpy as np

from mirarsetena.pipeline import PipelineError
from mirarsetena.pipeline.indices import NODATA, _align_to_grid, _encode, _read_band

UNCLASSIFIED = 255
CHANGE_MODE = "change"
RAW_MODE = "raw"

SPECKLE_WINDOW = 7
# Bump when the change preprocessing/classification changes: derived
# products and rendered change tiles cached under older versions must not
# be served again (raw mode is unaffected and keeps its cache keys).
PREPROCESS_VERSION = 1


def classify_delta(delta_db: float) -> int:
    """Seven signed classes: 0 strong decrease .. 3 neutral .. 6 strong increase."""
    if delta_db <= -3.0:
        return 0
    if delta_db <= -1.5:
        return 1
    if delta_db <= -0.5:
        return 2
    if delta_db < 0.5:
        return 3
    if delta_db < 1.5:
        return 4
    if delta_db < 3.0:
        return 5
    return 6


def default_baseline_date(dates: list[str], works_start: date) -> str:
    """Latest acquisition before works started; earliest overall as fallback."""
    if not dates:
        raise PipelineError("no dates available for baseline selection")
    ordered = sorted(dates)
    pre_works = [
        day for day in ordered if date.fromisoformat(day) < works_start
    ]
    return pre_works[-1] if pre_works else ordered[0]


# Class edges for a *clean* pair (dB for sigma0); the render widens them to
# the measured noise but never tightens below these (SCOPE R3-Q3 scheme).
BASE_THRESHOLDS_DB = (0.5, 1.5, 3.0)
BASE_THRESHOLDS_COHERENCE = (0.05, 0.15, 0.3)
_MIN_SAMPLES_FOR_NOISE = 50


def adaptive_thresholds(
    deltas: np.ndarray,
    base: tuple[float, float, float] = BASE_THRESHOLDS_DB,
) -> tuple[float, float, float]:
    """Class edges scaled to the pair's residual noise (robust sigma).

    sigma = 1.4826 * MAD of the (preprocessed) deltas. Edges become
    max(base, k*sigma) with k = 1, 2, 3 — identical to the base scheme for
    clean pairs, wider for noisy ones, so within-noise pixels classify
    neutral instead of scattering over the signed classes.
    """
    values = np.asarray(deltas, dtype=np.float64).ravel()
    values = values[np.isfinite(values)]
    if values.size < _MIN_SAMPLES_FOR_NOISE:
        return base
    median = np.median(values)
    mad = np.median(np.abs(values - median))
    sigma = float(1.4826 * mad)
    b1, b2, b3 = base
    return (max(b1, sigma), max(b2, 2.0 * sigma), max(b3, 3.0 * sigma))


def _classify_adaptive(
    delta: np.ndarray, thresholds: tuple[float, float, float]
) -> np.ndarray:
    """Same seven signed classes, edges from ``adaptive_thresholds``."""
    b1, b2, b3 = thresholds
    return np.select(
        [
            delta <= -b3,
            delta <= -b2,
            delta <= -b1,
            delta < b1,
            delta < b2,
            delta < b3,
        ],
        [0, 1, 2, 3, 4, 5],
        default=6,
    ).astype(np.uint8)


def _focal_mean(
    source: np.ndarray, valid: np.ndarray, size: int
) -> tuple[np.ndarray, np.ndarray]:
    """NODATA-aware ``size`` x ``size`` mean over a clipped window.

    Returns (mean, count) in float64; mean is NaN where the window holds
    no valid sample. Uses integral images, so cost is O(pixels) regardless
    of window size.
    """
    half = size // 2
    values = np.where(valid, source, 0.0).astype(np.float64)
    counts = valid.astype(np.float64)

    def integral(arr: np.ndarray) -> np.ndarray:
        out = np.zeros((arr.shape[0] + 1, arr.shape[1] + 1), dtype=np.float64)
        out[1:, 1:] = arr.cumsum(axis=0).cumsum(axis=1)
        return out

    iv, ic = integral(values), integral(counts)
    height, width = source.shape
    rows = np.arange(height)
    cols = np.arange(width)
    y0 = np.clip(rows - half, 0, height)
    y1 = np.clip(rows + half + 1, 0, height)
    x0 = np.clip(cols - half, 0, width)
    x1 = np.clip(cols + half + 1, 0, width)

    def window(ii: np.ndarray) -> np.ndarray:
        return (
            ii[np.ix_(y1, x1)]
            - ii[np.ix_(y0, x1)]
            - ii[np.ix_(y1, x0)]
            + ii[np.ix_(y0, x0)]
        )

    win_sum, win_count = window(iv), window(ic)
    mean = np.full(source.shape, np.nan, dtype=np.float64)
    np.divide(win_sum, win_count, out=mean, where=win_count > 0)
    return mean, win_count


def _smooth_layer(
    data: np.ndarray, valid: np.ndarray, layer: str, size: int = SPECKLE_WINDOW
) -> np.ndarray:
    """Speckle-reduce one radar layer over the pair's joint valid support.

    sigma0 averages *linear power* (intensity multilooking — the correct
    domain for multiplicative speckle) then converts back to dB;
    coherence is an already-linear estimate and is averaged directly.
    The original valid mask is preserved: smoothing must not resurrect
    NODATA (outside footprint) pixels.
    """
    if layer == "sigma0":
        source = np.where(valid, np.power(10.0, np.asarray(data, np.float64) / 10.0), 0.0)
    else:
        source = np.where(valid, np.asarray(data, np.float64), 0.0)
    mean, count = _focal_mean(source, valid, size)
    out = np.full(data.shape, NODATA, dtype=np.float64)
    if layer == "sigma0":
        with np.errstate(divide="ignore"):
            out[count > 0] = 10.0 * np.log10(mean[count > 0])
    else:
        out[count > 0] = mean[count > 0]
    out[~valid] = NODATA
    return out


def render_change(
    scene_bytes: bytes,
    baseline_bytes: bytes | None = None,
    *,
    mode: str = CHANGE_MODE,
    layer: str = "sigma0",
) -> bytes:
    """Render one date's layer, either as change-vs-baseline or raw."""
    scene, scene_profile = _read_band(scene_bytes)

    if mode == RAW_MODE:
        valid = scene != NODATA
        if not valid.any():
            return _encode(
                np.zeros(scene.shape, dtype=np.uint8), scene_profile, None
            )
        p_low, p_high = np.percentile(scene[valid], (2, 98))
        if p_high <= p_low:
            out = np.zeros(scene.shape, dtype=np.uint8)
        else:
            scaled = (scene.astype(np.float64) - p_low) * 255.0 / (p_high - p_low)
            out = np.clip(scaled, 0, 255).astype(np.uint8)
        out[~valid] = 0
        return _encode(out, scene_profile, None)

    if mode != CHANGE_MODE:
        raise PipelineError(f"unknown render mode {mode!r}")
    if baseline_bytes is None:
        raise PipelineError("change mode requires a baseline product")
    if layer not in ("sigma0", "coherence"):
        raise PipelineError(f"unknown radar layer {layer!r}")

    baseline, baseline_profile = _read_band(baseline_bytes)
    # Scene and baseline can sit on different grids: dual-scene dates merge
    # onto a north-up grid while single-scene dates keep per-date GCP
    # geometry, and footprints may cover only part of the AOI. Alignment is
    # identity when the grids already match; otherwise the baseline is
    # reprojected onto the scene grid and outside its footprint it stays
    # NODATA, rendering unclassified.
    baseline = _align_to_grid(baseline, baseline_profile, scene_profile)
    if scene.shape != baseline.shape:
        raise PipelineError(
            f"scene grid {scene.shape} does not match baseline {baseline.shape}"
        )

    valid = (scene != NODATA) & (baseline != NODATA)
    out = np.full(scene.shape, UNCLASSIFIED, dtype=np.uint8)
    if not valid.any():
        return _encode(out, scene_profile, UNCLASSIFIED)

    # 1) speckle reduction, 2) median bias, 3) noise-adaptive classes.
    smooth_scene = _smooth_layer(scene, valid, layer)
    smooth_baseline = _smooth_layer(baseline, valid, layer)
    deltas = (smooth_scene - smooth_baseline)[valid]
    bias = float(np.median(deltas))
    corrected = deltas - bias
    base = BASE_THRESHOLDS_DB if layer == "sigma0" else BASE_THRESHOLDS_COHERENCE
    thresholds = adaptive_thresholds(corrected, base)
    out[valid] = _classify_adaptive(corrected, thresholds)
    return _encode(out, scene_profile, UNCLASSIFIED)

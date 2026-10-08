"""Change-vs-baseline rendering for radar layers (sigma0, coherence).

Change mode classifies per-pixel dB deltas into seven signed classes with a
255 unclassified/nodata code; raw mode returns percentile-stretched
grayscale. Baseline default: the latest pre-works acquisition (SCOPE R3-Q3).
"""
from __future__ import annotations

from datetime import date

import numpy as np

from mirarsetena.pipeline import PipelineError
from mirarsetena.pipeline.indices import NODATA, _align_to_grid, _encode, _read_band

UNCLASSIFIED = 255
CHANGE_MODE = "change"
RAW_MODE = "raw"


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


def _classify_vector(delta: np.ndarray) -> np.ndarray:
    return np.select(
        [
            delta <= -3.0,
            delta <= -1.5,
            delta <= -0.5,
            delta < 0.5,
            delta < 1.5,
            delta < 3.0,
        ],
        [0, 1, 2, 3, 4, 5],
        default=6,
    ).astype(np.uint8)


def render_change(
    scene_bytes: bytes,
    baseline_bytes: bytes | None = None,
    *,
    mode: str = CHANGE_MODE,
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

    out = np.full(scene.shape, UNCLASSIFIED, dtype=np.uint8)
    valid = (scene != NODATA) & (baseline != NODATA)
    delta = scene.astype(np.float32) - baseline.astype(np.float32)
    out[valid] = _classify_vector(delta[valid])
    return _encode(out, scene_profile, UNCLASSIFIED)

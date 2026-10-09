"""E.2 — change-vs-baseline rendering for radar layers.

Seam: mirarsetena.pipeline.change (classify / render / baseline selection).
"""
from datetime import date

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from mirarsetena.pipeline.change import (
    classify_delta,
    default_baseline_date,
    render_change,
)
from mirarsetena.pipeline.indices import NODATA

GRID = from_origin(-83.672, 9.392, 0.001, 0.001)


def test_classify_delta_goldens():
    assert classify_delta(-5.0) == 0   # strong decrease
    assert classify_delta(-2.0) == 1
    assert classify_delta(-1.0) == 2
    assert classify_delta(-3.0) == 0   # boundary is inclusive
    assert classify_delta(0.0) == 3    # neutral
    assert classify_delta(1.0) == 4
    assert classify_delta(2.0) == 5
    assert classify_delta(4.0) == 6    # strong increase


def test_default_baseline_is_latest_pre_works_date():
    dates = ["2026-07-11", "2026-07-23", "2026-08-04", "2026-08-16"]
    assert default_baseline_date(dates, date(2026, 8, 1)) == "2026-07-23"


def test_default_baseline_falls_back_to_earliest_when_all_post_works():
    dates = ["2026-08-04", "2026-08-16"]
    assert default_baseline_date(dates, date(2026, 8, 1)) == "2026-08-04"


def _geo_bytes(values: np.ndarray, grid=GRID) -> bytes:
    data = values.astype(np.float32)
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": data.shape[0], "width": data.shape[1],
        "crs": "EPSG:4326", "transform": grid, "nodata": NODATA,
        "compress": "deflate",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(data, 1)
        return mem.read()


def test_render_change_classifies_delta_and_masks_nodata():
    # 32x32 so the 7x7 speckle window is meaningful: uniform background,
    # interior patches away from the window edges (their centers keep the
    # exact injected delta), one NODATA pixel.
    baseline_grid = np.full((32, 32), -10.0, dtype=np.float32)
    scene_grid = baseline_grid.copy()
    scene_grid[0, 0] = NODATA          # masked out entirely
    scene_grid[12:20, 12:20] = -6.0    # +4 dB -> strong increase
    scene_grid[12:20, 24:32] = -13.5   # -3.5 dB -> strong decrease
    baseline = _geo_bytes(baseline_grid)
    scene = _geo_bytes(scene_grid)
    out = render_change(scene, baseline)
    with MemoryFile(out) as memfile, memfile.open() as src:
        classes = src.read(1)
        assert src.dtypes[0] == "uint8"
        assert classes[0, 0] == 255     # nodata -> unclassified
        assert classes[5, 5] == 3       # untouched background -> neutral
        assert classes[16, 16] == 6     # patch centre: +4 dB -> class 6
        assert classes[16, 27] == 0     # patch centre: -3.5 dB -> class 0


def test_render_raw_mode_returns_stretched_grayscale():
    baseline = _geo_bytes(np.array([[-10.0, -10.0], [-10.0, -10.0]]))
    scene = _geo_bytes(np.array([[-13.0, -13.0], [-13.0, -13.0]]))
    out = render_change(scene, baseline, mode="raw")
    with MemoryFile(out) as memfile, memfile.open() as src:
        raw = src.read(1)
        assert src.dtypes[0] == "uint8"
        # constant input -> percentile stretch collapses to zero
        assert set(np.unique(raw).tolist()) == {0}


def test_render_change_aligns_baseline_from_another_grid():
    """Scene and baseline can land on different grids: dual-scene dates go
    through a north-up merge while single-scene dates keep the per-date GCP
    geometry, and footprints may cover only part of the AOI. The baseline
    must be reprojected onto the scene's grid; pixels outside its footprint
    render unclassified (regression: PipelineError -> 500 on radar change
    tiles)."""
    top_left = from_origin(-83.672, 9.392, 0.001, 0.001)  # one pixel only
    baseline = _geo_bytes(np.array([[-10.0]]), grid=top_left)
    scene = _geo_bytes(np.array([[-10.0, -13.0], [-10.0, -8.5]]))

    out = render_change(scene, baseline)
    with MemoryFile(out) as memfile, memfile.open() as src:
        classes = src.read(1)
        assert classes.shape == (2, 2)
        assert classes[0, 0] == 3   # covered by baseline: delta 0 -> neutral
        assert classes[0, 1] == 255  # outside baseline footprint
        assert classes[1, 0] == 255
        assert classes[1, 1] == 255


def test_adaptive_thresholds_never_tighter_than_the_base_scheme():
    """Clean pairs keep the exact 0.5/1.5/3.0 dB class edges — the
    user-approved color scheme must never get tighter than documented."""
    from mirarsetena.pipeline.change import adaptive_thresholds

    clean = np.array([-0.1, 0.0, 0.1], dtype=np.float32)
    assert adaptive_thresholds(clean) == (0.5, 1.5, 3.0)


def test_adaptive_thresholds_widen_to_the_measured_noise():
    """When the pair's residual noise is large, neutral/weak bands widen so
    within-noise pixels classify as neutral instead of random classes."""
    from mirarsetena.pipeline.change import adaptive_thresholds

    # median 0, median absolute deviation 1 -> sigma = 1.4826;
    # 50 samples (the minimum for noise estimation), same distribution
    noisy = np.tile(np.array([-2.0, -1.0, 0.0, 1.0, 2.0], dtype=np.float32), 10)
    b1, b2, b3 = adaptive_thresholds(noisy)
    assert b1 == pytest.approx(1.4826)
    assert b2 == pytest.approx(2 * 1.4826)
    assert b3 == pytest.approx(3 * 1.4826)


def test_adaptive_thresholds_degenerate_input_falls_back_to_base():
    from mirarsetena.pipeline.change import adaptive_thresholds

    assert adaptive_thresholds(np.array([], dtype=np.float32)) == (0.5, 1.5, 3.0)
    assert adaptive_thresholds(np.array([5.0], dtype=np.float32)) == (0.5, 1.5, 3.0)


def test_uniform_pair_offset_is_removed_as_bias():
    """A season-wide / orbit-wide dB shift is not per-pixel change: scene
    = baseline + 4 dB everywhere must render neutral, not strong increase
    (regression: everything painted as change regardless of period)."""
    grid = np.full((32, 32), -10.0, dtype=np.float32)
    baseline = _geo_bytes(grid)
    scene = _geo_bytes(grid + 4.0)
    out = render_change(scene, baseline)
    with MemoryFile(out) as memfile, memfile.open() as src:
        classes = src.read(1)
    neutral = int((classes == 3).sum())
    assert neutral == classes.size  # every pixel: neutral


def test_speckle_only_pairs_classify_mostly_neutral():
    """Two GRD acquisitions of an unchanged scene differ by speckle alone
    (sigma ~1.5 dB per date). After power-domain multilooking the delta
    collapses into the neutral class instead of scattering over classes."""
    rng = np.random.default_rng(7)
    baseline_grid = np.full((128, 128), -10.0, dtype=np.float32)
    scene_grid = baseline_grid + rng.normal(0.0, 1.5, (128, 128)).astype(np.float32)
    baseline = _geo_bytes(baseline_grid)
    scene = _geo_bytes(scene_grid)
    out = render_change(scene, baseline)
    with MemoryFile(out) as memfile, memfile.open() as src:
        classes = src.read(1)
    neutral = int((classes == 3).sum())
    assert neutral / classes.size >= 0.75


def test_spatially_correlated_noise_still_classifies_mostly_neutral():
    """Smooth noise fields survive the 7x7 multilook (unlike iid speckle).
    The adaptive edges must widen the neutral band to the measured sigma
    instead of scattering the field over the signed classes."""
    rng = np.random.default_rng(11)
    noise = rng.normal(0.0, 1.0, (64, 64)).astype(np.float32)
    # 5x5 box smoothing keeps sigma well above the 0.5 dB neutral edge
    kernel = np.ones((5, 5), dtype=np.float32) / 25.0
    smooth = np.apply_along_axis(
        lambda r: np.convolve(r, kernel[0], mode="same"), 1, noise
    )
    smooth = np.apply_along_axis(
        lambda c: np.convolve(c, kernel[0], mode="same"), 1, smooth.T
    ).T
    smooth *= 1.0 / max(float(smooth.std()), 1e-6)
    smooth *= 1.2  # residual sigma ~1.2 dB after the render's multilook

    baseline_grid = np.full((64, 64), -10.0, dtype=np.float32)
    baseline = _geo_bytes(baseline_grid)
    scene = _geo_bytes(baseline_grid + smooth)
    out = render_change(scene, baseline)
    with MemoryFile(out) as memfile, memfile.open() as src:
        classes = src.read(1)
    neutral = int((classes == 3).sum())
    # fixed 0.5 dB edge would leave ~34% neutral for sigma=1.2 noise
    assert neutral / classes.size >= 0.60


def test_cochange_uses_coherence_value_thresholds():
    """Coherence deltas live in [0, 1]; the dB edges (0.5/1.5/3) classified
    every coherence pair as neutral. Layer-aware base edges (0.05/0.15/0.3)
    make a -0.25 coherence drop visible."""
    baseline_grid = np.full((32, 32), 0.6, dtype=np.float32)
    scene_grid = baseline_grid.copy()
    scene_grid[12:20, 12:20] = 0.35  # -0.25 -> class 1 (<= -0.15)
    baseline = _geo_bytes(baseline_grid)
    scene = _geo_bytes(scene_grid)
    out = render_change(scene, baseline, layer="coherence")
    with MemoryFile(out) as memfile, memfile.open() as src:
        classes = src.read(1)
    assert classes[5, 5] == 3   # no change -> neutral
    assert classes[16, 16] == 1  # patch centre: -0.25 -> class 1

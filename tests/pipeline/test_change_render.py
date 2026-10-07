"""E.2 — change-vs-baseline rendering for radar layers.

Seam: mirarsetena.pipeline.change (classify / render / baseline selection).
"""
from datetime import date

import numpy as np
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


def _geo_bytes(values: np.ndarray) -> bytes:
    data = values.astype(np.float32)
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": data.shape[0], "width": data.shape[1],
        "crs": "EPSG:4326", "transform": GRID, "nodata": NODATA,
        "compress": "deflate",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(data, 1)
        return mem.read()


def test_render_change_classifies_delta_and_masks_nodata():
    baseline = _geo_bytes(np.array([[-10.0, -10.0], [-10.0, -10.0]]))
    scene = _geo_bytes(np.array([[-10.0, -13.0], [NODATA, -8.5]]))
    out = render_change(scene, baseline)
    with MemoryFile(out) as memfile, memfile.open() as src:
        classes = src.read(1)
        assert src.dtypes[0] == "uint8"
        assert classes[0, 0] == 3  # delta 0 -> neutral
        assert classes[0, 1] == 0  # delta -3 -> strong decrease
        assert classes[1, 0] == 255  # nodata -> unclassified
        assert classes[1, 1] == 5  # delta +1.5 lands on the class-5 boundary


def test_render_raw_mode_returns_stretched_grayscale():
    baseline = _geo_bytes(np.array([[-10.0, -10.0], [-10.0, -10.0]]))
    scene = _geo_bytes(np.array([[-13.0, -13.0], [-13.0, -13.0]]))
    out = render_change(scene, baseline, mode="raw")
    with MemoryFile(out) as memfile, memfile.open() as src:
        raw = src.read(1)
        assert src.dtypes[0] == "uint8"
        # constant input -> percentile stretch collapses to zero
        assert set(np.unique(raw).tolist()) == {0}

"""D.3 — index computation + SCL cloud scoring.

Seam: mirarsetena.pipeline.indices (pure index math + process_s2_daily).
Hand-computed expected values; synthetic same-scene band grids (including a
20 m band that must be resampled onto the 10 m grid).
"""
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mirarsetena.pipeline.catalog import DailyScene, Scene
from mirarsetena.pipeline.indices import (
    NODATA,
    bsi,
    cloud_pct,
    layer_key,
    mndwi,
    ndvi,
    process_s2_daily,
    stretch_band,
    stretch_rgb,
)
from mirarsetena.storage import LocalStore

BBOX = (-83.672, 9.380, -83.664, 9.392)


def test_ndvi_hand_computed():
    value = ndvi(np.array([0.3], np.float32), np.array([0.1], np.float32))
    assert value[0] == pytest.approx(0.5, rel=1e-6)


def test_mndwi_hand_computed():
    value = mndwi(np.array([0.08], np.float32), np.array([0.04], np.float32))
    assert value[0] == pytest.approx(1 / 3, rel=1e-6)


def test_bsi_hand_computed():
    # ((swir+red)-(nir+blue)) / ((swir+red)+(nir+blue))
    # ((0.2+0.15)-(0.3+0.1)) / ((0.2+0.15)+(0.3+0.1)) = -0.05/0.75
    value = bsi(
        np.array([0.2], np.float32),
        np.array([0.15], np.float32),
        np.array([0.3], np.float32),
        np.array([0.1], np.float32),
    )
    assert value[0] == pytest.approx(-0.05 / 0.75, rel=1e-6)


def test_zero_denominator_yields_nodata():
    zero = np.array([0.0], np.float32)
    assert ndvi(zero, zero)[0] == NODATA


def test_stretch_band_maps_percentiles_to_0_255():
    values = np.arange(0, 101, dtype=np.uint16)
    stretched = stretch_band(values)
    assert stretched[0] == 0        # below p2
    assert stretched[2] == 0        # p2
    assert stretched[50] == 127     # midpoint of [2, 98] -> 0.5*255 truncated
    assert stretched[98] == 255     # p98
    assert stretched[100] == 255    # above p98
    assert stretched.dtype == np.uint8


def test_stretch_rgb_returns_three_uint8_bands():
    stack = np.stack([np.full((4, 4), v, dtype=np.uint16) for v in (10, 20, 30)])
    rgb = stretch_rgb(stack[0], stack[1], stack[2])
    assert rgb.shape == (3, 4, 4)
    assert rgb.dtype == np.uint8


def test_cloud_pct_known_fraction():
    scl = np.array([[8, 9, 10, 1], [0, 2, 0, 0]], dtype=np.uint8)
    assert cloud_pct(scl) == pytest.approx(37.5)


def _write_band(path: Path, value, res=0.001, dtype="uint16"):
    """Grid exactly covering BBOX: 8 x 12 pixels at `res` degrees."""
    width = round(0.008 / res)
    height = round(0.012 / res)
    transform = from_origin(-83.672, 9.392, res, res)
    data = np.full((height, width), value, dtype=dtype)
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1,
        dtype=dtype, crs="EPSG:4326", transform=transform, nodata=0,
    ) as dst:
        dst.write(data, 1)
    return path


def test_process_s2_daily_writes_layers_and_cloud_meta(tmp_path):
    bands = {
        "blue": _write_band(tmp_path / "blue.tif", 100),
        "green": _write_band(tmp_path / "green.tif", 80),
        "red": _write_band(tmp_path / "red.tif", 100),
        "nir": _write_band(tmp_path / "nir.tif", 300),
        "swir16": _write_band(tmp_path / "swir16.tif", 200, res=0.002),
        "scl": _write_band(tmp_path / "scl.tif", 8, res=0.002, dtype="uint8"),
        "visual": _write_band(tmp_path / "visual.tif", 500),
    }
    scene = Scene(
        id="TEST_SCENE",
        collection="sentinel-2-l2a",
        datetime="2026-07-03T10:00:00Z",
        date="2026-07-03",
        cloud=12.0,
        assets={name: str(path) for name, path in bands.items()},
    )
    daily = DailyScene(date="2026-07-03", scenes=(scene,))
    storage = LocalStore(tmp_path / "cache")

    keys = process_s2_daily(storage, "proj-a", daily, BBOX)

    for layer in ("rgb", "ndvi", "mndwi", "bsi", "cloud_meta"):
        assert layer in keys
        assert storage.exists(keys[layer])
        if layer != "cloud_meta":
            assert keys[layer].startswith("p/proj-a/layers/")

    # NDVI on the 10 m grid: (300-100)/(300+100) = 0.5 everywhere
    from rasterio.io import MemoryFile

    with MemoryFile(storage.get(keys["ndvi"])) as memfile, memfile.open() as src:
        ndvi_arr = src.read(1)
        assert src.dtypes[0] == "float32"
        assert ndvi_arr[0, 0] == pytest.approx(0.5, rel=1e-6)
        # 20 m swir16 band was resampled onto the 10 m output grid
        assert ndvi_arr.shape == (12, 8)

    with MemoryFile(storage.get(keys["mndwi"])) as memfile, memfile.open() as src:
        # mndwi mixes the 10 m green with the 20 m swir16 -> resampled grid
        assert src.read(1).shape == (12, 8)

    meta = json.loads(storage.get(keys["cloud_meta"]).decode("utf-8"))
    assert meta["cloud"] == pytest.approx(100.0)  # every scl pixel is class 8
    assert meta["date"] == "2026-07-03"

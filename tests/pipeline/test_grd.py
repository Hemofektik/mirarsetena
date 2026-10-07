"""E.1 — Sentinel-1 GRD window + sigma-naught backscatter.

Seam: mirarsetena.pipeline.grd (to_db, process_s1_daily).
"""
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from mirarsetena.pipeline.catalog import DailyScene, Scene
from mirarsetena.pipeline.grd import process_s1_daily, to_db
from mirarsetena.pipeline.indices import NODATA
from mirarsetena.storage import LocalStore

BBOX = (-83.672, 9.380, -83.664, 9.392)


def test_to_db_goldens():
    assert to_db(np.array([1.0], np.float32))[0] == pytest.approx(0.0, abs=1e-6)
    assert to_db(np.array([0.1], np.float32))[0] == pytest.approx(-10.0, abs=1e-4)
    assert to_db(np.array([0.01], np.float32))[0] == pytest.approx(-20.0, abs=1e-4)
    result = to_db(np.array([0.0, -1.0], np.float32))
    assert result[0] == NODATA
    assert result[1] == NODATA


def _write_linear_vv(path: Path, value=0.1):
    transform = from_origin(-83.672, 9.392, 0.001, 0.001)
    data = np.full((12, 8), value, dtype=np.float32)
    with rasterio.open(
        path, "w", driver="GTiff", height=12, width=8, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform, nodata=0,
    ) as dst:
        dst.write(data, 1)
    return path


def _daily(tmp_path) -> DailyScene:
    vv = _write_linear_vv(tmp_path / "vv.tif")
    scene = Scene(
        id="S1_TEST",
        collection="sentinel-1-grd",
        datetime="2026-07-11T23:47:58Z",
        date="2026-07-11",
        cloud=None,
        assets={"vv": str(vv), "vh": str(vv)},
        platform="sentinel-1d",
        orbit_state="descending",
        relative_orbit=84,
    )
    return DailyScene(date="2026-07-11", scenes=(scene,))


def test_process_s1_daily_writes_sigma0_in_db(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    keys = process_s1_daily(storage, "proj-a", _daily(tmp_path), BBOX)
    assert "sigma0" in keys
    assert keys["sigma0"].startswith("p/proj-a/layers/sentinel-1-grd_2026-07-11")
    assert storage.exists(keys["sigma0"])

    with MemoryFile(storage.get(keys["sigma0"])) as memfile, memfile.open() as src:
        data = src.read(1)
        assert src.dtypes[0] == "float32"
        assert data[0, 0] == pytest.approx(-10.0, abs=1e-4)  # 10*log10(0.1)
        assert src.nodata == NODATA


def test_second_process_reuses_the_level1_window(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    calls = {"n": 0}
    daily = _daily(tmp_path)

    def counting_opener(uri, bbox):
        calls["n"] += 1
        from mirarsetena.pipeline.window import read_window

        return read_window(uri, bbox)

    process_s1_daily(storage, "proj-a", daily, BBOX, opener=counting_opener)
    first = calls["n"]
    process_s1_daily(storage, "proj-a", daily, BBOX, opener=counting_opener)
    assert first == 1
    assert calls["n"] == 1  # window cache hit; layer re-encode is cheap

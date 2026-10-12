"""INSAR-PLAN L.3/L.5 — the InSAR pair job: SLC pair → LOS-cm product.

The injected processor produces an unwrapped-phase + coherence pair of
GeoTIFFs (SNAP + snaphu in production; a fake in tests). This job applies the
height delta (phase → LOS cm, coherence-masked) and stores the result at the
SERVABLE layer key the tile service reads — so the background job and the map
share one contract. Sources are purged like every pair job (SCOPE R4-Q4).
"""
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mirarsetena.coherence.insar import insar_result_key, run_insar_pair_job
from mirarsetena.coherence.pairs import InterferometricPair
from mirarsetena.pipeline.indices import layer_key
from mirarsetena.storage import LocalStore

BBOX = (-83.675, 9.378, -83.660, 9.397)


def _pair() -> InterferometricPair:
    from mirarsetena.pipeline.catalog import Scene

    first = Scene(
        id="S1A_TEST_1", collection="sentinel-1-slc",
        datetime="2026-07-11T23:47:58Z", date="2026-07-11",
        cloud=None, assets={}, orbit_state="ascending", relative_orbit=92,
    )
    second = Scene(
        id="S1A_TEST_2", collection="sentinel-1-slc",
        datetime="2026-07-23T23:47:59Z", date="2026-07-23",
        cloud=None, assets={}, orbit_state="ascending", relative_orbit=92,
    )
    return InterferometricPair(first=first, second=second)


def _geo(height=4, width=4):
    return {
        "crs": "EPSG:4326",
        "transform": from_origin(-83.675, 9.397, 0.00375, 0.00475),
        "height": height,
        "width": width,
    }


def _encode(values, like, nodata=None):
    from rasterio.io import MemoryFile

    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": like["height"], "width": like["width"],
        "crs": like["crs"], "transform": like["transform"],
        "nodata": nodata, "compress": "deflate",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(np.asarray(values, dtype=np.float32), 1)
        return mem.read()


def test_insar_result_key_is_the_servable_layer_key():
    pair = _pair()
    key = insar_result_key("proj-a", pair)
    # must equal what list_dates/TileService compute for the servable product
    assert key == layer_key("proj-a", "sentinel-1-slc", "2026-07-23", "insar")


def test_run_insar_pair_job_applies_height_delta_and_stores(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    like = _geo()
    phase = np.full((like["height"], like["width"]), np.pi, dtype=np.float32)
    coherence = np.full((like["height"], like["width"]), 0.9, dtype=np.float32)
    coherence[0, 0] = 0.05  # one incoherent pixel
    phase_bytes = _encode(phase, like)
    coherence_bytes = _encode(coherence, like)

    created = []

    def downloader(pair, workdir):
        paths = []
        for scene in (pair.first, pair.second):
            path = workdir / f"{scene.id}.zip"
            path.write_bytes(b"SLC" * 1000)
            paths.append(path)
        created.append(workdir)
        return paths

    def processor(paths, bbox):
        # production: SNAP+snaphu. Returns the unwrapped phase + coherence.
        assert all(p.exists() for p in paths)
        return {"phase": phase_bytes, "coherence": coherence_bytes}

    result = run_insar_pair_job(
        storage, "proj-a", _pair(), bbox=BBOX,
        downloader=downloader, processor=processor,
        workdir=tmp_path / "work", threshold=0.35,
    )

    key = insar_result_key("proj-a", _pair())
    assert result["key"] == key
    assert storage.get(key) is not None
    # the stored product IS the LOS-cm rendering source (float32, NODATA)
    with rasterio.io.MemoryFile(storage.get(key)) as mem, mem.open() as src:
        data = src.read(1)
        assert src.nodata == -9999.0
    assert data[2, 2] == pytest.approx(1.3866441, abs=1e-5)  # +λ/4 cm
    assert data[0, 0] == -9999.0  # incoherent pixel masked
    assert not created[0].exists()  # sources purged

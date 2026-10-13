"""INSAR-PLAN L.1 fallback — ISCE2 topsApp interferogram processor.

Seam: mirarsetena.coherence.isce. ISCE2 is the plan's fallback processor
(SNAP's installer CDN is hard-blocked on this network, verified via two
public resolvers — see H.3 spike note). Mirrors the SNAP adapter's contract:
`Processor(paths, bbox) -> {"phase": bytes, "coherence": bytes}`.

The topsApp run is driven by a pure config builder (testable golden) and a
Python-driver runner exercised against a fake `isce2` interpreter in CI.
"""
import sys

import pytest

from mirarsetena.coherence.cdse import CoherenceUnavailable
from mirarsetena.coherence.isce import (
    build_topsapp_driver,
    find_isce,
    make_isce_processor,
)

BBOX = (-83.675, 9.378, -83.660, 9.397)


def test_driver_wires_slcs_bbox_and_outputs():
    driver = build_topsapp_driver(
        slc1="/data/S1A_1.zip", slc2="/data/S1A_2.zip",
        bbox=BBOX, phase_out="/out/phase.tif", coherence_out="/out/coh.tif",
        workdir="/work",
    )
    # both SLCs feed the topsApp stack
    assert "/data/S1A_1.zip" in driver
    assert "/data/S1A_2.zip" in driver
    # topographic phase removal is on (mandatory over ~1169 m relief)
    assert "do_topo" in driver and "True" in driver
    # geocoded outputs land where build_los_product expects them
    assert "/out/phase.tif" in driver
    assert "/out/coh.tif" in driver
    # the AOI bbox reaches the topsApp subset window
    assert "-83.675" in driver and "9.397" in driver


def test_find_isce_absent_returns_none(monkeypatch):
    monkeypatch.setattr(
        "mirarsetena.coherence.isce.shutil.which", lambda name: None
    )
    assert find_isce() is None


def _fake_isce(tmp_path, *, fail=False):
    """A fake ISCE2 interpreter: writes the two output GeoTIFFs the driver
    names, or fails — so the runner can be exercised without ISCE2."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    for name in ("phase.tif", "coh.tif"):
        path = tmp_path / name
        profile = {
            "driver": "GTiff", "dtype": "float32", "count": 1,
            "height": 4, "width": 4, "crs": "EPSG:4326",
            "transform": from_origin(-83.675, 9.397, 0.00375, 0.00475),
            "nodata": -9999.0, "compress": "deflate",
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(np.full((4, 4), 3.14159, dtype=np.float32), 1)

    interp = tmp_path / "isce-python"
    if fail:
        body = f"#!{sys.executable}\nimport sys\nprint('topsapp failed', file=sys.stderr)\nsys.exit(1)\n"
    else:
        body = f"#!{sys.executable}\nimport sys\nsys.exit(0)\n"
    interp.write_text(body)
    interp.chmod(0o755)
    return interp


def test_isce_processor_runs_driver_and_returns_bands(tmp_path):
    interp = _fake_isce(tmp_path)
    processor = make_isce_processor(
        python_bin=str(interp), phase_out=tmp_path / "phase.tif",
        coherence_out=tmp_path / "coh.tif", workdir=tmp_path / "work",
    )
    result = processor([tmp_path / "a.zip", tmp_path / "b.zip"], BBOX)
    assert set(result) == {"phase", "coherence"}
    assert len(result["phase"]) > 0
    assert len(result["coherence"]) > 0


def test_isce_processor_failure_raises(tmp_path):
    interp = _fake_isce(tmp_path, fail=True)
    processor = make_isce_processor(
        python_bin=str(interp), phase_out=tmp_path / "phase.tif",
        coherence_out=tmp_path / "coh.tif", workdir=tmp_path / "work",
    )
    with pytest.raises(RuntimeError, match="topsapp failed"):
        processor([tmp_path / "a.zip", tmp_path / "b.zip"], BBOX)


def test_make_isce_processor_without_interp_raises(tmp_path):
    with pytest.raises(CoherenceUnavailable, match="ISCE2"):
        make_isce_processor(
            python_bin=None, phase_out=tmp_path / "phase.tif",
            coherence_out=tmp_path / "coh.tif", workdir=tmp_path / "work",
        )

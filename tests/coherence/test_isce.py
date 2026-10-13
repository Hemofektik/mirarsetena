"""INSAR-PLAN L.1 fallback — ISCE2 topsApp interferogram processor.

Seam: mirarsetena.coherence.isce. ISCE2 is the plan's fallback processor
(SNAP's installer CDN and the precise-orbit server have no reachable DNS on
this network, verified via public resolvers — see H.3 spike note). Mirrors
the SNAP adapter's contract: `Processor(paths, bbox) -> {"phase": bytes,
"coherence": bytes}`.

The real ISCE2 interface is the `topsApp.py` CLI driven by an XML runconfig
whose Reference/Secondary components are TOPS sensors over SAFE directories.
The config is a pure string builder (testable golden); the CLI invocation is
exercised against a fake `topsApp.py` in CI.
"""

import pytest

from mirarsetena.coherence.cdse import CoherenceUnavailable
from mirarsetena.coherence.isce import (
    build_topsapp_config,
    find_isce,
    make_isce_processor,
)

BBOX = (-83.675, 9.378, -83.660, 9.397)


def test_config_declares_reference_and_secondary_safe():
    xml = build_topsapp_config(
        safe1="/data/S1A_1.SAFE", safe2="/data/S1A_2.SAFE",
        swath=7, polarization="VV",
        bbox=BBOX, phase_out="/out/phase.tif", coherence_out="/out/coh.tif",
        workdir="/work", dem="/data/srtm.dem",
    )
    # the ISCE2 runconfig wraps everything in the application component —
    # without it topsApp parses NOTHING (frame.output stays None; caught by
    # the first real spike run)
    assert '<component name="topsinsar">' in xml
    # both SAFE directories feed the reference/secondary TOPS sensors
    assert xml.count("/data/S1A_1.SAFE") == 1
    assert xml.count("/data/S1A_2.SAFE") == 1
    assert '<component name="reference">' in xml
    assert '<component name="secondary">' in xml
    # separate output dirs: sharing one overwrites the unpack metadata
    assert "/work/reference" in xml
    assert "/work/secondary" in xml
    # swath + polarization reach the sensor config
    assert '<property name="swath number">7</property>' in xml
    assert '<property name="polarization">VV</property>' in xml
    # a DEM is wired (topographic phase removal over ~1169 m relief)
    assert "DEM" in xml and "/data/srtm.dem" in xml
    assert "-83.675" in xml and "9.397" in xml
    # snaphu unwrapping is requested
    assert "snaphu" in xml
    # swaths must be list-parsed: a bare int breaks getInputSwathList
    assert '<property name="swaths">[7]</property>' in xml
    # topsApp has no "do geocode" property — configuring it aborts the run
    assert "do geocode" not in xml


def test_config_omits_dem_property_when_not_given():
    xml = build_topsapp_config(
        safe1="/data/a.SAFE", safe2="/data/b.SAFE",
        swath=7, polarization="VV",
        bbox=BBOX, phase_out="/out/phase.tif", coherence_out="/out/coh.tif",
        workdir="/work",
    )
    assert "DEM filename" not in xml


def test_find_isce_absent_returns_none(monkeypatch):
    monkeypatch.setattr(
        "mirarsetena.coherence.isce.shutil.which", lambda name: None
    )
    monkeypatch.delenv("MIRAR_ISCE_PYTHON", raising=False)
    assert find_isce() is None


def _fake_topsapp(tmp_path, *, fail=False):
    """A fake topsApp.py: writes the two output GeoTIFFs the config names,
    or fails — so the runner can be exercised without ISCE2."""
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

    script = tmp_path / "topsApp.py"
    if fail:
        body = (
            "#!/usr/bin/env python3\n"
            "import sys\nprint('topsApp failed', file=sys.stderr)\nsys.exit(1)\n"
        )
    else:
        body = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
    script.write_text(body)
    script.chmod(0o755)
    return script


def test_isce_processor_runs_cli_and_returns_bands(tmp_path):
    script = _fake_topsapp(tmp_path)
    processor = make_isce_processor(
        python_bin="python3", topsapp_bin=str(script),
        phase_out=tmp_path / "phase.tif", coherence_out=tmp_path / "coh.tif",
        workdir=tmp_path / "work",
    )
    result = processor([tmp_path / "a.SAFE", tmp_path / "b.SAFE"], BBOX)
    assert set(result) == {"phase", "coherence"}
    assert len(result["phase"]) > 0
    assert len(result["coherence"]) > 0


def test_isce_processor_failure_raises(tmp_path):
    script = _fake_topsapp(tmp_path, fail=True)
    processor = make_isce_processor(
        python_bin="python3", topsapp_bin=str(script),
        phase_out=tmp_path / "phase.tif", coherence_out=tmp_path / "coh.tif",
        workdir=tmp_path / "work",
    )
    with pytest.raises(RuntimeError, match="topsApp failed"):
        processor([tmp_path / "a.SAFE", tmp_path / "b.SAFE"], BBOX)


def test_make_isce_processor_without_topsapp_raises(tmp_path):
    with pytest.raises(CoherenceUnavailable, match="ISCE2"):
        make_isce_processor(
            python_bin="python3", topsapp_bin=None,
            phase_out=tmp_path / "phase.tif",
            coherence_out=tmp_path / "coh.tif", workdir=tmp_path / "work",
        )

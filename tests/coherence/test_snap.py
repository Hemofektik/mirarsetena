"""INSAR-PLAN L.1 — SNAP GPT interferogram processor (scaffolding + spike).

Seam: mirarsetena.coherence.snap. Builds the SNAP GPT graph that turns a
downloaded SLC pair into an unwrapped-phase + coherence GeoTIFF, and runs it
via the `gpt` binary. The graph is pure (testable golden); the runner shells
out to `gpt` and is exercised against a FAKE `gpt` script (no SNAP installed
in CI — the real spike is the L.1 decision gate that fills H.3).
"""
import shutil

import pytest

from mirarsetena.coherence.snap import (
    build_interferogram_graph,
    find_gpt,
    make_snap_processor,
)

BBOX = (-83.675, 9.378, -83.660, 9.397)


def test_graph_contains_interferogram_chain_in_order():
    xml = build_interferogram_graph(
        slc1="/data/S1A_1.zip", slc2="/data/S1A_2.zip",
        bbox=BBOX, phase_out="/out/phase.tif", coherence_out="/out/coh.tif",
    )
    # the standard InSAR chain, in operator-execution order
    sequence = [
        "Read", "Apply-Orbit-File", "Back-Geocoding", "Interferogram",
        "TopoPhaseRemoval", "GoldsteinPhaseFiltering", "Subset", "Write",
    ]
    positions = [xml.index(f"<operator>{op}</operator>") for op in sequence]
    assert positions == sorted(positions), "SNAP nodes out of chain order"
    # both SLCs are wired into Read nodes
    assert xml.count("/data/S1A_1.zip") == 1
    assert xml.count("/data/S1A_2.zip") == 1
    # the AOI bbox appears as the Subset region
    assert "-83.675" in xml and "9.397" in xml


def test_find_gpt_absent_returns_none(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert find_gpt() is None


def test_find_gpt_present_returns_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/snap/bin/gpt")
    assert find_gpt() == "/opt/snap/bin/gpt"


def _fake_gpt(tmp_path, *, fail=False):
    """A fake `gpt` executable: writes the two output GeoTIFFs the graph
    names, or fails — so the runner can be exercised without SNAP."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    # pre-build the fixture rasters the fake gpt will "produce"
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

    script = tmp_path / "gpt"
    if fail:
        body = "#!/bin/sh\necho 'graph failed' >&2\nexit 1\n"
    else:
        body = "#!/bin/sh\nexit 0\n"
    script.write_text(body)
    script.chmod(0o755)
    return script


def test_snap_processor_runs_gpt_and_returns_bands(tmp_path):
    gpt = _fake_gpt(tmp_path)
    phase_out = tmp_path / "phase.tif"
    coh_out = tmp_path / "coh.tif"
    processor = make_snap_processor(
        gpt_bin=str(gpt), phase_out=phase_out, coherence_out=coh_out,
    )
    result = processor([tmp_path / "a.zip", tmp_path / "b.zip"], BBOX)
    assert set(result) == {"phase", "coherence"}
    assert result["phase"].startswith(b"\x89PNG") or len(result["phase"]) > 0
    assert len(result["coherence"]) > 0


def test_snap_processor_gpt_failure_raises(tmp_path):
    gpt = _fake_gpt(tmp_path, fail=True)
    processor = make_snap_processor(
        gpt_bin=str(gpt), phase_out=tmp_path / "phase.tif",
        coherence_out=tmp_path / "coh.tif",
    )
    with pytest.raises(RuntimeError, match="gpt failed"):
        processor([tmp_path / "a.zip", tmp_path / "b.zip"], BBOX)


def test_make_snap_processor_without_gpt_raises(tmp_path):
    from mirarsetena.coherence.cdse import CoherenceUnavailable

    with pytest.raises(CoherenceUnavailable, match="gpt"):
        make_snap_processor(
            gpt_bin=None, phase_out=tmp_path / "phase.tif",
            coherence_out=tmp_path / "coh.tif",
        )

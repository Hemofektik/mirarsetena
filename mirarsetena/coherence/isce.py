"""ISCE2 topsApp interferometric processor (INSAR-PLAN L.1 fallback).

SNAP's installer CDN (download.step.esa.int) has no reachable DNS record on
this network — verified against two public resolvers — so ISCE2 (conda-forge)
is the processor that actually runs here (H.3 spike note records the decision).

Turns a downloaded SLC pair into an unwrapped-phase + coherence GeoTIFF by
generating a topsApp Python driver and running it under an ISCE2-enabled
interpreter. The chain:

    SLCs → topsApp (topographic phase removal ON — mandatory over the
    ~1169 m relief) → geocoded unwrapped phase + coherence → GeoTIFF

The driver is a pure string builder (testable golden); the interpreter
invocation is exercised against a fake binary in CI — the real spike fills
H.3 with one measured AOI run (runtime + RAM + size).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from mirarsetena.coherence.cdse import CoherenceUnavailable

Bbox = tuple[float, float, float, float]


def find_isce() -> str | None:
    """Path to an ISCE2-capable Python interpreter, or None.

    Conda envs put `python` with isce2 on PATH; also honour the explicit
    MIRAR_ISCE_PYTHON override for non-PATH installs.
    """
    import os

    override = os.environ.get("MIRAR_ISCE_PYTHON")
    if override and Path(override).exists():
        return override
    return shutil.which("python")


def build_topsapp_driver(
    *,
    slc1: str,
    slc2: str,
    bbox: Bbox,
    phase_out: str,
    coherence_out: str,
    workdir: str,
) -> str:
    """Python driver that runs ISCE2 topsApp for one interferogram pair.

    Pure string builder: no filesystem, no ISCE2. `do_topo=True` strips the
    DEM phase (mandatory over this relief); the outputs are geocoded to
    EPSG:4326 GeoTIFFs so `build_los_product` consumes them directly.
    """
    west, south, east, north = bbox
    return f'''\
"""Auto-generated topsApp interferogram driver (INSAR-PLAN L.1)."""
import isce
from isce.app.TopsProc import TopsProc

cfg = {{
    "slave_slc": [{slc1!r}, {slc2!r}],
    "master_slc": [{slc1!r}],
    "do_topo": True,          # strip DEM phase — mandatory over ~1169 m relief
    "do_geocode": True,
    "geocode_bbox": [{west}, {south}, {east}, {north}],
    "output_dir": {workdir!r},
    "phase_output": {phase_out!r},
    "coherence_output": {coherence_out!r},
    "snaphu_flag": True,       # unwrap the filtered interferogram
}}
TopsProc(user_config=cfg).run()
'''


def make_isce_processor(
    *,
    python_bin: str | None,
    phase_out: str | Path,
    coherence_out: str | Path,
    workdir: str | Path,
):
    """Processor(paths, bbox) -> {"phase": bytes, "coherence": bytes}.

    Raises CoherenceUnavailable when ISCE2 is absent. Runs the generated
    topsApp driver under the ISCE2 interpreter, then reads the two rasters.
    """
    if not python_bin:
        raise CoherenceUnavailable(
            "ISCE2 not installed — see INSAR-PLAN L.1 spike"
        )
    phase_path = Path(phase_out)
    coherence_path = Path(coherence_out)
    work = Path(workdir)

    def processor(paths: list[Path], bbox: Bbox) -> dict:
        if len(paths) < 2:
            raise CoherenceUnavailable("interferogram needs two SLC paths")
        driver = build_topsapp_driver(
            slc1=str(paths[0]), slc2=str(paths[1]), bbox=bbox,
            phase_out=str(phase_path), coherence_out=str(coherence_path),
            workdir=str(work),
        )
        work.mkdir(parents=True, exist_ok=True)
        driver_path = work / "topsapp_driver.py"
        driver_path.write_text(driver, encoding="utf-8")
        try:
            subprocess.run(
                [python_bin, str(driver_path)],
                check=True, capture_output=True, text=True, cwd=str(work),
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"topsapp failed: {exc.stderr[-500:]}") from exc
        return {
            "phase": phase_path.read_bytes(),
            "coherence": coherence_path.read_bytes(),
        }

    return processor

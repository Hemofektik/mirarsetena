"""ISCE2 topsApp interferometric processor (INSAR-PLAN L.1 fallback).

SNAP's installer CDN (download.step.esa.int) and the precise-orbit server
(qc.assist.copernicus.eu) have no reachable DNS record on this host — verified
against public resolvers — so ISCE2 (conda-forge) is the processor that runs
here (H.3 spike note records the decision).

The real ISCE2 interface is the `topsApp.py` CLI driven by an XML runconfig
whose Reference/Secondary components are TOPS sensors over SAFE directories.
This module generates that runconfig (pure builder) and invokes the CLI:

    topsApp.py <runconfig.xml>

`dounwrap` + `unwrappername=snaphu` produce the unwrapped phase; the geocode
step writes phase + coherence GeoTIFFs that `build_los_product` consumes.
The interpreter + CLI path are injectable; CI exercises the runner against a
fake `topsApp.py`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from mirarsetena.coherence.cdse import CoherenceUnavailable

Bbox = tuple[float, float, float, float]


def find_isce() -> str | None:
    """Path to an ISCE2-capable Python interpreter, or None.

    Conda envs put a `python` with isce2 importable on PATH; honour the
    explicit MIRAR_ISCE_PYTHON override for non-PATH installs.
    """
    override = os.environ.get("MIRAR_ISCE_PYTHON")
    if override and Path(override).exists():
        return override
    return shutil.which("python")


def find_topsapp() -> str | None:
    """Path to the real `topsApp.py` launcher inside the isce2 package, or None."""
    override = os.environ.get("MIRAR_TOPSAPP")
    if override and Path(override).exists():
        return override
    here = shutil.which("python")
    if here is None:
        return None
    # isce2 conda package ships topsApp.py under site-packages/isce/applications
    try:
        out = subprocess.run(
            [here, "-c", "import isce, os; print(os.path.join(os.path.dirname(isce.__file__), 'applications', 'topsApp.py'))"],
            capture_output=True, text=True, check=True,
        )
        candidate = out.stdout.strip().splitlines()[-1]
        return candidate if Path(candidate).exists() else None
    except (subprocess.CalledProcessError, IndexError):
        return None


def build_topsapp_config(
    *,
    safe1: str,
    safe2: str,
    swath: int,
    polarization: str,
    bbox: Bbox,
    phase_out: str,
    coherence_out: str,
    workdir: str,
    dem: str | None = None,
) -> str:
    """topsApp XML runconfig for one Sentinel-1 interferogram pair.

    Pure string builder: no filesystem, no ISCE2. Reference = first SAFE,
    Secondary = second SAFE; `dounwrap` + snaphu produce unwrapped phase;
    the geocode step writes phase + coherence to the two output paths.
    `dem` (when given) pins the topographic-phase-removal DEM over the
    ~1169 m relief (omitting it lets topsApp fetch one, which needs network).
    """
    west, south, east, north = bbox
    dem_line = f'    <property name="DEM filename">{dem}</property>\n' if dem else ""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<topsApp>
  <property name="sensor name">SENTINEL1</property>
{dem_line}    <property name="do unwrap">True</property>
    <property name="unwrapper name">snaphu</property>
    <property name="do geocode">True</property>
    <property name="geocode bounding box">{north} {south} {east} {west}</property>
    <property name=" azimuth looks">2</property>
    <property name="range looks">8</property>
  <component name="Reference">
    <factory-module>isceobj.Sensor.TOPS</factory-module>
    <property name="safe">{safe1}</property>
    <property name="swath number">{swath}</property>
    <property name="polarization">{polarization}</property>
    <property name="output directory">{workdir}</property>
  </component>
  <component name="Secondary">
    <factory-module>isceobj.Sensor.TOPS</factory-module>
    <property name="safe">{safe2}</property>
    <property name="swath number">{swath}</property>
    <property name="polarization">{polarization}</property>
    <property name="output directory">{workdir}</property>
  </component>
</topsApp>
"""


def make_isce_processor(
    *,
    python_bin: str | None,
    topsapp_bin: str | None,
    phase_out: str | Path,
    coherence_out: str | Path,
    workdir: str | Path,
    swath: int = 7,
    polarization: str = "VV",
    dem: str | None = None,
):
    """Processor(paths, bbox) -> {"phase": bytes, "coherence": bytes}.

    Raises CoherenceUnavailable when ISCE2/topsApp is absent. Runs the real
    `topsApp.py` CLI on a generated runconfig for the two SAFE paths + bbox,
    then reads the two output rasters.
    """
    if not python_bin or not topsapp_bin:
        raise CoherenceUnavailable(
            "ISCE2 not installed — see INSAR-PLAN L.1 spike"
        )
    phase_path = Path(phase_out)
    coherence_path = Path(coherence_out)
    work = Path(workdir)

    def processor(paths: list[Path], bbox: Bbox) -> dict:
        if len(paths) < 2:
            raise CoherenceUnavailable("interferogram needs two SAFE paths")
        config = build_topsapp_config(
            safe1=str(paths[0]), safe2=str(paths[1]), swath=swath,
            polarization=polarization, bbox=bbox,
            phase_out=str(phase_path), coherence_out=str(coherence_path),
            workdir=str(work), dem=dem,
        )
        work.mkdir(parents=True, exist_ok=True)
        config_path = work / "topsapp_runconfig.xml"
        config_path.write_text(config, encoding="utf-8")
        try:
            subprocess.run(
                [python_bin, topsapp_bin, str(config_path)],
                check=True, capture_output=True, text=True, cwd=str(work),
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"topsApp failed: {exc.stderr[-500:]}") from exc
        return {
            "phase": phase_path.read_bytes(),
            "coherence": coherence_path.read_bytes(),
        }

    return processor

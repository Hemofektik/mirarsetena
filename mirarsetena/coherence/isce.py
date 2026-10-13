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
import xml.etree.ElementTree as ET
from pathlib import Path

from mirarsetena.coherence.cdse import CoherenceUnavailable

Bbox = tuple[float, float, float, float]


def annotation_corners(
    annotation_xml: str | Path,
) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) from a SAFE annotation's geolocation grid.

    The DEM for topsApp must cover the FULL swath footprint — processing runs
    over the whole swath, and an under-sized DEM leaves NaN heights that abort
    snaphu ("NaN or infinity found in input float data"; first spike run:
    180x120 km IW2 swath vs a 5x5 km AOI DEM). None when the annotation has
    no corner points.
    """
    try:
        root = ET.parse(annotation_xml).getroot()
    except (ET.ParseError, OSError):
        return None
    lats: list[float] = []
    lons: list[float] = []
    for point in root.iter("geolocationGridPoint"):
        lat = point.findtext("latitude")
        lon = point.findtext("longitude")
        if lat is not None and lon is not None:
            lats.append(float(lat))
            lons.append(float(lon))
    if not lats:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


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
    """Path to the real `topsApp.py` launcher inside the isce2 package, or None.

    Probes the CURRENT interpreter first (sys.executable) — when the job runs
    inside the conda `insar` env that interpreter has isce2 importable, while
    `shutil.which("python")` may resolve to a system Python without it.
    """
    import sys

    override = os.environ.get("MIRAR_TOPSAPP")
    if override and Path(override).exists():
        return override
    for interpreter in (sys.executable, shutil.which("python")):
        if not interpreter:
            continue
        # isce2 conda package ships topsApp.py under site-packages/isce/applications
        try:
            out = subprocess.run(
                [interpreter, "-c", "import isce, os; print(os.path.join(os.path.dirname(isce.__file__), 'applications', 'topsApp.py'))"],
                capture_output=True, text=True, check=True,
            )
            candidate = out.stdout.strip().splitlines()[-1]
            if Path(candidate).exists():
                return candidate
        except (subprocess.CalledProcessError, IndexError):
            continue
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

    Pure string builder: no filesystem, no ISCE2. Follows the canonical
    ISCE2 format (examples/input_files/topsApp.xml): EVERYTHING nests under
    `<component name="topsinsar">` — without that wrapper topsApp parses
    nothing and runPreprocessor dies on `frame.output = None` (caught by the
    first real spike run). Reference/Secondary are sub-components with
    SEPARATE output directories (sharing one overwrites the unpack metadata),
    and there is deliberately no "do geocode" property (it aborts configure).

    `dem` pins the topographic-phase-removal DEM over the ~1169 m relief
    (omitting it makes topsApp fetch one, which needs network access we do
    not have). Outputs land in `workdir` (topsApp naming);
    `phase_out`/`coherence_out` document where the post-processing step
    writes the bands `build_los_product` consumes.
    """
    west, south, east, north = bbox
    # lowercase: Sentinel1 globs annotations as `-slc-vv-*` (case-sensitive)
    polarization = polarization.lower()
    dem_line = (
        f'    <property name="DEM filename">{dem}</property>\n' if dem else ""
    )
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<topsApp>
  <component name="topsinsar">
{dem_line}    <property name="swaths">[{swath}]</property>  <!-- list form: a bare int has no len() in getInputSwathList -->
    <property name="do unwrap">True</property>
    <property name="unwrapper name">snaphu</property>
    <property name="geocode bounding box">{north} {south} {east} {west}</property>
    <component name="reference">
      <property name="safe">{safe1}</property>
      <property name="swath number">{swath}</property>
      <property name="polarization">{polarization}</property>
      <property name="output directory">{workdir}/reference</property>
    </component>
    <component name="secondary">
      <property name="safe">{safe2}</property>
      <property name="swath number">{swath}</property>
      <property name="polarization">{polarization}</property>
      <property name="output directory">{workdir}/secondary</property>
    </component>
  </component>
</topsApp>
<!-- phase -> {phase_out} ; coherence -> {coherence_out} (post-processing) -->
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

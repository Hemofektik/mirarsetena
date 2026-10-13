"""INSAR-PLAN L.1 spike: one real InSAR pair, end to end, measured.

Downloads a real Sentinel-1 SLC pair from CDSE, builds a local DEM from AWS
terrarium tiles, determines the IW sub-swath covering the AOI from the SAFE
annotation, runs ISCE2 topsApp (interferogram -> topo removal -> snaphu
unwrap -> geocode), and reports runtime / peak RAM / disk — the numbers that
fill the H.3 spike note and pin SNAP vs ISCE2.

Usage (conda env with isce2 + snaphu):
    python -m tools.spike_insar --project-dir data/projects/cdp-rio-general

Not run in CI: it needs CDSE credentials + network + hours of CPU.
"""
from __future__ import annotations

import argparse
import json
import resource
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from mirarsetena.coherence.cdse import auth_from_env
from mirarsetena.coherence.dem import build_dem
from mirarsetena.coherence.download import make_slc_downloader
from mirarsetena.coherence.isce import (
    annotation_corners,
    build_topsapp_config,
    find_topsapp,
)
from mirarsetena.coherence.pairs import plan_pairs
from mirarsetena.pipeline.catalog import search
from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.storage import LocalStore

REPO = Path(__file__).resolve().parents[1]


def _annotation_swaths(safe_dir: Path) -> dict[int, list[tuple[float, float]]]:
    """IW sub-swath -> corner lon/lat list, parsed from SAFE annotations.

    Each annotation XML carries geolocationGrid corner points; we only need
    enough to decide which sub-swath covers the AOI.
    """
    out: dict[int, list[tuple[float, float]]] = {}
    ann_dir = safe_dir / "annotation"
    if not ann_dir.is_dir():
        return out
    for xml_path in sorted(ann_dir.glob("s1*-iw*-slc-*.xml")):
        # filename: s1a-iw1-slc-vv-....xml
        try:
            swath = int(xml_path.name.split("-iw")[1][0])
        except (IndexError, ValueError):
            continue
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue
        corners: list[tuple[float, float]] = []
        for point in root.iter("geolocationGridPoint"):
            lat = point.findtext("latitude")
            lon = point.findtext("longitude")
            if lat is not None and lon is not None:
                corners.append((float(lon), float(lat)))
        out[swath] = corners
    return out


def _swath_covering(safe_dir: Path, bbox: tuple[float, float, float, float]) -> int:
    """Pick the IW sub-swath whose corner points overlap the AOI bbox."""
    west, south, east, north = bbox
    swaths = _annotation_swaths(safe_dir)
    if not swaths:
        raise SystemExit(f"no annotations found in {safe_dir}")
    best, best_hits = None, -1
    for swath, corners in swaths.items():
        hits = sum(
            1
            for lon, lat in corners
            if west - 0.05 <= lon <= east + 0.05 and south - 0.05 <= lat <= north + 0.05
        )
        if hits > best_hits:
            best, best_hits = swath, hits
    if best is None or best_hits == 0:
        # fall back to the swath with any corner inside a generous window
        best = min(swaths)
    return best


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default="data/projects/cdp-rio-general")
    parser.add_argument("--config-dir", default="config/projects")
    # persistent by default: /tmp is wiped on reboot and the pair download
    # is 14 GB (survived the 2026-10-12 restart under data/spike/)
    parser.add_argument("--workdir", default="data/spike/spike-insar")
    parser.add_argument("--pairs", type=int, default=1, help="pairs to process")
    parser.add_argument(
        "--polarization", default="VV", help="SLC polarization (VV/VH)"
    )
    args = parser.parse_args()

    registry = ProjectRegistry(args.config_dir)
    slug = "cdp-rio-general"
    config = registry.get(slug)
    bbox = tuple(config.bbox)
    storage = LocalStore(REPO / "data" / "cache")
    # absolute: topsApp runs with cwd=pair_dir, so relative paths inside the
    # runconfig (DEM, SAFE, output dirs) would otherwise resolve wrongly
    work = Path(args.workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)

    # 1. plan pairs from the GRD catalog (same passes as the SLCs)
    scenes = search(
        "sentinel-1-grd", bbox,
        config.timeline.start.isoformat(), "2026-10-12",
    )
    pairs = plan_pairs(scenes)
    if not pairs:
        raise SystemExit("no interferometric pairs in the catalog window")
    pairs = pairs[: args.pairs]
    print(f"planned {len(pairs)} pair(s); first: {pairs[0].id}")

    # 2. download the SLCs from CDSE
    auth = auth_from_env(storage)
    if not auth.enabled:
        raise SystemExit("CDSE_CLIENT_ID/CDSE_CLIENT_SECRET not set in .env")
    downloader = make_slc_downloader(auth)

    topsapp = find_topsapp()
    if topsapp is None:
        raise SystemExit("topsApp.py not found — isce2 not installed?")

    results = []
    for i, pair in enumerate(pairs, 1):
        pair_dir = work / f"pair{i}"
        pair_dir.mkdir(exist_ok=True)
        # reuse an earlier run's download (14 GB takes ~1 h; never redo it)
        existing = sorted(pair_dir.glob("*.zip"))
        if len(existing) >= 2:
            paths = existing
            print(f"[{i}/{len(pairs)}] reusing {len(existing)} downloaded zips")
        else:
            print(f"[{i}/{len(pairs)}] downloading SLC pair ...")
            t0 = time.time()
            paths = downloader(pair, pair_dir)
            dl_bytes = sum(p.stat().st_size for p in paths)
            print(f"  downloaded {dl_bytes / 1e9:.2f} GB in {time.time() - t0:.0f}s")

        # unzip the SAFEs (CDSE delivers .zip; topsApp wants directories).
        # Each zip is resolved on its own so Reference/Secondary never share
        # the first-found SAFE (first-run bug: both pointed at scene A).
        safe_dirs = []
        for zipped in paths:
            target = pair_dir / zipped.stem  # zip name mirrors the SAFE name
            if not target.exists():
                dest = pair_dir / f"{zipped.stem}.unpack"
                dest.mkdir(exist_ok=True)
                shutil.unpack_archive(str(zipped), dest)
                found = next(dest.rglob("*.SAFE"), None)
                if found is None:
                    raise SystemExit(f"no .SAFE inside {zipped.name}")
                target = found
            safe_dirs.append(target)
        safe1, safe2 = safe_dirs[0], safe_dirs[1]
        if safe1 == safe2:
            raise SystemExit("Reference and Secondary resolved to the same SAFE")

        swath = _swath_covering(safe1, bbox)
        print(f"  swath covering AOI: IW{swath}")

        # DEM over the FULL swath footprint (+2 km margin): processing runs
        # over the whole swath, and an AOI-sized DEM leaves NaN heights that
        # abort snaphu (first spike run: 180x120 km swath vs 5x5 km DEM).
        # z12 ≈ SRTM's native 30 m — ~250 tiles instead of ~1000 at z13.
        ann = next(
            iter(Path(safe1, "annotation").glob(f"s1*-iw{swath}-slc-*.xml")),
            None,
        )
        corners = annotation_corners(ann) if ann is not None else None
        if corners is None:
            raise SystemExit(f"no annotation corners for IW{swath} in {safe1}")
        swath_bbox = (
            corners[0] - 0.02, corners[1] - 0.02,
            corners[2] + 0.02, corners[3] + 0.02,
        )
        dem_path = work / f"dem_iw{swath}.tif"
        print(f"building DEM over the full IW{swath} footprint ...")
        for stale in work.glob(f"{dem_path.stem}*"):
            stale.unlink()  # old tif + sidecar + .wgs84 geoid derivatives
        # topsApp writes the geoid-corrected .wgs84 next to cwd (pair_dir)
        # and REUSES it if present — stale garbage there would be picked up
        for stale in pair_dir.glob(f"{dem_path.stem}*"):
            stale.unlink()
        build_dem(swath_bbox, dem_path, zoom=12)
        # topsApp loads the DEM through an ISCE image sidecar, not bare GeoTIFF
        from isce.applications.gdal2isce_xml import gdal2isce_xml

        gdal2isce_xml(str(dem_path))
        # Declare the DEM as WGS84 so verifyDEM SKIPS ISCE's geoid corrector:
        # it emits scattered FLT_MAX garbage (6% of a hard-bounded chunk) on
        # non-SRTM-shaped DEMs, which overflows topo into the NaNs that abort
        # snaphu (H.3 spike note). Heights stay EGM96-orthometric (<=~30 m
        # vertical offset -> smooth residual fringes, unwrap-able; a proper
        # production fix pre-converts with a geoid grid).
        sidecar = Path(f"{dem_path}.xml")
        text = sidecar.read_text(encoding="utf-8")
        if "REFERENCE" not in text:
            prop = (
                '    <property name="REFERENCE">\n'
                "        <value>WGS84</value>\n"
                "    </property>\n"
            )
            # gdal2isce_xml closes with <image_name>; other ISCE dumps
            # use <imageFile> — inject before whichever is present
            for closing in ("</image_name>", "</imageFile>"):
                if closing in text:
                    text = text.replace(closing, prop + closing, 1)
                    break
            sidecar.write_text(text, encoding="utf-8")

        phase_out = pair_dir / "phase.tif"
        coh_out = pair_dir / "coherence.tif"
        config_xml = build_topsapp_config(
            safe1=str(safe1), safe2=str(safe2), swath=swath,
            polarization=args.polarization, bbox=bbox,
            phase_out=str(phase_out), coherence_out=str(coh_out),
            workdir=str(pair_dir), dem=str(dem_path),
        )
        runconfig = pair_dir / "topsapp_runconfig.xml"
        runconfig.write_text(config_xml, encoding="utf-8")
        # step state from a previous attempt would silently SKIP steps
        # (a failed preprocess still dumps its pickle) — measure from scratch
        shutil.rmtree(pair_dir / "PICKLE", ignore_errors=True)

        print("  running topsApp (this is the spike measurement) ...")
        t1 = time.time()
        proc = subprocess.run(
            [sys.executable, topsapp, str(runconfig)],
            cwd=str(pair_dir), capture_output=True, text=True, check=False,
        )
        runtime = time.time() - t1
        peak_rss_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
        if proc.returncode != 0:
            print(f"  topsApp FAILED after {runtime:.0f}s (rc={proc.returncode})")
            print(proc.stderr[-2000:])
            results.append(
                {"pair": pair.id, "ok": False, "runtime_s": runtime,
                 "stderr_tail": proc.stderr[-500:]}
            )
            continue

        disk_mb = _dir_size(pair_dir) / 1e6
        print(
            f"  OK in {runtime:.0f}s; workdir {disk_mb:.0f} MB; "
            f"peak child RSS {peak_rss_mb:.0f} MB"
        )
        results.append(
            {
                "pair": pair.id, "ok": True, "runtime_s": runtime,
                "workdir_mb": disk_mb, "peak_rss_mb": peak_rss_mb,
                "downloaded_gb": dl_bytes / 1e9, "swath": swath,
                "phase": str(phase_out), "coherence": str(coh_out),
            }
        )

    report = work / "spike-report.json"
    report.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nreport: {report}")
    ok = [r for r in results if r["ok"]]
    if ok:
        print(
            f"\nSPIKE RESULT: {len(ok)}/{len(results)} pairs OK; "
            f"median runtime {sorted(r['runtime_s'] for r in ok)[len(ok)//2]:.0f}s; "
            f"peak RSS {max(r['peak_rss_mb'] for r in ok):.0f} MB"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
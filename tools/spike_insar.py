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
from mirarsetena.coherence.isce import build_topsapp_config, find_topsapp
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
    parser.add_argument("--workdir", default="/tmp/spike-insar")
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
    work = Path(args.workdir)
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

    # 3. local DEM from AWS terrarium (USGS source is unreachable here)
    dem_path = work / "aoi.dem.tif"
    if not dem_path.exists():
        print("building DEM from AWS terrarium tiles ...")
        build_dem(bbox, dem_path, zoom=13)

    topsapp = find_topsapp()
    if topsapp is None:
        raise SystemExit("topsApp.py not found — isce2 not installed?")

    results = []
    for i, pair in enumerate(pairs, 1):
        pair_dir = work / f"pair{i}"
        pair_dir.mkdir(exist_ok=True)
        print(f"[{i}/{len(pairs)}] downloading SLC pair ...")
        t0 = time.time()
        paths = downloader(pair, pair_dir)
        dl_bytes = sum(p.stat().st_size for p in paths)
        print(f"  downloaded {dl_bytes / 1e9:.2f} GB in {time.time() - t0:.0f}s")

        # unzip the SAFEs (CDSE delivers .zip; topsApp wants directories)
        safe_dirs = []
        for zipped in paths:
            safe_dir = pair_dir / zipped.stem
            if not safe_dir.exists():
                shutil.unpack_archive(str(zipped), pair_dir)
            # unpack_archive may nest; find the .SAFE dir
            found = next(pair_dir.rglob("*.SAFE"), None)
            if found is None:
                raise SystemExit(f"no .SAFE after unpacking {zipped.name}")
            safe_dirs.append(found)
        safe1, safe2 = safe_dirs[0], safe_dirs[1]

        swath = _swath_covering(safe1, bbox)
        print(f"  swath covering AOI: IW{swath}")

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
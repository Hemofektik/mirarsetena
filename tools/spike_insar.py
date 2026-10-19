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
from itertools import pairwise
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


def _annotation_swaths(safe_dir: Path) -> dict[int, list[tuple[int, int, float, float]]]:
    """IW sub-swath -> (line, pixel, lon, lat) geolocation grid points,
    parsed from SAFE annotations. line/pixel reconstruct the 2D grid, so
    coverage can be INTERPOLATED instead of testing sparse nodes."""
    out: dict[int, list[tuple[int, int, float, float]]] = {}
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
        points: list[tuple[int, int, float, float]] = []
        for point in root.iter("geolocationGridPoint"):
            lat = point.findtext("latitude")
            lon = point.findtext("longitude")
            line = point.findtext("line")
            pixel = point.findtext("pixel")
            if lat is not None and lon is not None and line and pixel:
                points.append(
                    (int(line), int(pixel), float(lon), float(lat))
                )
        out[swath] = points
    return out


# lon slack when testing AOI-centre containment in a swath band: covers
# interpolation noise at the strip edge, nothing more
SWATH_LON_EPS = 0.02


def lon_band_at(
    points: list[tuple[int, int, float, float]], lat: float
) -> tuple[float, float] | None:
    """Across-track lon span of a swath at one latitude.

    Annotation geolocation grids are ~0.11 deg apart — far coarser than
    the 0.015 deg AOI — so membership must interpolate along-track within
    each grid column. None when the latitude is off the strip.
    """
    columns: dict[int, list[tuple[float, float]]] = {}
    for _line, pixel, lon, point_lat in points:
        columns.setdefault(pixel, []).append((point_lat, lon))
    lo = hi = None
    for samples in columns.values():
        samples.sort()
        if not (samples[0][0] <= lat <= samples[-1][0]):
            continue
        for (a_lat, a_lon), (b_lat, b_lon) in pairwise(samples):
            if a_lat <= lat <= b_lat:
                t = 0.0 if b_lat == a_lat else (lat - a_lat) / (b_lat - a_lat)
                value = a_lon + t * (b_lon - a_lon)
                lo = value if lo is None else min(lo, value)
                hi = value if hi is None else max(hi, value)
                break
    if lo is None:
        return None
    return lo, hi


def swath_covering(
    points: list[tuple[int, int, float, float]],
    bbox: tuple[float, float, float, float],
    *,
    lon_eps: float = SWATH_LON_EPS,
) -> bool:
    """True when the strip CONTINUOUSLY covers the bbox centre latitude."""
    west, south, east, north = bbox
    band = lon_band_at(points, (south + north) / 2)
    if band is None:
        return False
    lo, hi = band
    lon = (west + east) / 2
    return lo - lon_eps <= lon <= hi + lon_eps


def _swath_covering(safe_dir: Path, bbox: tuple[float, float, float, float]) -> int:
    """The IW sub-swath whose strip covers the AOI (deepest margin wins).

    The old selection counted grid NODES inside the AOI — a lottery on the
    sparse annotation grid: for the 2026-07-23 ASC pass no node landed in
    the bbox, the fallback picked IW1 (which ends 0.5 deg west), and
    topsApp burned 65 minutes on a swath where the crop found nothing.
    """
    west, south, east, north = bbox
    lat = (south + north) / 2
    lon = (west + east) / 2
    swaths = _annotation_swaths(safe_dir)
    if not swaths:
        raise SystemExit(f"no annotations found in {safe_dir}")
    best, best_margin = None, None
    bands: dict[int, tuple[float, float] | None] = {}
    for swath in sorted(swaths):
        band = lon_band_at(swaths[swath], lat)
        bands[swath] = band
        if band is None:
            continue
        lo, hi = band
        if lo - SWATH_LON_EPS <= lon <= hi + SWATH_LON_EPS:
            margin = min(lon - lo, hi - lon)
            if best_margin is None or margin > best_margin:
                best, best_margin = swath, margin
    if best is not None:
        return best
    # fail BEFORE topsApp, with the evidence
    detail = ", ".join(
        f"IW{s}="
        + ("off-strip" if b is None else f"{b[0]:.3f}..{b[1]:.3f}")
        for s, b in bands.items()
    )
    raise SystemExit(
        f"no IW sub-swath covers lon {lon:.4f} at lat {lat:.4f} "
        f"(bands: {detail})"
    )


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _image_dims(xml_path: Path) -> tuple[int, int]:
    """(width, length) from an ISCE image sidecar."""
    root = ET.parse(xml_path).getroot()
    out: dict[str, int] = {}
    for prop in root.iter("property"):
        name = (prop.get("name") or "").lower()
        value = prop.findtext("value")
        if name in ("width", "length") and value:
            out[name] = int(value)
    return out["width"], out["length"]


def _mask_ifg_nan(pair_dir: Path) -> int:
    """Zero NaN interferogram pixels (phase + both coherence bands).

    12-day tropical decorrelation fails the fine-coreg windows over forest
    and ocean; mergebursts fills those pixels with NaN and snaphu aborts on
    ANY NaN ("NaN or infinity found in input float data"). Zero phase +
    zero coherence excludes them: snaphu skips zero-magnitude pixels and
    its MST isolates the masked regions into noise connected components,
    while every COHERENT pixel (roads, buildings, bare quarry ground)
    keeps its real measurement — the standard over-vegetation pattern.
    """
    import numpy as np

    merged = pair_dir / "merged"
    flat = merged / "filt_topophase.flat"
    width, length = _image_dims(Path(f"{flat}.xml"))
    a = np.fromfile(flat, dtype=np.complex64)
    a = a[: width * length].reshape(length, width)
    bad = ~(np.isfinite(a.real) & np.isfinite(a.imag))
    n = int(bad.sum())
    if n:
        a[bad] = 0
        a.tofile(flat)
    cor = merged / "topophase.cor"
    c = np.fromfile(cor, dtype=np.float32)
    if c.size >= 2 * width * length:
        c = c[: 2 * width * length].reshape(2 * length, width)
        c[~np.isfinite(c)] = 0
        c[:length][bad] = 0
        c.tofile(cor)
    return n


def select_pair(scenes, *, index: int, min_days: float, max_days: float,
                chain: str | None):
    """Plan pairs and pick one by index — WITH the chain filter applied.

    The batch driver filters by chain but passed only --pair-index here;
    re-planning without the filter picked the descending pair again
    (measured 2026-10-19). One shared selection, one meaning for an index.
    """
    from tools.batch_insar import filter_chain

    pairs = filter_chain(
        plan_pairs(scenes, min_days=min_days, max_days=max_days), chain
    )
    if not pairs:
        raise SystemExit("no interferometric pairs in the catalog window")
    return pairs[index % len(pairs)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default="data/projects/cdp-rio-general")
    parser.add_argument("--config-dir", default="config/projects")
    # persistent by default: /tmp is wiped on reboot and the pair download
    # is 14 GB (survived the 2026-10-12 restart under data/spike/)
    parser.add_argument("--workdir", default="data/spike/spike-insar")
    parser.add_argument(
        "--pair-index", type=int, default=-1,
        help="which planned pair to process (default -1 = most recent, "
        "i.e. post-works: the active quarry's bare ground correlates far "
        "better than January forest)",
    )
    parser.add_argument(
        "--stop-after", default="geocode", choices=["filter", "geocode"],
        help="run topsApp only up to this stage: 'filter' skips the "
        "full-swath unwrap->geocode stage (pathological over this "
        "tropically-decorrelated swath, >55 min without output, and the "
        "batch chain unwraps an AOI crop instead — tools/unwrap_crop); "
        "'geocode' keeps the original spike measurement behavior",
    )
    parser.add_argument(
        "--min-days", type=float, default=11.0,
        help="pair temporal-baseline window (must match the batch driver's "
        "planning so --pair-index selects the same pair; 23 for SBAS "
        "triangle closers)",
    )
    parser.add_argument(
        "--max-days", type=float, default=13.0,
    )
    parser.add_argument(
        "--chain", default=None,
        help="restrict planning to one chain (must match the batch "
        "driver's --chain so --pair-index selects the same pair)",
    )
    parser.add_argument(
        "--slc-cache", default=None,
        help="shared SLC zip cache: pair-dir zips become hardlinks, so "
        "chained pairs (…,B),(B,…) download B once (tools/batch_insar)",
    )
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
    pairs = [
        select_pair(
            scenes, index=args.pair_index,
            min_days=args.min_days, max_days=args.max_days,
            chain=args.chain,
        )
    ]
    print(f"planned {len(pairs)} pair(s); first: {pairs[0].id}")

    # 2. download the SLCs from CDSE
    auth = auth_from_env(storage)
    if not auth.enabled:
        raise SystemExit("CDSE_CLIENT_ID/CDSE_CLIENT_SECRET not set in .env")
    downloader = (
        make_slc_downloader(
            auth,
            cache_dir=Path(args.slc_cache).resolve(),
            budget_bytes=config.cache.slc_budget_bytes,
        )
        if args.slc_cache
        else make_slc_downloader(auth)
    )

    topsapp = find_topsapp()
    if topsapp is None:
        raise SystemExit("topsApp.py not found — isce2 not installed?")

    results = []
    for i, pair in enumerate(pairs, 1):
        # pair-specific dir keyed by dates + track; legacy date-only
        # dirs are reused only when their runconfig matches this pass
        from tools.batch_insar import pair_dir_for

        pair_dir = pair_dir_for(pair, work)
        pair_dir.mkdir(exist_ok=True)
        # reuse an earlier run's download (14 GB takes ~1 h; never redo it)
        from mirarsetena.coherence.download import is_valid_zip

        existing = [
            z for z in sorted(pair_dir.glob("*.zip")) if is_valid_zip(z)
        ]
        invalid = [z for z in pair_dir.glob("*.zip") if z not in existing]
        for z in invalid:
            print(f"  dropping corrupt zip {z.name}")
            z.unlink(missing_ok=True)
        if len(existing) >= 2:
            paths = existing
            dl_bytes = sum(p.stat().st_size for p in paths)
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

        print("  running topsApp preprocess->filter (the spike measurement) ...")
        t1 = time.time()
        proc = subprocess.run(
            [sys.executable, topsapp, str(runconfig),
             "--steps", "--start=preprocess", "--end=filter"],
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

        masked = _mask_ifg_nan(pair_dir)
        print(f"  masked {masked} NaN ifg pixels (zero phase + zero coherence)")

        if args.stop_after == "filter":
            # Batch chain (tools/batch_insar): merged/ is complete after
            # filter, the full-swath unwrap here is pathological over this
            # swath, and the chain unwraps an AOI crop instead.
            print("  stopping after filter (--stop-after filter)")
        else:
            print("  running topsApp unwrap->geocode ...")
            t2 = time.time()
            proc2 = subprocess.run(
                [sys.executable, topsapp, str(runconfig),
                 "--steps", "--start=unwrap", "--end=geocode"],
                cwd=str(pair_dir), capture_output=True, text=True, check=False,
            )
            runtime += time.time() - t2
            peak_rss_mb = max(
                peak_rss_mb, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
            )
            if proc2.returncode != 0:
                print(f"  topsApp FAILED after {runtime:.0f}s (rc={proc2.returncode})")
                print(proc2.stderr[-2000:])
                results.append(
                    {"pair": pair.id, "ok": False, "runtime_s": runtime,
                     "masked": masked, "stderr_tail": proc2.stderr[-500:]}
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
                "masked": masked,
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
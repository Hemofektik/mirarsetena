"""Batch InSAR history builder: previous pairs → servable products (L.6).

The 2026-10-03 spike proved the local chain for ONE pair. This drives it
for the pairs BEFORE it, so the scrubber's InSAR dates carry real history
instead of blank tiles:

    spike_insar (download SLCs + topsApp up to filter, NaN-mask)
      → unwrap_crop (AOI-crop snaphu, seconds)
      → insar_product (geocode @10 m + reference + store + POI series)
      → deploy (clear that date's tiles, copy product+series into the
        container's store) → purge (free the 28 GB SAFEs + heavy ISCE
        intermediates; merged/ + unwrap2/ stay for re-derivation)

Resumable after a restart (the user's PC may shut down mid-run): a pair
whose product AND series exist is skipped, and re-downloads are reused
from pair_dir zips. Local-only data prep (SCOPE/INSAR-PLAN decision) —
not run in CI: it needs CDSE credentials, the isce2 conda env and hours.

Usage (from the repo root, inside the isce2 env):
    python -m tools.batch_insar --pair-indexes -5,-4,-3,-2
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# topsApp leftovers that the product build never reads (the SAFEs are
# 14 GB zips + 14 GB unpacks; fine_* coreg stacks are the next biggest).
# merged/ (flat, phsig, lat/lon rdr) and unwrap2/ (crop.unw) are the
# rebuild inputs and are NEVER purged.
HEAVY_PAIR_DIRS = (
    "coarse_coreg", "coarse_interferogram", "coarse_offsets",
    "fine_coreg", "fine_interferogram", "fine_offsets",
    "ESD", "geom_reference", "reference", "secondary", "PICKLE",
    "unwrap",  # topsApp's full-swath attempt; the chain uses unwrap2/
)
KEEP_PAIR_DIRS = ("merged", "unwrap2")


def resolve_pairs(search_fn, bbox, start, end):
    """Planned pairs in the SAME catalog window the spike indexes against
    (a mismatch would make --pair-index select a different pair)."""
    from mirarsetena.coherence.pairs import plan_pairs

    scenes = search_fn("sentinel-1-grd", bbox, start, end)
    return plan_pairs(scenes)


def pair_is_done(store, slug: str, date: str) -> bool:
    """Resume gate: BOTH the product and its POI series are stored."""
    from mirarsetena.pipeline.indices import layer_key
    from mirarsetena.pipeline.insar import insar_series_key

    return bool(
        store.exists(layer_key(slug, "sentinel-1-slc", date, "insar"))
        and store.exists(insar_series_key(slug, date))
    )


def purge_candidates(pair_dir: Path) -> list[Path]:
    """Pair-dir entries safe to delete after the product is deployed."""
    out: list[Path] = []
    for child in sorted(pair_dir.iterdir()):
        if child.name in KEEP_PAIR_DIRS:
            continue
        if child.name.endswith(".unpack") or child.suffix == ".zip":
            out.append(child)
            continue
        if child.is_dir() and child.name in HEAVY_PAIR_DIRS:
            out.append(child)
    return out


def _run(cmd: list[str], *, cwd: Path | None = None, label: str) -> None:
    print(f"  $ {' '.join(cmd)}", flush=True)
    t0 = time.time()
    proc = subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, check=False
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-3000:]
        raise SystemExit(
            f"{label} FAILED (rc={proc.returncode}) after "
            f"{time.time() - t0:.0f}s\n{tail}"
        )
    print(f"    ok in {time.time() - t0:.0f}s", flush=True)


def deploy_commands(
    date: str, slug: str, e2e_root: Path, container: str
) -> list[list[str]]:
    """docker argv list for one pair: clear that date's stale tiles, then
    copy the built product + series into the LIVE store. Pure — pinned by
    test because a wrong destination silently undid a whole pair's deploy
    (2026-10-14: '/app/data/p/...' instead of '/app/data/cache/p/...')."""
    from mirarsetena.pipeline.indices import layer_key
    from mirarsetena.pipeline.insar import insar_series_key

    prefix = f"/app/data/cache/p/{slug}"
    out = [
        ["docker", "exec", container, "rm", "-rf",
         f"{prefix}/tiles/insar/{date}"],
    ]
    for key in (
        layer_key(slug, "sentinel-1-slc", date, "insar"),
        insar_series_key(slug, date),
    ):
        out.append(
            ["docker", "cp", str(e2e_root / key),
             # storage root is /app/data/cache — NOT /app/data (the path
             # typo that failed the first 08-04 deploy)
             f"{container}:/app/data/cache/{key}"]
        )
    return out


def deploy(date: str, slug: str, e2e_root: Path, container: str) -> None:
    """Move the built product+series into the LIVE store and drop that
    date's stale tiles (the tile cache key carries no product identity)."""
    for cmd in deploy_commands(date, slug, e2e_root, container):
        _run(cmd, label=f"deploy {cmd[-1].rsplit('/', 1)[-1]}")
    # host-side tiles are the SAME files (./data is bind-mounted), but the
    # stale e2e store copy must go too — its e2e renders read it directly
    host_tiles = e2e_root / f"p/{slug}/tiles/insar/{date}"
    if host_tiles.is_dir():
        shutil.rmtree(host_tiles)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-indexes", default="-5,-4,-3,-2",
                        help="comma list of plan_pairs indexes (negative = "
                        "from the newest; default: the four before the "
                        "already-built 10-03 spike product)")
    parser.add_argument("--config-dir", default="config/projects")
    parser.add_argument("--workdir", default="data/spike/spike-insar")
    parser.add_argument(
        "--catalog-end", default="2026-10-12",
        help="must match spike_insar's window so indexes select the same pairs",
    )
    parser.add_argument("--container", default="mirarsetena-api-1")
    parser.add_argument(
        "--slc-cache", default="data/spike/slc-cache",
        help="shared SLC zip cache across pairs (hardlinks): the four "
        "default pairs share 5 scenes — without it, 4 are re-downloaded "
        "(~28 GB). Pass '' to disable.",
    )
    parser.add_argument("--keep-downloads", action="store_true",
                        help="do not purge SAFEs/intermediates (disk-heavy)")
    args = parser.parse_args()

    from mirarsetena.pipeline.catalog import search
    from mirarsetena.projects.registry import ProjectRegistry
    from mirarsetena.storage import LocalStore

    registry = ProjectRegistry(args.config_dir)
    slug = "cdp-rio-general"
    config = registry.get(slug)
    bbox = tuple(config.bbox)
    work = Path(args.workdir).resolve()
    # deploy source: the rsc-owned e2e store (data/cache's layers/ is
    # root-owned on the host; the container copy gets it via docker cp)
    e2e_root = REPO / "data" / "cache-e2e"
    live_store = LocalStore(REPO / "data" / "cache")

    pairs = resolve_pairs(
        search, bbox, config.timeline.start.isoformat(), args.catalog_end
    )
    if not pairs:
        raise SystemExit("no interferometric pairs in the catalog window")
    indexes = [int(x) for x in args.pair_indexes.split(",") if x.strip()]

    report_path = work / "batch-report.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else []

    for idx in indexes:
        pair = pairs[idx % len(pairs)]
        date = pair.second.date
        print(f"\n=== pair[{idx}] {pair.first.date} -> {date} ===", flush=True)

        if pair_is_done(live_store, slug, date):
            print("  already deployed (product+series) — skipping", flush=True)
            continue
        if pair_is_done(LocalStore(e2e_root), slug, date):
            # built but the deploy failed (e.g. the 2026-10-14 path bug):
            # reuse it instead of re-running an hour of topsApp
            print("  built (e2e store), not deployed — deploying only",
                  flush=True)
            deploy(date, slug, e2e_root, args.container)
            continue

        _run(
            [sys.executable, "-m", "tools.spike_insar",
             "--pair-index", str(idx), "--stop-after", "filter",
             "--workdir", str(work),
             *(["--slc-cache", str(Path(args.slc_cache).resolve())]
               if args.slc_cache else [])],
            cwd=REPO, label=f"spike {date}",
        )
        pair_dir = work / date
        if not (pair_dir / "merged" / "filt_topophase.flat").is_file():
            raise SystemExit(f"spike produced no merged/ for {date}")

        _run(
            [sys.executable, "-m", "tools.unwrap_crop",
             "--merged", str(pair_dir / "merged"),
             f"--bbox={','.join(str(c) for c in bbox)}",
             "--workdir", str(pair_dir / "unwrap2")],
            cwd=REPO, label=f"unwrap {date}",
        )
        unw = pair_dir / "unwrap2" / "crop.unw"
        if not unw.is_file():
            raise SystemExit(f"unwrap produced no crop.unw for {date}")

        official = config.official_path or ""
        _run(
            [sys.executable, "-m", "tools.insar_product",
             "--merged", str(pair_dir / "merged"),
             "--unw", str(unw),
             f"--bbox={','.join(str(c) for c in bbox)}",
             *(["--official", official] if official else []),
             "--slug", slug, "--date", date,
             "--storage-root", str(e2e_root)],
            cwd=REPO, label=f"product {date}",
        )

        deploy(date, slug, e2e_root, args.container)

        if not args.keep_downloads:
            freed = 0
            for path in purge_candidates(pair_dir):
                freed += sum(
                    f.stat().st_size for f in path.rglob("*") if f.is_file()
                ) if path.is_dir() else path.stat().st_size
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() \
                    else path.unlink(missing_ok=True)
            print(f"  purged {freed / 1e9:.1f} GB", flush=True)

        report.append({"pair": pair.id, "date": date, "ok": True,
                       "finished_at": time.time()})
        report_path.write_text(json.dumps(report, indent=2))

    print(f"\nbatch done; report: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure and improve the SBAS network's data quality (L.7 follow-up).

Three steps on ONE chain, all from the stored edges:
1. closure stats BEFORE — per-triangle |d(i,k) - d(i,j) - d(j,k)|: the
   network's inconsistency (per-edge atmosphere + reference drift);
2. consistent re-referencing — every edge re-zeroed on ONE fixed pixel
   set (outside the official perimeter, valid in ALL edges), removing
   the mutually-inconsistent per-pair reference medians;
3. re-inversion + closure stats AFTER — the improvement is the metric.

Publishes the improved cumulative products/series (same keys as
sbas_run: servable insar_cum layer + sbas/series-*.json) and a quality
report (sbas/quality-<chain>.json). Run AFTER tools/sbas_run.

Usage (isce2 env, repo root):
    python -m tools.sbas_quality --chain S1D-asc
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]


def _official_mask(shape: tuple[int, int], official: Path, like_bytes: bytes):
    """True OUTSIDE the official perimeter on the product grid."""
    import rasterio
    from rasterio.features import geometry_mask

    with rasterio.io.MemoryFile(like_bytes) as mem, mem.open() as src:
        transform, crs = src.transform, src.crs
    gj = json.loads(official.read_text(encoding="utf-8"))
    geoms = [f["geometry"] for f in gj.get("features", [])]
    # geometries are EPSG:4326, same as the product grid — this rasterio
    # generation's geometry_mask takes only out_shape/transform
    inside = geometry_mask(
        geoms, out_shape=shape, transform=transform, invert=False
    )
    return ~inside


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", required=True)
    parser.add_argument("--config-dir", default="config/projects")
    parser.add_argument("--storage-root", default="data/cache-e2e")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO))
    from mirarsetena.coherence.sbas import closure_stats, rereference_edges, solve_sbas
    from mirarsetena.pipeline.indices import layer_key
    from mirarsetena.pipeline.insar import sample_pois
    from mirarsetena.projects.registry import ProjectRegistry
    from mirarsetena.storage import LocalStore
    from tools.sbas_run import CHAINS, _load_edge, cumulative_tif_bytes

    slug = "cdp-rio-general"
    registry = ProjectRegistry(args.config_dir)
    config = registry.get(slug)
    bbox = tuple(config.bbox)
    official = REPO / config.official_path if config.official_path else None
    storage_root = Path(args.storage_root)
    store = LocalStore(storage_root)
    pair_root = REPO / "data" / "spike" / "spike-insar"
    spec = CHAINS[args.chain]

    like_key = layer_key(slug, "sentinel-1-slc", spec["edges"][0][2], "insar")
    like_bytes = store.get(like_key)
    if like_bytes is None:
        raise SystemExit(f"missing reference product {like_key}")

    print(f"loading {len(spec['edges'])} edges of {args.chain} ...", flush=True)
    edges = []
    for first, second, served in spec["edges"]:
        delta = _load_edge(
            store, slug, first, second, served, pair_root, bbox,
            official, storage_root,
        )
        edges.append((first, second, delta))

    before = closure_stats(edges)
    med_before = float(np.median([s["median_abs"] for s in before])) if before else None
    print("\nclosures BEFORE (median |closure| cm):")
    for s in before:
        print(f"  {s['triangle']}: {s['median_abs']:.3f} (p90 {s['p90_abs']:.3f}, n={s['n']})")

    # one fixed reference set: outside the official perimeter AND valid
    # in EVERY edge
    ref_valid = np.ones(edges[0][2].shape, dtype=bool)
    for _, _, d in edges:
        ref_valid &= np.isfinite(d)
    ref_mask = ref_valid & _official_mask(edges[0][2].shape, official, like_bytes)
    print(f"\ncommon reference pixels: {int(ref_mask.sum())}")

    arrays = [d for _, _, d in edges]
    rereferenced, offsets = rereference_edges(arrays, ref_mask)
    redged = [
        (first, second, rd) for (first, second, _), rd in zip(edges, rereferenced)
    ]
    after = closure_stats(redged)
    med_after = float(np.median([s["median_abs"] for s in after])) if after else None
    print("closures AFTER consistent referencing:")
    for s in after:
        print(f"  {s['triangle']}: {s['median_abs']:.3f} (p90 {s['p90_abs']:.3f})")
    print(f"\nmedian |closure|: {med_before:.3f} -> {med_after:.3f} cm"
          if med_before is not None and med_after is not None else "")

    # re-invert and publish
    dates, fields, valid = solve_sbas(spec["dates"], redged)
    print(f"\ninverted {int(valid.sum())}/{valid.size} px; publishing ...")

    for t, date in enumerate(dates):
        blob = cumulative_tif_bytes(fields[t], like_bytes)
        store.put(f"p/{slug}/sbas/cum/{date}.tif", blob)
        store.put(layer_key(slug, "sentinel-1-slc", date, "insar_cum"), blob)

    pois = registry.load_pois(slug)
    series = {"chain": args.chain, "method": "sbas", "dates": dates, "pois": []}
    for poi in pois:
        values = []
        for t, date in enumerate(dates):
            blob = store.get(f"p/{slug}/sbas/cum/{date}.tif")
            values.append(sample_pois(blob, [poi])[0]["los_cm"])
        series["pois"].append({"id": poi["id"], "los_cm": values})
    store.put(
        f"p/{slug}/sbas/series-{args.chain}.json",
        json.dumps(series, ensure_ascii=False).encode(),
    )
    report = {
        "chain": args.chain,
        "median_abs_closure_before_cm": med_before,
        "median_abs_closure_after_cm": med_after,
        "reference_pixels": int(ref_mask.sum()),
        "edge_offsets_cm": offsets,
        "triangles_before": before,
        "triangles_after": after,
    }
    store.put(
        f"p/{slug}/sbas/quality-{args.chain}.json",
        json.dumps(report, indent=2).encode(),
    )
    print("quarry trajectory after:")
    q = next(p for p in series["pois"] if p["id"] == "quarry")
    print("  " + "  ".join(f"{v:+.2f}" if v is not None else " -- " for v in q["los_cm"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

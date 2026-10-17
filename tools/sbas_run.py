"""Run the in-repo SBAS inversion over one chain (INSAR-PLAN L.6a).

Assembles the chain's network from what exists on disk — the served
12-day products (layers/) and the 24-day closers (sbas/edges/, built on
demand from the kept merged/ + unwrap2/ inputs) — inverts per pixel with
solve_sbas, and stores:
  p/{slug}/sbas/cum/{date}.tif          per-date cumulative LOS (cm)
  p/{slug}/sbas/series.json             per-POI cumulative series

Usage (isce2 env, repo root):
    python -m tools.sbas_run --chain S1D-asc

Not in CI: it reads the on-disk products.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio

REPO = Path(__file__).resolve().parents[1]

# The one chain with triangles today: 5 consecutive 12-day pairs + the
# 4 24-day closers over nodes 07-11..09-09 (S1D ascending).
CHAINS: dict[str, dict] = {
    "S1D-asc": {
        "dates": [
            "2026-07-11", "2026-07-23", "2026-08-04",
            "2026-08-16", "2026-08-28", "2026-09-09",
        ],
        # (first, second): served-product date or closer pair dir
        "edges": [
            ("2026-07-11", "2026-07-23", "2026-07-23"),
            ("2026-07-23", "2026-08-04", "2026-08-04"),
            ("2026-08-04", "2026-08-16", "2026-08-16"),
            ("2026-08-16", "2026-08-28", "2026-08-28"),
            ("2026-08-28", "2026-09-09", "2026-09-09"),
            ("2026-07-11", "2026-08-04", None),   # 24-day closer
            ("2026-07-23", "2026-08-16", None),
            ("2026-08-04", "2026-08-28", None),
            ("2026-08-16", "2026-09-09", None),
        ],
    },
}


def _load_edge(store, slug: str, first: str, second: str, served: str | None,
               pair_root: Path, bbox, official: Path | None,
               storage_root: Path) -> np.ndarray:
    """Edge delta as a float64 array (NODATA -> NaN), building a closer
    on demand from its kept raw inputs."""
    from mirarsetena.pipeline.indices import NODATA, layer_key
    from mirarsetena.pipeline.insar import edge_key

    if served is not None:
        blob = store.get(layer_key(slug, "sentinel-1-slc", served, "insar"))
        if blob is None:
            raise SystemExit(f"missing served product for {first}->{second}")
    else:
        key = edge_key(slug, first, second)
        blob = store.get(key)
        if blob is None:
            pair_dir = pair_root / f"{first}_{second}"
            print(f"  building closer edge {first}->{second} ...", flush=True)
            from tools.insar_product import build_and_store

            build_and_store(
                merged=pair_dir / "merged",
                unw=pair_dir / "unwrap2" / "crop.unw",
                bbox=bbox,
                official=official,
                slug=slug,
                date=first,
                storage_root=storage_root,
                storage_key=key,
                write_series=False,
            )
            blob = store.get(key)
    with rasterio.io.MemoryFile(blob) as mem, mem.open() as src:
        data = src.read(1).astype(np.float64)
    data[data == NODATA] = np.nan
    return data


def cumulative_tif_bytes(fields_t: np.ndarray, like_bytes: bytes) -> bytes:
    """Encode one per-date cumulative field as a product-style GeoTIFF."""
    from rasterio.io import MemoryFile

    from mirarsetena.pipeline.indices import NODATA

    with MemoryFile(like_bytes) as mem, mem.open() as src:
        profile = src.profile.copy()
    profile.update(dtype="float32", count=1, nodata=NODATA, compress="deflate")
    out = np.where(np.isnan(fields_t), NODATA, fields_t).astype(np.float32)
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(out, 1)
        return mem.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", required=True, choices=sorted(CHAINS))
    parser.add_argument("--config-dir", default="config/projects")
    parser.add_argument("--storage-root", default="data/cache-e2e")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO))
    from mirarsetena.coherence.sbas import solve_sbas
    from mirarsetena.pipeline.indices import layer_key
    from mirarsetena.pipeline.insar import sample_pois
    from mirarsetena.projects.registry import ProjectRegistry
    from mirarsetena.storage import LocalStore

    slug = "cdp-rio-general"
    registry = ProjectRegistry(args.config_dir)
    config = registry.get(slug)
    bbox = tuple(config.bbox)
    official = (
        REPO / config.official_path if config.official_path else None
    )
    storage_root = Path(args.storage_root)
    store = LocalStore(storage_root)
    pair_root = REPO / "data" / "spike" / "spike-insar"
    spec = CHAINS[args.chain]

    like_key = layer_key(slug, "sentinel-1-slc", spec["edges"][0][2], "insar")
    like_bytes = store.get(like_key)
    if like_bytes is None:
        raise SystemExit(f"missing reference product {like_key}")

    print(f"assembling {args.chain}: {len(spec['edges'])} edges ...", flush=True)
    edges = []
    for first, second, served in spec["edges"]:
        delta = _load_edge(
            store, slug, first, second, served, pair_root, bbox,
            official, storage_root,
        )
        edges.append((first, second, delta))
        print(f"  {first}->{second}: {int(np.isfinite(delta).sum())} valid px",
              flush=True)

    print("inverting (per-pixel weighted LSQ) ...", flush=True)
    dates, fields, valid = solve_sbas(spec["dates"], edges)
    print(f"  valid pixels: {int(valid.sum())}/{valid.size}")

    # per-date cumulative products: internal sbas/ path AND the
    # SERVABLE insar_cum layer key (the map tiles read the latter)
    from mirarsetena.pipeline.indices import layer_key

    pois_registry = registry.load_pois(slug)
    cum_prefix = f"p/{slug}/sbas/cum"
    for t, date in enumerate(dates):
        blob = cumulative_tif_bytes(fields[t], like_bytes)
        store.put(f"{cum_prefix}/{date}.tif", blob)
        servable = layer_key(slug, "sentinel-1-slc", date, "insar_cum")
        store.put(servable, blob)
        # NO per-date series write: it would clobber the per-pair series
        # at insar_series_key (same key!) — the legend ramp is computed
        # from the product itself, and the cumulative POI series lives in
        # sbas/series-<chain>.json

    # per-POI cumulative series
    pois = pois_registry
    series = {"chain": args.chain, "method": "sbas", "dates": dates, "pois": []}
    for poi in pois:
        values = []
        for t, date in enumerate(dates):
            blob = store.get(f"{cum_prefix}/{date}.tif")
            got = sample_pois(blob, [poi])[0]["los_cm"]
            values.append(got)
        series["pois"].append({"id": poi["id"], "los_cm": values})
    series_key = f"p/{slug}/sbas/series-{args.chain}.json"
    store.put(series_key, json.dumps(series, ensure_ascii=False).encode())

    print(f"\ncumulative series ({args.chain}, relative to {dates[0]}):")
    print("  poi          " + "  ".join(d[5:] for d in dates))
    for entry in series["pois"]:
        cells = "  ".join(
            "  -- " if v is None else f"{v:+6.2f}" for v in entry["los_cm"]
        )
        print(f"  {entry['id']:<12} {cells}")
    print(f"\nstored: {cum_prefix}/<date>.tif, {series_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

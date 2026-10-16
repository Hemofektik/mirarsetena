"""Build + store the servable InSAR product from an existing crop unwrap.

Local data-prep step (INSAR-PLAN L.1 fast path): takes the merged ISCE
products (`filt_topophase.flat`, `phsig.cor`, `lat/lon.rdr.full.vrt`) plus a
completed AOI-crop unwrap (`tools/unwrap_crop`), geocodes the crop to the
project product grid, applies the height delta, re-zeroes against the
official perimeter, and stores the result at the SERVABLE layer key the tile
service reads — so the map stops 404ing on `insar` tiles.

Usage (conda env with rasterio, from the repo root):
    python -m tools.insar_product \
        --merged data/spike/spike-insar/2026-10-03/merged \
        --unw data/spike/spike-insar/2026-10-03/unwrap2/crop.unw \
        --bbox=-83.675,9.378,-83.660,9.397 \
        --official data/projects/cdp-rio-general/official.geojson \
        --slug cdp-rio-general --date 2026-10-03 --storage-root data/cache

Not run in CI: it needs the spike's on-disk products.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio

from mirarsetena.coherence.crop_chain import build_crop_product, reference_mask_outside
from mirarsetena.coherence.isce_products import (
    radar_window,
    read_isce_image,
    read_snaphu_unw,
)
from mirarsetena.pipeline.indices import NODATA, layer_key
from mirarsetena.pipeline.insar import write_insar_series


def load_pois(slug: str, config_dir: Path | None = None) -> list[dict]:
    """POI sample points for the plan-vs-actual series (L.9).

    Thin delegate to ProjectRegistry.load_pois — the tool keeps a one-line
    entry point; the contract lives in the service layer (which ships in
    the container, unlike this tools/ package).
    """
    from mirarsetena.projects.registry import ProjectRegistry

    config_dir = Path(config_dir) if config_dir else Path("config/projects")
    return ProjectRegistry(config_dir).load_pois(slug)


def build_and_store(
    *,
    merged: Path,
    unw: Path,
    bbox: tuple[float, float, float, float],
    official: Path | None,
    slug: str,
    date: str,
    storage_root: Path,
    # ~10 m/px: matches the S2 optical display grid (user 2026-10-14: the
    # old 0.0003° (33 m) default looked "much more coarse than the raw
    # Sentinel-1 data", whose source grid measures 2.9 x 13.9 m/px)
    resolution: float = 0.00009,
    chain: str | None = None,
) -> str:
    """Build the product and store it at the servable key; returns the key."""
    from mirarsetena.storage import LocalStore

    flat = read_isce_image(merged / "filt_topophase.flat")
    length, width = flat.shape
    r0, r1, c0, c1 = radar_window(merged, bbox, width, length)
    ch, cw = r1 - r0, c1 - c0

    phase, amp = read_snaphu_unw(unw, (ch, cw))
    coherence = read_isce_image(merged / "phsig.cor")[r0:r1, c0:c1].astype(np.float32)
    # snaphu zeroes NaN input; keep the mask consistent with the unwrap
    coherence[amp <= 0] = 0.0

    with rasterio.open(merged / "lat.rdr.full.vrt") as src:
        latf = src.read(1)
    with rasterio.open(merged / "lon.rdr.full.vrt") as src:
        lonf = src.read(1)
    ri = (np.arange(length) * latf.shape[0] / length).astype(int).clip(0, latf.shape[0] - 1)
    ci = (np.arange(width) * lonf.shape[1] / width).astype(int).clip(0, lonf.shape[1] - 1)
    lat = latf[np.ix_(ri, ci)][r0:r1, c0:c1]
    lon = lonf[np.ix_(ri, ci)][r0:r1, c0:c1]

    product = build_crop_product(
        phase, coherence, lat, lon, bbox, resolution=resolution
    )

    polygons: list[dict] = []
    if official and official.exists():
        gj = json.loads(official.read_text(encoding="utf-8"))
        polygons = [f["geometry"] for f in gj.get("features", [])]
    reference = reference_mask_outside(product, polygons)
    if reference.any():
        product = build_crop_product(
            phase, coherence, lat, lon, bbox,
            resolution=resolution, reference=reference,
        )

    key = layer_key(slug, "sentinel-1-slc", date, "insar")
    store = LocalStore(storage_root)
    store.put(key, product)

    # L.9 plan-vs-actual: sample the POIs NOW so the API serves a
    # pre-computed series instead of re-opening rasters per request.
    series_key = write_insar_series(
        store, slug, date, product, load_pois(slug), chain=chain
    )

    with rasterio.io.MemoryFile(product) as mem, mem.open() as src:
        data = src.read(1)
    valid = data[data != NODATA]
    print(f"key: {key}")
    print(f"series: {series_key}")
    print(f"size: {len(product)} bytes  grid: {src.width}x{src.height}")
    print(f"valid px: {valid.size}/{data.size}")
    if valid.size:
        print(
            f"LOS cm: median={np.median(valid):+.2f} "
            f"p1={np.percentile(valid, 1):+.2f} "
            f"p99={np.percentile(valid, 99):+.2f} "
            f"p2p={valid.max() - valid.min():.2f} "
            f"sat@+-5={100 * np.mean((valid <= -5) | (valid >= 5)):.1f}%"
        )
    return key


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--merged", required=True, help="ISCE merged/ directory")
    p.add_argument("--unw", required=True, help="crop.unw from unwrap_crop")
    p.add_argument("--bbox", required=True, help="W,S,E,N in WGS84")
    p.add_argument("--official", default=None, help="official perimeter GeoJSON")
    p.add_argument("--slug", required=True)
    p.add_argument("--date", required=True, help="pair END date (YYYY-MM-DD)")
    p.add_argument("--storage-root", default="data/cache")
    p.add_argument("--resolution", type=float, default=0.00009)
    p.add_argument("--chain", default=None,
                   help="SBAS network id, e.g. S1A-asc (from chain_id)")
    args = p.parse_args()
    bbox = tuple(float(x) for x in args.bbox.split(","))
    build_and_store(
        merged=Path(args.merged), unw=Path(args.unw), bbox=bbox,
        official=Path(args.official) if args.official else None,
        slug=args.slug, date=args.date,
        storage_root=Path(args.storage_root), resolution=args.resolution,
        chain=args.chain,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
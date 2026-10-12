"""InSAR pair job: SLC pair → unwrapped LOS displacement product.

The injected processor produces the unwrapped-phase + coherence GeoTIFFs
(SNAP TopoPhaseRemoval + Goldstein + snaphu unwrap in production; a fake in
tests). This job applies the height delta — phase → LOS centimetres,
coherence-masked — via `build_los_product`, and stores the result at the
SERVABLE layer key the tile service reads, so the background job and the map
share one contract (INSAR-PLAN L.3).

Sources are purged in a finally block like every Phase H pair job
(compute-and-purge, SCOPE R4-Q4): SLC zips are GBs and must not persist.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from mirarsetena.coherence.pairs import InterferometricPair
from mirarsetena.coherence.pipeline import safe_name
from mirarsetena.pipeline.indices import layer_key
from mirarsetena.pipeline.insar import DEFAULT_COHERENCE_THRESHOLD, build_los_product
from mirarsetena.storage import Storage

Bbox = tuple[float, float, float, float]
Downloader = Callable[[InterferometricPair, Path], list[Path]]
# Production: SNAP+snaphu. Returns {"phase": GeoTIFF bytes, "coherence": bytes}.
Processor = Callable[[list[Path], Bbox], dict]


def insar_result_key(slug: str, pair: InterferometricPair) -> str:
    """Servable key: the pair's END date is the product date (matches
    `_list_insar_dates`, so the scrubber date selects this product)."""
    return layer_key(slug, "sentinel-1-slc", pair.second.date, "insar")


def run_insar_pair_job(
    storage: Storage,
    slug: str,
    pair: InterferometricPair,
    *,
    bbox: Bbox,
    downloader: Downloader,
    processor: Processor,
    workdir: str | Path,
    threshold: float = DEFAULT_COHERENCE_THRESHOLD,
) -> dict:
    """Download SLC pair, unwrap to LOS cm, cache product, purge sources."""
    pair_dir = Path(workdir) / safe_name(pair.id)
    try:
        pair_dir.mkdir(parents=True, exist_ok=True)
        paths = downloader(pair, pair_dir)
        downloaded_bytes = sum(path.stat().st_size for path in paths)
        bands = processor(paths, bbox)
        product = build_los_product(
            bands["phase"], bands["coherence"], threshold=threshold
        )
        key = insar_result_key(slug, pair)
        storage.put(key, product)
        return {"key": key, "downloaded_bytes": downloaded_bytes, "pair": pair.id}
    finally:
        shutil.rmtree(pair_dir, ignore_errors=True)

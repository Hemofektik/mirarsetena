"""Compute-and-purge coherence pipeline (SCOPE R4-Q4).

SLC scenes are large (~1-2 GB each): every pair job downloads, processes,
caches the small coherence result, and deletes the sources in a finally
block — disk stays bounded forever regardless of outcome.

Processor spike (IMPLEMENTATION H.3): find_processor() probes for an
installed interferometric processor (SNAP `gpt`, ISCE2). This host has
none installed, so the processor is injectable and jobs report
CoherenceUnavailable rather than fabricating results.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from mirarsetena.coherence.cdse import CoherenceUnavailable
from mirarsetena.coherence.pairs import InterferometricPair, pair_key
from mirarsetena.pipeline.catalog import Scene
from mirarsetena.projects.registry import cache_key
from mirarsetena.storage import Storage

Bbox = tuple[float, float, float, float]
Downloader = Callable[[InterferometricPair, Path], list[Path]]
Processor = Callable[[list[Path], Bbox], bytes]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str) -> str:
    return _UNSAFE.sub("_", value).strip("_")[:120]


def find_processor() -> Processor | None:
    """Locate an installed interferometric processor, if any (spike)."""
    if shutil.which("gpt") or shutil.which("isce2") or shutil.which("snaphu"):
        # Integration lands when the spike pins one (see IMPLEMENTATION H.3).
        return None
    return None


def scene_to_dict(scene: Scene) -> dict:
    return {
        "id": scene.id,
        "collection": scene.collection,
        "datetime": scene.datetime,
        "date": scene.date,
        "cloud": scene.cloud,
        "assets": dict(scene.assets),
        "platform": scene.platform,
        "orbit_state": scene.orbit_state,
        "relative_orbit": scene.relative_orbit,
    }


def scene_from_dict(data: dict) -> Scene:
    return Scene(
        id=str(data["id"]),
        collection=str(data["collection"]),
        datetime=str(data["datetime"]),
        date=str(data["date"]),
        cloud=data.get("cloud"),
        assets=dict(data.get("assets") or {}),
        platform=data.get("platform"),
        orbit_state=data.get("orbit_state"),
        relative_orbit=data.get("relative_orbit"),
    )


def pair_to_payload(pair: InterferometricPair) -> dict:
    return {"first": scene_to_dict(pair.first), "second": scene_to_dict(pair.second)}


def pair_from_payload(data: dict) -> InterferometricPair:
    return InterferometricPair(
        first=scene_from_dict(data["first"]),
        second=scene_from_dict(data["second"]),
    )


def result_key(slug: str, pair: InterferometricPair) -> str:
    return cache_key(slug, "coherence", f"{safe_name(pair.id)}.tif")


def run_pair_job(
    storage: Storage,
    slug: str,
    pair: InterferometricPair,
    *,
    bbox: Bbox,
    downloader: Downloader,
    processor: Processor,
    workdir: str | Path,
) -> dict:
    """Download SLC, compute coherence, cache result, purge sources."""
    pair_dir = Path(workdir) / safe_name(pair.id)
    try:
        pair_dir.mkdir(parents=True, exist_ok=True)
        paths = downloader(pair, pair_dir)
        downloaded_bytes = sum(path.stat().st_size for path in paths)
        payload = processor(paths, bbox)
        key = result_key(slug, pair)
        storage.put(key, payload)
        return {
            "key": key,
            "downloaded_bytes": downloaded_bytes,
            "pair": pair.id,
            "pair_key": pair_key(pair),
        }
    finally:
        shutil.rmtree(pair_dir, ignore_errors=True)


def make_pair_handler(
    storage: Storage,
    slug: str,
    bbox: Bbox,
    *,
    downloader: Downloader,
    processor: Processor | None = None,
    workdir: str | Path,
) -> Callable[[dict], None]:
    """Job handler for 'coherence_pair' payloads (needs an installed
    processor; raises CoherenceUnavailable otherwise). Idempotent: skips
    pairs whose result is already cached."""

    def handler(payload: dict) -> None:
        pair = pair_from_payload(payload["pair"])
        if storage.get(result_key(slug, pair)) is not None:
            return
        chosen = processor if processor is not None else find_processor()
        if chosen is None:
            raise CoherenceUnavailable(
                "no interferometric processor installed (SNAP gpt / ISCE2) — "
                "see IMPLEMENTATION H.3 spike note"
            )
        run_pair_job(
            storage,
            slug,
            pair,
            bbox=bbox,
            downloader=downloader,
            processor=chosen,
            workdir=workdir,
        )

    return handler


def make_eager_handler(
    storage: Storage,
    slug: str,
    bbox: Bbox,
    *,
    downloader: Downloader,
    processor: Processor | None = None,
    workdir: str | Path,
) -> Callable[[dict], None]:
    """Handler for the single 'coherence_eager' job: runs every pair the
    first view knew about, skipping pairs already computed."""

    def handler(payload: dict) -> None:
        chosen = processor if processor is not None else find_processor()
        if chosen is None and payload.get("pairs"):
            raise CoherenceUnavailable(
                "no interferometric processor installed (SNAP gpt / ISCE2) — "
                "see IMPLEMENTATION H.3 spike note"
            )
        for raw in payload.get("pairs", []):
            pair = pair_from_payload(raw)
            if storage.get(result_key(slug, pair)) is not None:
                continue
            run_pair_job(
                storage,
                slug,
                pair,
                bbox=bbox,
                downloader=downloader,
                processor=chosen,
                workdir=workdir,
            )

    return handler

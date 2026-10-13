"""Scheduled catalog polling: enqueue (idempotent) + drain, on an asyncio loop.

Runs inside the API process (SCOPE R2-Q5 hybrid freshness): every interval
the queue gets one catalog_poll job per project; the loop drains inline, so
a single process needs no worker daemon.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from mirarsetena.coherence.pairs import plan_pairs
from mirarsetena.pipeline.catalog import CatalogError
from mirarsetena.pipeline.dates import refresh_index

log = logging.getLogger("mirarsetena.poller")

MAX_DRAIN_PER_CYCLE = 50


def make_catalog_poll_handler(registry, storage, *, search_fn):
    def handler(payload: dict) -> None:
        config = registry.get(payload["slug"])
        refresh_index(storage, config, search_fn=search_fn)

    return handler


def _slc_downloader(storage, slug, *, registry):
    """Download an SLC pair from CDSE into the workdir (INSAR-PLAN L.2),
    through the scene cache when the store exposes a root directory.

    Chained pairs [(A,B), (B,C)] share scene B: without the cache B would
    be downloaded twice (~7 GB each). Disk stays LRU-bounded by the
    project's cache.slc_budget_bytes. Degrades to CoherenceUnavailable when
    CDSE credentials are absent — the job fails honestly (SCOPE R4-Q1).
    """
    from mirarsetena.coherence.cdse import CoherenceUnavailable, auth_from_env
    from mirarsetena.coherence.download import make_slc_downloader

    auth = auth_from_env(storage)
    if not auth.enabled:
        raise CoherenceUnavailable(
            "CDSE credentials not configured — set CDSE_CLIENT_ID/SECRET"
        )
    root = getattr(storage, "root", None)
    if root is None:
        return make_slc_downloader(auth)
    budget = registry.get(slug).cache.slc_budget_bytes
    return make_slc_downloader(
        auth,
        cache_dir=Path(root) / "slc" / slug,
        budget_bytes=budget,
    )


def insar_handlers(registry, storage, *, search_fn, processor=None):
    """Job handlers that drain the InSAR pair queue (INSAR-PLAN L.5).

    Gap #3 fix: these handlers were enqueued by the trigger but never
    registered, so pair jobs parked forever. `processor` is the SNAP+snaphu
    interferometric processor (injectable; None until the L.1 spike pins one),
    so without it the handlers raise CoherenceUnavailable and the job fails
    honestly rather than fabricating displacement.
    """
    from mirarsetena.coherence.cdse import CoherenceUnavailable
    from mirarsetena.coherence.insar import run_insar_pair_job
    from mirarsetena.coherence.pipeline import find_processor, pair_from_payload

    def _require_processor():
        chosen = processor if processor is not None else find_processor()
        if chosen is None:
            raise CoherenceUnavailable(
                "no interferometric processor installed (SNAP gpt / ISCE2) — "
                "see INSAR-PLAN L.1 spike"
            )
        return chosen

    def _bbox(slug):
        return tuple(registry.get(slug).bbox)

    def _workdir(slug):
        import tempfile

        return tempfile.mkdtemp(prefix=f"insar-{slug}-")

    def pair_handler(payload: dict) -> None:
        slug = payload["slug"]
        pair = pair_from_payload(payload["pair"])
        chosen = _require_processor()
        run_insar_pair_job(
            storage, slug, pair, bbox=_bbox(slug),
            downloader=_slc_downloader(storage, slug, registry=registry),
            processor=chosen, workdir=_workdir(slug),
        )

    def eager_handler(payload: dict) -> None:
        slug = payload["slug"]
        config = registry.get(slug)
        chosen = _require_processor()
        downloader = _slc_downloader(storage, slug, registry=registry)
        try:
            scenes = search_fn(
                "sentinel-1-grd", config.bbox,
                config.timeline.start.isoformat(),
                datetime.now(timezone.utc).date().isoformat(),
            )
        except CatalogError:
            scenes = []
        for pair in plan_pairs(scenes):
            run_insar_pair_job(
                storage, slug, pair, bbox=_bbox(slug),
                downloader=downloader,
                processor=chosen, workdir=_workdir(slug),
            )

    return {"insar_pair": pair_handler, "insar_eager": eager_handler}


def poll_once(registry, queue) -> int:
    """Enqueue one catalog_poll per registered project; returns new jobs."""
    enqueued = 0
    for slug in registry.all():
        if queue.enqueue(
            "catalog_poll", {"slug": slug}, dedupe=f"catalog_poll:{slug}"
        ):
            enqueued += 1
    return enqueued


def drain(queue, handlers, *, limit: int = MAX_DRAIN_PER_CYCLE) -> int:
    ran = 0
    while ran < limit and queue.run_once(handlers) is not None:
        ran += 1
    return ran


def _append_insar_pairs(app) -> None:
    """After the first view, each cycle appends one insar_pair job per
    not-yet-queued pair (INSAR-PLAN cadence: new SLC scenes publish every
    12 days; GRD metadata shares the passes, so pairs are planned from it).
    Before the first view nothing is appended — the eager job owns the backlog.
    """
    from mirarsetena.coherence.trigger import (
        enqueue_new_pairs,
        insar_viewed_flag_key,
    )

    registry = app.state.registry
    storage = app.state.storage
    queue = app.state.queue
    for slug in registry.all():
        if storage.get(insar_viewed_flag_key(slug)) is None:
            continue
        config = registry.get(slug)
        try:
            scenes = app.state.search_fn(
                "sentinel-1-grd", config.bbox,
                config.timeline.start.isoformat(),
                datetime.now(timezone.utc).date().isoformat(),
            )
        except CatalogError:
            continue
        enqueue_new_pairs(
            slug, plan_pairs(scenes), queue, job_type="insar_pair"
        )


def cycle(app) -> int:
    """One poll cycle: enqueue for all projects, then drain the queue."""
    registry = app.state.registry
    queue = app.state.queue
    handlers = {
        "catalog_poll": make_catalog_poll_handler(
            registry, app.state.storage, search_fn=app.state.search_fn
        )
    }
    # InSAR pair jobs (INSAR-PLAN L.5): registered so the queue drains them
    # instead of parking them forever (gap #3). Without a SNAP/ISCE2 processor
    # or CDSE creds they raise CoherenceUnavailable and fail honestly.
    handlers.update(
        insar_handlers(registry, app.state.storage, search_fn=app.state.search_fn)
    )
    poll_once(registry, queue)
    _append_insar_pairs(app)
    return drain(queue, handlers)


async def poller_loop(app, interval_seconds: int) -> None:
    while True:
        try:
            await asyncio.to_thread(cycle, app)
        except Exception:
            log.exception("catalog poll cycle failed")
        await asyncio.sleep(interval_seconds)

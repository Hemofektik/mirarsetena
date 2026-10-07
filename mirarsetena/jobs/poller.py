"""Scheduled catalog polling: enqueue (idempotent) + drain, on an asyncio loop.

Runs inside the API process (SCOPE R2-Q5 hybrid freshness): every interval
the queue gets one catalog_poll job per project; the loop drains inline, so
a single process needs no worker daemon.
"""
from __future__ import annotations

import asyncio
import logging

from mirarsetena.pipeline.dates import refresh_index

log = logging.getLogger("mirarsetena.poller")

MAX_DRAIN_PER_CYCLE = 50


def make_catalog_poll_handler(registry, storage, *, search_fn):
    def handler(payload: dict) -> None:
        config = registry.get(payload["slug"])
        refresh_index(storage, config, search_fn=search_fn)

    return handler


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


def cycle(app) -> int:
    """One poll cycle: enqueue for all projects, then drain the queue."""
    registry = app.state.registry
    queue = app.state.queue
    handlers = {
        "catalog_poll": make_catalog_poll_handler(
            registry, app.state.storage, search_fn=app.state.search_fn
        )
    }
    poll_once(registry, queue)
    return drain(queue, handlers)


async def poller_loop(app, interval_seconds: int) -> None:
    while True:
        try:
            await asyncio.to_thread(cycle, app)
        except Exception:
            log.exception("catalog poll cycle failed")
        await asyncio.sleep(interval_seconds)

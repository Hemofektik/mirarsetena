"""Project status: cache sizes, catalog freshness, jobs, per-mission ETA.

The missions section is pair-aware for InSAR (2026-10-14: the status page
read "sentinel-1-slc: 0 / last_scene: null" — pairs are derived from GRD
passes and never catalog-indexed, so the index-based counts were stale
lies). Jobs separate runnable 'pending' from 'parked' jobs of retired
types whose handlers no longer exist, and by_type explains failures.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from mirarsetena.jobs.eta import next_acquisition
from mirarsetena.pipeline.dates import index_key, mission_for_layer
from mirarsetena.projects.registry import ProjectConfig, cache_key
from mirarsetena.storage import Storage

_PREFIXES = ("scenes", "layers", "tiles", "catalog")


def status_payload(
    storage: Storage,
    config: ProjectConfig,
    queue: Any,
    *,
    now: datetime,
    search_fn: Any = None,
    runnable_types: tuple[str, ...] | None = None,
) -> dict:
    slug = config.slug

    by_prefix = {
        prefix: storage.total_size(cache_key(slug, prefix) + "/")
        for prefix in _PREFIXES
    }

    raw = storage.get(index_key(slug))
    index = json.loads(raw) if raw else {}

    missions: dict[str, dict] = {}
    for layer in config.layers:
        mission = mission_for_layer(layer)
        if mission in missions:
            continue
        entries = index.get("missions", {}).get(mission, [])
        last_scene = max((entry["date"] for entry in entries), default=None)
        repeat = (
            config.satellite.s2_repeat_days
            if mission == "sentinel-2-l2a"
            else config.satellite.s1_repeat_days
        )
        upcoming = next_acquisition(
            date.fromisoformat(last_scene) if last_scene else None,
            repeat,
            now.date(),
        )
        missions[mission] = {
            "last_scene": last_scene,
            "next_acquisition": upcoming.isoformat() if upcoming else None,
            "repeat_days": repeat,
        }

    if "sentinel-1-slc" in missions:
        slc = missions["sentinel-1-slc"]
        # products are storage truth (no network); the rest needs the
        # GRD passes the pairs are planned from
        prefix = cache_key(slug, "layers") + "/"
        slc["products_processed"] = sum(
            1
            for key in storage.list(prefix)
            # list() returns FULL keys: sentinel-1-slc_<date>_insar.tif
            # (the _series.json sidecars end differently on purpose)
            if key.rsplit("/", 1)[-1].startswith("sentinel-1-slc_")
            and key.endswith("_insar.tif")
        )
        slc["pairs_planned"] = None
        if search_fn is not None:
            from mirarsetena.coherence.pairs import plan_pairs
            from mirarsetena.pipeline.catalog import CatalogError

            try:
                scenes = search_fn(
                    "sentinel-1-grd",
                    config.bbox,
                    config.timeline.start.isoformat(),
                    now.date().isoformat(),
                )
            except CatalogError:
                scenes = []
            pairs = plan_pairs(scenes)
            slc["pairs_planned"] = len(pairs)
            last = max((pair.second.date for pair in pairs), default=None)
            if last:
                slc["last_scene"] = last
                upcoming = next_acquisition(
                    date.fromisoformat(last),
                    config.satellite.s1_repeat_days,
                    now.date(),
                )
                slc["next_acquisition"] = (
                    upcoming.isoformat() if upcoming else None
                )

    catalog_missions = {
        mission: len(entries)
        for mission, entries in index.get("missions", {}).items()
        # SLC is not catalog-searchable (pair-derived): reporting its
        # placeholder 0 here was the outdated number users noticed
        if mission != "sentinel-1-slc"
    }

    jobs: dict[str, Any] = dict(queue.counts())
    by_type = queue.breakdown() if hasattr(queue, "breakdown") else {}
    if runnable_types is not None:
        parked = sum(
            entry.get("pending", 0)
            for type_, entry in by_type.items()
            if type_ not in runnable_types
        )
        jobs["pending"] = jobs.get("pending", 0) - parked
        jobs["parked"] = parked
    jobs["by_type"] = by_type

    return {
        "project": slug,
        "generated_at": now.isoformat(),
        "cache": {"by_prefix": by_prefix, "total_bytes": sum(by_prefix.values())},
        "catalog": {
            "updated_at": index.get("updated_at"),
            "mission_counts": catalog_missions,
        },
        "jobs": jobs,
        "missions": missions,
    }

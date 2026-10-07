"""Project status: cache sizes, catalog freshness, jobs, per-mission ETA."""
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

    return {
        "project": slug,
        "generated_at": now.isoformat(),
        "cache": {"by_prefix": by_prefix, "total_bytes": sum(by_prefix.values())},
        "catalog": {
            "updated_at": index.get("updated_at"),
            "mission_counts": {
                mission: len(entries)
                for mission, entries in index.get("missions", {}).items()
            },
        },
        "jobs": queue.counts(),
        "missions": missions,
    }

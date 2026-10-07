"""Layer-driven date lists: which dates the scrubber offers, with cloud scores.

Mission is implied by the selected layer (SCOPE R4-Q2): S2 layers show S2
dates, radar layers show S1 dates. Availability comes from a local date
index refreshed at most every INDEX_TTL (SCOPE R2-Q5 hybrid freshness).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from mirarsetena.pipeline import catalog
from mirarsetena.pipeline.catalog import DailyScene, Scene, group_by_date, search
from mirarsetena.pipeline.window import scene_meta_key
from mirarsetena.projects.registry import ProjectConfig, cache_key
from mirarsetena.storage import Storage

INDEX_TTL = timedelta(hours=6)

S2_LAYERS = {"rgb", "ndvi", "mndwi", "bsi"}
S1_LAYERS = {"sigma0", "coherence"}

_MISSION_BY_LAYER = {
    **{layer: "sentinel-2-l2a" for layer in S2_LAYERS},
    "sigma0": "sentinel-1-grd",
    "coherence": "sentinel-1-slc",
}

SearchFn = Callable[..., list[Scene]]


class UnknownLayer(ValueError):
    """The requested layer is not part of the project layer set."""


def mission_for_layer(layer: str) -> str:
    try:
        return _MISSION_BY_LAYER[layer]
    except KeyError:
        raise UnknownLayer(f"unknown layer {layer!r}") from None


def index_key(slug: str) -> str:
    return cache_key(slug, "catalog", "dates.json")


def _catalog_cloud(daily: DailyScene) -> float | None:
    clouds = [s.cloud for s in daily.scenes if s.cloud is not None]
    if not clouds:
        return None
    return round(sum(clouds) / len(clouds), 2)


def _fresh(index: dict | None, now: datetime) -> bool:
    if not index:
        return False
    try:
        updated = datetime.fromisoformat(index["updated_at"])
    except (KeyError, ValueError):
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return now - updated < INDEX_TTL


def refresh_index(
    storage: Storage,
    config: ProjectConfig,
    *,
    search_fn: SearchFn = search,
    now: datetime | None = None,
) -> dict:
    """Query the catalog for every mission the project's layers need."""
    now = now or datetime.now(timezone.utc)
    missions: dict[str, list[dict]] = {}
    for layer in config.layers:
        mission = mission_for_layer(layer)
        if mission in missions:
            continue
        try:
            scenes = search_fn(
                mission, config.bbox,
                config.timeline.start.isoformat(), now.date().isoformat(),
            )
        except catalog.CatalogError:
            scenes = []
        missions[mission] = [
            {
                "date": daily.date,
                "cloud": _catalog_cloud(daily),
                "scenes": [scene.id for scene in daily.scenes],
            }
            for daily in group_by_date(scenes)
        ]
    index = {"updated_at": now.isoformat(), "missions": missions}
    storage.put(index_key(config.slug), json.dumps(index).encode("utf-8"))
    return index


def list_dates(
    storage: Storage,
    slug: str,
    config: ProjectConfig,
    layer: str,
    *,
    max_cloud: float | None = None,
    search_fn: SearchFn = search,
    now: datetime | None = None,
) -> dict:
    """Date list for one layer: mission-implied, cloud-filtered, timeline."""
    now = now or datetime.now(timezone.utc)
    mission = mission_for_layer(layer)

    raw = storage.get(index_key(slug))
    index = json.loads(raw) if raw else None
    if not _fresh(index, now):
        index = refresh_index(storage, config, search_fn=search_fn, now=now)

    entries = list(index["missions"].get(mission, []))

    # Prefer the AOI cloud score computed from SCL (D.3) over catalog values.
    enriched = []
    for entry in entries:
        cloud = entry.get("cloud")
        processed = False
        meta_raw = storage.get(
            scene_meta_key(slug, mission, entry["date"])
        )
        if meta_raw:
            meta = json.loads(meta_raw)
            if meta.get("cloud") is not None:
                cloud = round(float(meta["cloud"]), 2)
                processed = True
        enriched.append(
            {"date": entry["date"], "cloud": cloud, "processed": processed}
        )

    if max_cloud is not None:
        enriched = [
            entry
            for entry in enriched
            if entry["cloud"] is not None and entry["cloud"] <= max_cloud
        ]

    return {
        "layer": layer,
        "mission": mission,
        "dates": enriched,
        "timeline": {
            "start": config.timeline.start.isoformat(),
            "works_start": config.timeline.works_start.isoformat(),
        },
    }

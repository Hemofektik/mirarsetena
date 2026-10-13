"""Layer-driven date lists: which dates the scrubber offers, with cloud scores.

Mission is implied by the selected layer (SCOPE R4-Q2): S2 layers show S2
dates, radar layers show S1 dates. Availability comes from a local date
index refreshed at most every INDEX_TTL (SCOPE R2-Q5 hybrid freshness).
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from mirarsetena.pipeline import catalog
from mirarsetena.pipeline.catalog import DailyScene, Scene, group_by_date, search
from mirarsetena.pipeline.indices import layer_key
from mirarsetena.pipeline.window import scene_meta_key
from mirarsetena.projects.registry import ProjectConfig, cache_key
from mirarsetena.storage import Storage

INDEX_TTL = timedelta(hours=6)
INDEX_VERSION = 2  # v2 = per-date orbit geometry (viewing-direction matching)

S2_LAYERS = {"rgb", "ndvi", "mndwi", "bsi"}
S1_LAYERS = {"sigma0", "coherence"}
INSAR_LAYERS = {"insar"}

_MISSION_BY_LAYER = {
    **{layer: "sentinel-2-l2a" for layer in S2_LAYERS},
    "sigma0": "sentinel-1-grd",
    "coherence": "sentinel-1-slc",
    # InSAR LOS displacement is produced from SLC phase by the background
    # interferometry jobs (INSAR-PLAN L.3); no lazy path, jobs only.
    "insar": "sentinel-1-slc",
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
    # schema bump: indexes written before orbit geometry existed must
    # re-refresh instead of serving geometry-less entries for 6h
    if index.get("v") != INDEX_VERSION:
        return False
    try:
        updated = datetime.fromisoformat(index["updated_at"])
    except (KeyError, ValueError):
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return now - updated < INDEX_TTL


# Single-flight for the catalog refresh: fetchDates + the tile fan-out of one
# layer switch observe the same stale index in the same instant, and a
# refresh is three catalog searches — they must share exactly one round.
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


def _refresh_once(
    storage: Storage,
    config: ProjectConfig,
    *,
    search_fn: SearchFn,
    now: datetime,
) -> dict:
    with _refresh_lock(config.slug):
        stored = storage.get(index_key(config.slug))
        index = json.loads(stored) if stored else None
        if _fresh(index, now):
            return index  # a concurrent caller refreshed while we waited
        return refresh_index(storage, config, search_fn=search_fn, now=now)


def _refresh_lock(slug: str) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        return _REFRESH_LOCKS.setdefault(slug, threading.Lock())


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
        # sentinel-1-slc (the InSAR layer) is not catalog-searchable: its
        # dates are pair-derived in _list_insar_dates, so skip the wasted
        # search here (INSAR-PLAN L.5).
        if mission == "sentinel-1-slc":
            missions[mission] = []
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
                # viewing geometry per date: unique (state, relative orbit)
                # pairs of the scenes merged into this daily product
                "orbits": [
                    list(pair)
                    for pair in sorted(
                        {
                            (scene.orbit_state, scene.relative_orbit)
                            for scene in daily.scenes
                            if scene.orbit_state
                            or scene.relative_orbit is not None
                        },
                        key=lambda pair: (pair[0] or "", pair[1] or 0),
                    )
                ],
            }
            for daily in group_by_date(scenes)
        ]
    index = {
        "v": INDEX_VERSION,
        "updated_at": now.isoformat(),
        "missions": missions,
    }
    storage.put(index_key(config.slug), json.dumps(index).encode("utf-8"))
    return index


def _list_insar_dates(
    storage: Storage,
    slug: str,
    config: ProjectConfig,
    *,
    search_fn: SearchFn,
    now: datetime,
) -> dict:
    """InSAR dates = END dates of planned interferometric pairs.

    SLC is not catalog-searchable (Earth Search serves GRD only), but SLC
    acquisitions share the passes with GRD — so we plan pairs from the GRD
    metadata (INSAR-PLAN L.3). Each date's product is the unwrapped LOS
    displacement for that pair, produced by the background jobs.
    """
    from mirarsetena.coherence.pairs import plan_pairs

    try:
        scenes = search_fn(
            "sentinel-1-grd",
            config.bbox,
            config.timeline.start.isoformat(),
            now.date().isoformat(),
        )
    except catalog.CatalogError:
        scenes = []
    pairs = plan_pairs(scenes)

    # The end date of each pair is the product date; track the geometry so
    # the frontend can label ASC/DESC like the old radar scrubber did.
    by_end: dict[str, dict] = {}
    for pair in pairs:
        entry = by_end.setdefault(
            pair.second.date,
            {
                "date": pair.second.date,
                "cloud": None,       # radar: no SCL, always cloudless
                "processed": False,  # becomes True when a job stores the product
                "orbits": [],
            },
        )
        geometry = (pair.second.orbit_state, pair.second.relative_orbit)
        if list(geometry) not in entry["orbits"]:
            entry["orbits"].append(list(geometry))

    # processed=True when the job already cached a product for this pair;
    # each processed entry carries its own adaptive ramp limit so the legend
    # matches the tile actually on screen (INSAR-PLAN decision 5 gate: the
    # fixed +-5 cm MVP ramp rendered the real product all-white).
    from mirarsetena.pipeline.insar import los_range

    limits = []
    for entry in by_end.values():
        product_key = layer_key(slug, "sentinel-1-slc", entry["date"], "insar")
        product = storage.get(product_key)
        entry["processed"] = product is not None
        entry["los_range"] = los_range(product) if product is not None else None
        if entry["los_range"] is not None:
            limits.append(entry["los_range"])

    dates = [by_end[key] for key in sorted(by_end)]
    return {
        "layer": "insar",
        "mission": "sentinel-1-slc",
        "dates": dates,
        "los_range": max(limits) if limits else None,
        "timeline": {
            "start": config.timeline.start.isoformat(),
            "works_start": config.timeline.works_start.isoformat(),
        },
    }


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

    if layer in INSAR_LAYERS:
        return _list_insar_dates(storage, slug, config, search_fn=search_fn, now=now)

    raw = storage.get(index_key(slug))
    index = json.loads(raw) if raw else None
    if not _fresh(index, now):
        index = _refresh_once(storage, config, search_fn=search_fn, now=now)

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
            {
                "date": entry["date"],
                "cloud": cloud,
                "processed": processed,
                "orbits": [
                    list(pair) for pair in entry.get("orbits", [])
                ],
            }
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

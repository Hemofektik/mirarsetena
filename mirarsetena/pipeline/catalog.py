"""Earth Search STAC access: search, parse, group scenes by acquisition date.

No account required (SCOPE R4-Q1 dual catalog: this drives Sentinel-2 and
Sentinel-1 GRD; Copernicus CDSE is only queried for SLC coherence).
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date as date_type

import httpx

EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"


class CatalogError(RuntimeError):
    """The satellite catalog could not be queried or understood."""


@dataclass(frozen=True)
class Scene:
    id: str
    collection: str
    datetime: str  # full ISO timestamp
    date: str  # YYYY-MM-DD acquisition date
    cloud: float | None
    assets: dict[str, str] = field(default_factory=dict)
    platform: str | None = None
    orbit_state: str | None = None
    relative_orbit: int | None = None


@dataclass(frozen=True)
class DailyScene:
    """All tiles acquired on one date (S2 dual-tile days mosaic together)."""

    date: str
    scenes: tuple[Scene, ...]


def parse_scenes(collection: str, payload: dict) -> list[Scene]:
    try:
        features = payload["features"]
    except (KeyError, TypeError) as exc:
        raise CatalogError(f"unexpected STAC payload for {collection}") from exc

    scenes = []
    for feature in features:
        props = feature.get("properties") or {}
        datetime = props.get("datetime")
        if not datetime:
            continue
        cloud = props.get("s2:cloud_cover", props.get("eo:cloud_cover"))
        scenes.append(
            Scene(
                id=str(feature.get("id", "")),
                collection=collection,
                datetime=str(datetime),
                date=str(datetime)[:10],
                cloud=float(cloud) if cloud is not None else None,
                assets={
                    str(name): str(asset.get("href", ""))
                    for name, asset in (feature.get("assets") or {}).items()
                },
                platform=props.get("platform"),
                orbit_state=props.get("sat:orbit_state"),
                relative_orbit=props.get("sat:relative_orbit"),
            )
        )
    return scenes


def group_by_date(scenes: Iterable[Scene]) -> list[DailyScene]:
    buckets: dict[str, list[Scene]] = {}
    for scene in scenes:
        buckets.setdefault(scene.date, []).append(scene)
    return [
        DailyScene(date=date, scenes=tuple(buckets[date]))
        for date in sorted(buckets)
    ]


def search(
    collection: str,
    bbox: Sequence[float],
    start: str,
    end: str,
    *,
    client: httpx.Client | None = None,
) -> list[Scene]:
    """Query the Earth Search STAC API for one collection and bbox."""
    body = {
        "collections": [collection],
        "bbox": list(bbox),
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": 200,
    }
    owns_client = client is None
    client = client or httpx.Client(timeout=60)
    try:
        response = client.post(EARTH_SEARCH_URL, json=body)
        if response.status_code >= 400:
            raise CatalogError(
                f"STAC search failed: HTTP {response.status_code} for {collection}"
            )
        return parse_scenes(collection, response.json())
    except httpx.HTTPError as exc:
        raise CatalogError(f"STAC search transport error: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"STAC search returned invalid JSON: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def search_day(
    collection: str,
    bbox: Sequence[float],
    day: date_type,
    *,
    client: httpx.Client | None = None,
) -> list[Scene]:
    return search(collection, bbox, day.isoformat(), day.isoformat(), client=client)

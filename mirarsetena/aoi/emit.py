"""Emit anchored AOI geometry as RFC 7946 GeoJSON (WGS84, no CRS member)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from pyproj import Transformer

from mirarsetena.aoi.anchor import Poi
from mirarsetena.aoi.traverse import Plan

_CRTM_TO_WGS84 = Transformer.from_crs("EPSG:5367", "EPSG:4326", always_xy=True)


def to_wgs84(vertices_crtm: Sequence[tuple[float, float]]) -> list[list[float]]:
    """Convert CRTM05 (E, N) vertices to [lon, lat] pairs, order preserved."""
    ring = []
    for east, north in vertices_crtm:
        lon, lat = _CRTM_TO_WGS84.transform(east, north)
        ring.append([lon, lat])
    return ring


def parcel_feature(plan: Plan, ring_wgs84: Sequence[list[float]]) -> dict:
    ring = [list(point) for point in ring_wgs84]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return {
        "type": "Feature",
        "properties": {
            "id": plan.id,
            "finca": plan.finca,
            "stated_area_m2": plan.stated_area_m2,
            "source": "plan-survey-1991",
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def poi_feature(poi: Poi) -> dict:
    lon, lat = _CRTM_TO_WGS84.transform(poi.east, poi.north)
    return {
        "type": "Feature",
        "properties": {"id": poi.id, "label": poi.label, "group": poi.group},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def feature_collection(features: Sequence[dict]) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def write_geojson(path: str | Path, collection: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(collection, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )

"""Emit anchored AOI geometry as RFC 7946 GeoJSON (WGS84, no CRS member)."""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pyproj import Transformer

from mirarsetena.aoi.anchor import Poi
from mirarsetena.aoi.traverse import Plan

_CRTM_TO_WGS84 = Transformer.from_crs("EPSG:5367", "EPSG:4326", always_xy=True)
# NOTE: EPSG:5367's authority axis order is (Northing, Easting). Feeding our
# (E, N) pairs into an axis-order-honoring tool (cs2cs without always_xy, most
# online converters) silently swaps them and lands ~500 km away. always_xy=True
# pins input to (E, N) and output to (lon, lat) as RFC 7946 requires.
# Cross-verified against PROJ 8.2.1 (cs2cs), 9.2 (pyproj) and 9.7 (rasterio):
# identical to <1e-6 deg; selected op "CR05 to WGS 84 (2)", accuracy 1.0 m.


def to_wgs84(vertices_crtm: Sequence[tuple[float, float]]) -> list[list[float]]:
    """Convert CRTM05 (E, N) vertices to [lon, lat] pairs, order preserved."""
    ring = []
    for east, north in vertices_crtm:
        lon, lat = _CRTM_TO_WGS84.transform(east, north)
        ring.append([lon, lat])
    return ring


def parcel_feature(
    plan: Plan,
    ring_wgs84: Sequence[list[float]],
    *,
    aligned: str | None = None,
) -> dict:
    ring = [list(point) for point in ring_wgs84]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    properties = {
        "id": plan.id,
        "finca": plan.finca,
        "stated_area_m2": plan.stated_area_m2,
        "source": "plan-survey-1991",
    }
    if aligned:
        properties["aligned"] = aligned
    return {
        "type": "Feature",
        "properties": properties,
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

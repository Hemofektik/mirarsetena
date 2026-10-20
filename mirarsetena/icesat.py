"""ICESat-2 ground tracks over the project AOI (user 2026-10-20).

CMR granule metadata (open, no auth) tells us WHERE and WHEN the ATLAS
laser measured near the AOI — 24 ATL06 passes since 2019 (~3/year).
The granule footprint is a long thin swath polygon; clipped to the
project bbox it shows the pass crossing the AOI on that date.

Elevation VALUES live in the ATL06 HDF5 files behind Earthdata login —
not fetched here (no credentials yet); the tracks layer is the honest
spatial/temporal record until then.
"""
from __future__ import annotations

Bbox = tuple[float, float, float, float]  # W, S, E, N

CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"


def _parse_cmr_polygon(field) -> list[tuple[float, float]]:
    """CMR polygon 'lat lon lat lon ...' -> [(lon, lat), ...].

    CMR hands polygons as either a bare string or a one-element list of
    strings (granule JSON varies) — both accepted.
    """
    if isinstance(field, (list, tuple)):
        if not field:
            return []
        field = field[0]
    nums = [float(x) for x in str(field).split()]
    pts = list(zip(nums[1::2], nums[0::2]))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _clip_edge(
    polygon: list[tuple[float, float]],
    inside,
    intersect,
) -> list[tuple[float, float]]:
    """Sutherland-Hodgman pass against one half-plane."""
    if not polygon:
        return []
    out = []
    prev = polygon[-1]
    prev_in = inside(prev)
    for cur in polygon:
        cur_in = inside(cur)
        if cur_in:
            if not prev_in:
                out.append(intersect(prev, cur))
            out.append(cur)
        elif prev_in:
            out.append(intersect(prev, cur))
        prev, prev_in = cur, cur_in
    return out


def clip_polygon_to_bbox(
    polygon: list[tuple[float, float]], bbox: Bbox
) -> list[tuple[float, float]]:
    """Clip a (lon, lat) polygon to the bbox (Sutherland-Hodgman).

    Returns [] when the polygon lies entirely outside.
    """
    west, south, east, north = bbox
    pts = list(polygon)

    def cut(p0, p1, value, axis, keep_ge):
        """Clip against axis == value; keep the >= (or <=) side."""
        a0, a1 = p0[axis], p1[axis]
        if keep_ge:
            in0, in1 = a0 >= value, a1 >= value
        else:
            in0, in1 = a0 <= value, a1 <= value
        if in0 == in1:
            return in1
        t = (value - a0) / (a1 - a0)
        return (p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1]))

    for value, axis, keep_ge in (
        (west, 0, True),
        (east, 0, False),
        (south, 1, True),
        (north, 1, False),
    ):
        pts = _clip_edge(
            pts,
            inside=lambda p, v=value, ax=axis, kg=keep_ge: (
                p[ax] >= v if kg else p[ax] <= v
            ),
            intersect=lambda a, b, v=value, ax=axis, kg=keep_ge: cut(
                a, b, v, ax, kg
            ),
        )
        if not pts:
            return []
    return pts


def granules_to_features(payload: dict, bbox: Bbox) -> list[dict]:
    """CMR granule JSON -> GeoJSON pass features clipped to the bbox.

    One Feature per granule whose footprint intersects the AOI, sorted by
    acquisition date; properties carry the date and granule id.
    """
    features = []
    for entry in payload.get("feed", {}).get("entry", []):
        date = (entry.get("time_start") or "")[:10]
        if len(date) != 10:
            continue
        for field in entry.get("polygons") or []:
            ring = _parse_cmr_polygon(field)
            if len(ring) < 3:
                continue
            clipped = clip_polygon_to_bbox(ring, bbox)
            if len(clipped) < 3:
                continue
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "date": date,
                        "granule": entry.get("producer_granule_id", ""),
                        "product": "ATL06",
                        "satellite": "ICESat-2",
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [clipped + [clipped[0]]],
                    },
                }
            )
            break  # one feature per granule (first intersecting polygon)
    features.sort(key=lambda f: f["properties"]["date"])
    return features


def fetch_tracks(bbox: Bbox, *, temporal: str | None = None) -> dict:
    """Fetch ATL06 granules from CMR (open metadata) as a FeatureCollection."""
    import httpx

    params = {
        "short_name": "ATL06",
        "bounding_box": ",".join(str(c) for c in bbox),
        "page_size": "200",
    }
    if temporal:
        params["temporal"] = temporal
    response = httpx.get(
        CMR_GRANULES_URL, params=params, timeout=30.0,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return {
        "type": "FeatureCollection",
        "features": granules_to_features(response.json(), bbox),
    }

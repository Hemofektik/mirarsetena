"""Anchor resolution: georeference cadastral traverses onto CRTM05.

SCOPE §7 strategy: the plans' shared survey edge fixes their *relative*
placement exactly; the *absolute* translation is chosen from anchor hypotheses
(registry vertices / centroids / edge midpoints) scored against physical
control — Río General along the western boundaries near the channel inspection
fix, the road east of the parcels, the parcels inside the project extent —
then ranked by registry consistency.

The registry printouts self-declare "Verificado Zona Catastrada: No", so
registry points are treated as approximate control: the best hypothesis must
still fit within MAX_REGISTRY_RESIDUAL_M, otherwise resolution fails loudly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml
from pyproj import Transformer

from mirarsetena.aoi.traverse import Leg, Plan, compute

MAX_REGISTRY_RESIDUAL_M = 50.0
CHANNEL_TOLERANCE_M = 50.0
EDGE_TOLERANCE_M = 0.5
EXTENT_TOLERANCE_DEG = 1e-4
LENGTH_MATCH_TOLERANCE_M = 0.1
AZIMUTH_OPPOSITION_TOLERANCE_DEG = 0.5

_WGS84 = Transformer.from_crs("EPSG:5367", "EPSG:4326", always_xy=True)


class ResolutionError(ValueError):
    """No consistent anchor could be found for the given plans."""


@dataclass(frozen=True)
class Poi:
    id: str
    group: str
    east: float
    north: float


@dataclass(frozen=True)
class Reference:
    registry: dict[str, tuple[float, float]]
    pois: dict[str, Poi]
    extent_wgs84: tuple[float, float, float, float]


@dataclass(frozen=True)
class AnchorSolution:
    """Absolute CRTM05 vertices per plan id, chosen anchors, fit diagnostics."""

    vertices: dict[str, tuple[tuple[float, float], ...]]
    features: dict[str, str]
    registry_residual_m: float
    wgs84_bbox: tuple[float, float, float, float]


def load_reference(path: str | Path) -> Reference:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    registry = {
        str(plan_id): (float(coord["east"]), float(coord["north"]))
        for plan_id, coord in raw["registry"].items()
    }
    pois = {
        str(row["id"]): Poi(
            id=str(row["id"]),
            group=str(row["group"]),
            east=float(row["east"]),
            north=float(row["north"]),
        )
        for row in raw["pois"]
    }
    extent = tuple(float(v) for v in raw["extent_wgs84"])
    return Reference(registry=registry, pois=pois, extent_wgs84=extent)


def _centroid(polygon: Sequence[tuple[float, float]]) -> tuple[float, float]:
    area = cx = cy = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area *= 0.5
    if area == 0:
        raise ResolutionError("degenerate polygon (zero area)")
    return (cx / (6 * area), cy / (6 * area))


def _features(polygon: Sequence[tuple[float, float]]) -> dict[str, tuple[float, float]]:
    feats = {f"v{i + 1}": p for i, p in enumerate(polygon)}
    feats["centroid"] = _centroid(polygon)
    n = len(polygon)
    for i in range(n):
        a = polygon[i]
        b = polygon[(i + 1) % n]
        feats[f"m{i + 1}-{(i + 1) % n + 1}"] = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return feats


def _find_shared_edge(
    plan_a: Plan, plan_b: Plan
) -> tuple[Leg, Leg]:
    """The unique equal-length, azimuth-opposed leg pair shared by two plans."""
    matches = []
    for leg_a in plan_a.legs:
        for leg_b in plan_b.legs:
            if abs(leg_a.distance_m - leg_b.distance_m) > LENGTH_MATCH_TOLERANCE_M:
                continue
            delta = (leg_a.azimuth_deg - leg_b.azimuth_deg) % 360
            if abs(delta - 180) <= AZIMUTH_OPPOSITION_TOLERANCE_DEG:
                matches.append((leg_a, leg_b))
    if len(matches) != 1:
        raise ResolutionError(
            f"expected exactly one shared edge between {plan_a.id} and "
            f"{plan_b.id}, found {len(matches)}"
        )
    return matches[0]


def _wgs84_bbox(
    points: Sequence[tuple[float, float]],
) -> tuple[float, float, float, float]:
    lons, lats = [], []
    for east, north in points:
        lon, lat = _WGS84.transform(east, north)
        lons.append(lon)
        lats.append(lat)
    return (min(lons), min(lats), max(lons), max(lats))


def resolve(plans: Sequence[Plan], reference: Reference) -> AnchorSolution:
    """Place every plan in absolute CRTM05 coordinates.

    Requires exactly two plans sharing one survey edge (the CDP Río General
    case); future projects load ready-made GeoJSON instead (SCOPE R5-Q2).
    """
    if len(plans) != 2:
        raise ResolutionError(f"anchor resolution needs exactly 2 plans, got {len(plans)}")
    plan_a, plan_b = plans

    local_a = compute(plan_a.legs).vertices[:-1]
    local_b = compute(plan_b.legs).vertices[:-1]

    leg_a, leg_b = _find_shared_edge(plan_a, plan_b)

    def vertex_of(plan: Plan, label: str) -> int:
        labels = []
        seen_first = None
        for leg in plan.legs:
            if seen_first is None:
                seen_first = leg.start
            labels.append(leg.start)
        # polygon vertex i corresponds to leg i's start label
        return labels.index(label)

    ia_start, ia_end = vertex_of(plan_a, leg_a.start), vertex_of(plan_a, leg_a.end)
    ib_start, ib_end = vertex_of(plan_b, leg_b.start), vertex_of(plan_b, leg_b.end)

    # Try both endpoint pairings of the shared segment; keep the better fit.
    best = None
    for a_end, b_end in ((ia_end, ib_start), (ia_end, ib_end)):
        a_start = ia_start if a_end == ia_end else ia_end
        b_start = ib_start if b_end == ib_end else ib_end
        shift = (
            local_a[a_end][0] - local_b[b_end][0],
            local_a[a_end][1] - local_b[b_end][1],
        )
        error = math.dist(
            local_a[a_start],
            (local_b[b_start][0] + shift[0], local_b[b_start][1] + shift[1]),
        )
        if best is None or error < best[0]:
            best = (error, shift, a_start, b_start)
    if best is None or best[0] > EDGE_TOLERANCE_M:
        raise ResolutionError(
            f"shared edge does not align: {best[0] if best else float('inf'):.3f} m"
        )
    _, shift, _, _ = best

    # Plan B expressed in plan A's local frame (relative placement exact).
    local_b_in_a = [(x + shift[0], y + shift[1]) for x, y in local_b]

    feats_a = _features(local_a)
    feats_b = {name: (p[0] + shift[0], p[1] + shift[1]) for name, p in _features(local_b).items()}

    reg_a = reference.registry[plan_a.id]
    reg_b = reference.registry[plan_b.id]
    delta = (reg_b[0] - reg_a[0], reg_b[1] - reg_a[1])
    channel = reference.pois["channel"]
    road = reference.pois["road-start"]
    extent = reference.extent_wgs84

    candidates = []
    for name_a, feat_a in feats_a.items():
        for name_b, feat_b in feats_b.items():
            residual = math.dist(
                (feat_b[0] - feat_a[0], feat_b[1] - feat_a[1]), delta
            )
            t_a = (reg_a[0] - feat_a[0], reg_a[1] - feat_a[1])
            t_b = (reg_b[0] - feat_b[0], reg_b[1] - feat_b[1])
            t = ((t_a[0] + t_b[0]) / 2, (t_a[1] + t_b[1]) / 2)

            verts_a = tuple((x + t[0], y + t[1]) for x, y in local_a)
            verts_b = tuple((x + t[0], y + t[1]) for x, y in local_b_in_a)

            # C1: Río General along the west — each plan's west edge near the
            # channel inspection fix longitude.
            west_ok = all(
                abs(min(v[0] for v in verts) - channel.east) <= CHANNEL_TOLERANCE_M
                for verts in (verts_a, verts_b)
            )
            # C2: road east of the parcels.
            road_ok = max(
                max(v[0] for v in verts) for verts in (verts_a, verts_b)
            ) <= road.east
            # C3: inside the project extent (SCOPE §1).
            bbox = _wgs84_bbox(verts_a + verts_b)
            west, south, east, north = bbox
            ew, es, ee, en = extent
            extent_ok = (
                west >= ew - EXTENT_TOLERANCE_DEG
                and south >= es - EXTENT_TOLERANCE_DEG
                and east <= ee + EXTENT_TOLERANCE_DEG
                and north <= en + EXTENT_TOLERANCE_DEG
            )

            if west_ok and road_ok and extent_ok:
                candidates.append((residual, name_a, name_b, t, verts_a, verts_b, bbox))

    if not candidates:
        raise ResolutionError(
            "no anchor hypothesis satisfies the physical control "
            "(river west / road east / extent)"
        )

    candidates.sort(key=lambda c: c[0])
    residual, name_a, name_b, _, verts_a, verts_b, bbox = candidates[0]
    if residual >= MAX_REGISTRY_RESIDUAL_M:
        raise ResolutionError(
            f"best registry consistency {residual:.1f} m exceeds "
            f"{MAX_REGISTRY_RESIDUAL_M} m"
        )

    return AnchorSolution(
        vertices={plan_a.id: verts_a, plan_b.id: verts_b},
        features={plan_a.id: name_a, plan_b.id: name_b},
        registry_residual_m=residual,
        wgs84_bbox=bbox,
    )

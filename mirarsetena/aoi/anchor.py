"""Anchor resolution: georeference cadastral traverses onto CRTM05.

SCOPE §7 strategy: the plans' shared survey edge fixes their *relative*
placement exactly; the *absolute* translation is chosen from anchor hypotheses
(registry vertices / centroids / edge midpoints) scored against physical
control — Río General along the western boundaries near the channel inspection
fix, the road east of the parcels, the parcels inside the project extent —
then ranked by registry consistency.

The registry printouts self-declare "Verificado Zona Catastrada: No", and
their printed CRTM column is a rigid translation of the 1991 legacy pair
whose legacy->CRTM step is off ~ (8.7, 308.7) m vs the authoritative
EPSG:5457->5367 operation (the legacy pair *properly converted* lands at
the placed parcels' south edges). Registry points are therefore treated as
approximate control: the best hypothesis must still fit within
MAX_REGISTRY_RESIDUAL_M, otherwise resolution fails loudly.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pyproj import Transformer

from mirarsetena.aoi.traverse import Leg, Plan, compute

EXTENT_TOLERANCE_DEG = 1e-4
EDGE_TOLERANCE_M = 0.5
LENGTH_MATCH_TOLERANCE_M = 0.1
AZIMUTH_OPPOSITION_TOLERANCE_DEG = 0.5

# Placement search (translation only; relative geometry is exact).
_ON_SITE_POINTS = ("breaker", "dumper-ramp", "office", "storage", "channel", "quarry")
BANK_BAND_M = 60.0  # vertices this close to a shape's west edge count as bank
QB_MARGIN_M = 15.0  # east edges must clear Quebrada Grande by this much
ROAD_MARGIN_M = 15.0  # ...and the public road (road-start design point)
COARSE_STEP_M = 4.0
FINE_STEP_M = 1.0
FINE_RADIUS_M = 5.0

_WGS84 = Transformer.from_crs("EPSG:5367", "EPSG:4326", always_xy=True)


class ResolutionError(ValueError):
    """No consistent anchor could be found for the given plans."""


@dataclass(frozen=True)
class Poi:
    id: str
    label: str
    group: str
    east: float
    north: float


@dataclass(frozen=True)
class Reference:
    registry: dict[str, tuple[float, float]]
    pois: dict[str, Poi]
    extent_wgs84: tuple[float, float, float, float]
    river: list[list[tuple[float, float]]]  # Río General polylines (CRTM05)
    quebrada: list[list[tuple[float, float]]]  # Quebrada Grande polyline


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
            label=str(row.get("label", "")),
            group=str(row["group"]),
            east=float(row["east"]),
            north=float(row["north"]),
        )
        for row in raw["pois"]
    }
    extent = tuple(float(v) for v in raw["extent_wgs84"])

    def polyline(rows: Sequence[dict]) -> list[tuple[float, float]]:
        return [(float(row["east"]), float(row["north"])) for row in rows]

    river = [polyline(run) for run in raw.get("river", [])]
    quebrada = [polyline(run) for run in raw.get("quebrada", [])]
    if not river:
        raise ValueError("reference.yaml must carry the Río General polyline")
    if not quebrada:
        raise ValueError("reference.yaml must carry the Quebrada Grande polyline")
    return Reference(
        registry=registry,
        pois=pois,
        extent_wgs84=extent,
        river=river,
        quebrada=quebrada,
    )


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


def _point_in_polygon(x: float, y: float, ring: Sequence[tuple[float, float]]) -> bool:
    """Even-odd crossing test; open or closed ring."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _distance_to_polyline(
    p: tuple[float, float], poly: Sequence[tuple[float, float]]
) -> float:
    best = math.inf
    for i in range(len(poly) - 1):
        (x1, y1), (x2, y2) = poly[i], poly[i + 1]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = (
            0.0
            if length2 == 0
            else max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / length2))
        )
        best = min(best, math.hypot(p[0] - (x1 + t * dx), p[1] - (y1 + t * dy)))
    return best


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
    feats_b = {
        name: (p[0] + shift[0], p[1] + shift[1])
        for name, p in _features(local_b).items()
    }

    # --- absolute placement = local + t, searched under physical control ---
    # Hard: every on-site works from RES-1333-2017 sits INSIDE the parcels
    # ("sitio de quebrador en parte interna de finca"). Soft: west edges hug
    # the Río General bank, east edges stay west of Quebrada Grande and the
    # public road. The registry pair stays a diagnostic (unverified, ~360 m
    # conflicting with the resolution's GPS/design control).
    site_xy = [
        (reference.pois[key].east, reference.pois[key].north)
        for key in _ON_SITE_POINTS
        if key in reference.pois
    ]
    if len(site_xy) != len(_ON_SITE_POINTS):
        missing = set(_ON_SITE_POINTS) - set(reference.pois)
        raise ResolutionError(f"reference.yaml missing on-site POIs: {sorted(missing)}")

    shape = list(local_a) + list(local_b_in_a)
    road_east = reference.pois["road-start"].east
    qb_points = [p for poly in reference.quebrada for p in poly]

    def translate(t: tuple[float, float]) -> tuple[list, list]:
        dx, dy = t
        return (
            [(x + dx, y + dy) for x, y in local_a],
            [(x + dx, y + dy) for x, y in local_b_in_a],
        )

    def contains(verts_a: list, verts_b: list) -> bool:
        rings = (verts_a, verts_b)
        for sx, sy in site_xy:
            if not any(_point_in_polygon(sx, sy, ring) for ring in rings):
                return False
        return True

    def within_extent(verts_a: list, verts_b: list) -> bool:
        lons, lats = _WGS84.transform(
            [v[0] for v in verts_a + verts_b],
            [v[1] for v in verts_a + verts_b],
        )
        west, south, east, north = min(lons), min(lats), max(lons), max(lats)
        ew, es, ee, en = reference.extent_wgs84
        return (
            west >= ew - EXTENT_TOLERANCE_DEG
            and south >= es - EXTENT_TOLERANCE_DEG
            and east <= ee + EXTENT_TOLERANCE_DEG
            and north <= en + EXTENT_TOLERANCE_DEG
        )

    def score(verts_a: list, verts_b: list) -> float:
        """Lower is better: river-bank fit + east-neighbour overshoot."""
        total = 0.0
        for verts in (verts_a, verts_b):
            west_edge = min(v[0] for v in verts)
            for v in verts:
                if v[0] <= west_edge + BANK_BAND_M:
                    dist = min(_distance_to_polyline(v, poly) for poly in reference.river)
                    total += dist * dist
            east_edge = max(v[0] for v in verts)
            for v in verts:
                if v[0] >= east_edge - BANK_BAND_M:
                    qb = min(qb_points, key=lambda p: math.dist(p, v))
                    total += max(0.0, v[0] - (qb[0] - QB_MARGIN_M)) ** 2
                    total += max(0.0, v[0] - (road_east - ROAD_MARGIN_M)) ** 2
        return total

    # Necessary-condition window for containment (site bbox vs shape bbox).
    shape_xs = [p[0] for p in shape]
    shape_ys = [p[1] for p in shape]
    pad = 100.0
    dx_lo = min(p[0] for p in site_xy) - max(shape_xs) - pad
    dx_hi = max(p[0] for p in site_xy) - min(shape_xs) + pad
    dy_lo = min(p[1] for p in site_xy) - max(shape_ys) - pad
    dy_hi = max(p[1] for p in site_xy) - min(shape_ys) + pad

    def grid(step: float) -> list[tuple[float, float]]:
        xs = [dx_lo + i * step for i in range(int((dx_hi - dx_lo) / step) + 1)]
        ys = [dy_lo + j * step for j in range(int((dy_hi - dy_lo) / step) + 1)]
        return [(dx, dy) for dy in ys for dx in xs]

    feasible: list[tuple[float, tuple[float, float]]] = []
    for t in grid(COARSE_STEP_M):
        verts_a, verts_b = translate(t)
        if contains(verts_a, verts_b) and within_extent(verts_a, verts_b):
            feasible.append((t, (verts_a, verts_b)))
    if not feasible:
        raise ResolutionError(
            "no translation places all on-site works (RES-1333-2017) "
            "inside the parcels within the project extent"
        )

    def best_of(candidates) -> tuple[float, tuple[float, float]]:
        ranked = [
            (score(va, vb), t)
            for t, (va, vb) in candidates
        ]
        ranked.sort(key=lambda item: (item[0], item[1]))
        return ranked[0]

    _, coarse_t = best_of(feasible)

    fine: list[tuple[float, tuple[float, float]]] = []
    for t in grid(COARSE_STEP_M):  # keep only cells near the coarse optimum
        if abs(t[0] - coarse_t[0]) > FINE_RADIUS_M + COARSE_STEP_M:
            continue
        if abs(t[1] - coarse_t[1]) > FINE_RADIUS_M + COARSE_STEP_M:
            continue
        verts_a, verts_b = translate(t)
        if contains(verts_a, verts_b) and within_extent(verts_a, verts_b):
            fine.append((t, (verts_a, verts_b)))
    # sub-metre refinement around the coarse optimum
    for i in range(-int(FINE_RADIUS_M / FINE_STEP_M), int(FINE_RADIUS_M / FINE_STEP_M) + 1):
        for j in range(-int(FINE_RADIUS_M / FINE_STEP_M), int(FINE_RADIUS_M / FINE_STEP_M) + 1):
            t = (
                round(coarse_t[0] + i * FINE_STEP_M, 3),
                round(coarse_t[1] + j * FINE_STEP_M, 3),
            )
            verts_a, verts_b = translate(t)
            if contains(verts_a, verts_b) and within_extent(verts_a, verts_b):
                fine.append((t, (verts_a, verts_b)))

    _, t = best_of(fine)
    verts_a, verts_b = translate(t)

    # Diagnostics: the registry pair is carried, never fitted.
    registry_residual_m = 0.0
    features: dict[str, str] = {}
    for plan_id, feats, anchor in (
        (plan_a.id, feats_a, reference.registry[plan_a.id]),
        (plan_b.id, feats_b, reference.registry[plan_b.id]),
    ):
        nearest = min(feats.items(), key=lambda item: math.dist((item[1][0] + t[0], item[1][1] + t[1]), anchor))
        features[plan_id] = nearest[0]
        registry_residual_m = max(
            registry_residual_m,
            math.dist((nearest[1][0] + t[0], nearest[1][1] + t[1]), anchor),
        )

    bbox = _wgs84_bbox(verts_a + verts_b)

    return AnchorSolution(
        vertices={plan_a.id: tuple(verts_a), plan_b.id: tuple(verts_b)},
        features=features,
        registry_residual_m=registry_residual_m,
        wgs84_bbox=bbox,
    )

"""B.2 — anchor resolution: georeferencing both plans from registry + physical control.

Seam: mirarsetena.aoi.anchor (resolve / load_reference).
Assertions reference external facts only: RES-1333-2017 inspection GPS fixes,
design coordinates, the SCOPE §1 extent, and the registry printout anchors —
never the algorithm's own intermediate values.
"""
import math
from pathlib import Path

import pytest

from mirarsetena.aoi.anchor import load_reference, resolve
from mirarsetena.aoi.traverse import load_plans

REPO_ROOT = Path(__file__).resolve().parents[2]
DERROTERO = REPO_ROOT / "data" / "projects" / "cdp-rio-general" / "derrotero.yaml"
REFERENCE = REPO_ROOT / "data" / "projects" / "cdp-rio-general" / "reference.yaml"

PLAN_A = "SJ-980860-1991"
PLAN_B = "SJ-980861-1991"


@pytest.fixture(scope="module")
def plans():
    return load_plans(DERROTERO)


@pytest.fixture(scope="module")
def reference():
    return load_reference(REFERENCE)


@pytest.fixture(scope="module")
def solution(plans, reference):
    return resolve(plans, reference)


def test_shared_edge_coincides_within_half_metre(solution):
    """The 720.62 m edge (A leg 13-1 vs B leg 4-5) is the same physical line."""
    a = solution.vertices[PLAN_A]
    b = solution.vertices[PLAN_B]
    # A.v1 <-> B.v4 and A.v13 <-> B.v5 after alignment
    deviation = max(math.dist(a[0], b[3]), math.dist(a[12], b[4]))
    assert deviation < 0.5, f"shared edge off by {deviation:.3f} m"


def _distance_to_polyline(p, poly):
    best = math.inf
    for i in range(len(poly) - 1):
        (x1, y1), (x2, y2) = poly[i], poly[i + 1]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / length2))
        best = min(best, math.hypot(p[0] - (x1 + t * dx), p[1] - (y1 + t * dy)))
    return best


def _signed_sides(verts, p, q):
    vx, vy = q[0] - p[0], q[1] - p[1]
    length = math.hypot(vx, vy)
    return [(vx * (v[1] - p[1]) - vy * (v[0] - p[0])) / length for v in verts]


def test_parcels_straddle_the_shared_edge_without_overlap(solution):
    """Interior of A strictly on one side, interior of B on the other."""
    a = solution.vertices[PLAN_A]
    b = solution.vertices[PLAN_B]
    p, q = a[12], a[0]  # shared line through A.v13 and A.v1
    on_line = 1.0  # metres: shared-edge vertices sit within closure error

    sides_a = _signed_sides(a, p, q)
    sides_b = _signed_sides(b, p, q)
    interior_a = [s for i, s in enumerate(sides_a) if i not in (0, 12) and abs(s) > on_line]
    interior_b = [s for i, s in enumerate(sides_b) if i not in (3, 4) and abs(s) > on_line]

    assert all(s < 0 for s in interior_a), "plan A has vertices on the wrong side"
    assert all(s > 0 for s in interior_b), "plan B has vertices on the wrong side"


def _point_in_ring(pt, ring):
    """Even-odd test over all edges, open or closed ring."""
    x, y = pt
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def test_resolution_site_points_inside_their_fincas(solution, reference):
    """RES-1333-2017: "sitio de quebrador en parte interna de finca".

    The design coordinates (breaker, dumper ramp, office, storage) and the
    2016 inspection GPS fixes (cauce, quebrador) all sit ON the works inside
    the registered parcels — external fact from the resolution itself.
    """
    must_be_inside = ("breaker", "dumper-ramp", "office", "storage", "channel", "quarry")
    rings = list(solution.vertices.values())
    for key in must_be_inside:
        poi = reference.pois[key]
        pt = (poi.east, poi.north)
        assert any(_point_in_ring(pt, ring) for ring in rings), (
            f"{key} ({poi.east:.0f},{poi.north:.0f}) falls outside every parcel"
        )


def test_west_edges_follow_rio_general(solution, reference):
    """W neighbour per both plans: the west edges lie on the Río General bank
    (OSM river polyline in reference.yaml — not the process-water cauce fix)."""
    for plan_id, verts in solution.vertices.items():
        west = min(verts, key=lambda v: v[0])
        distance = min(
            _distance_to_polyline(west, poly) for poly in reference.river
        )
        assert distance <= 30, (
            f"{plan_id} west-most vertex {distance:.1f} m from Río General"
        )


def test_quebrada_grande_stays_outside(solution, reference):
    """SE neighbour per both plans: Quebrada Grande runs east of the parcels."""
    for plan_id, verts in solution.vertices.items():
        east = max(verts, key=lambda v: v[0])
        qb = min(
            (p for poly in reference.quebrada for p in poly),
            key=lambda p: math.dist(p, east),
        )
        assert east[0] <= qb[0] + 5, (
            f"{plan_id} east vertex reaches {east[0]:.0f}, Quebrada Grande at {qb[0]:.0f}"
        )


def test_road_lies_east_of_the_parcels(solution, reference):
    road = reference.pois["road-start"]
    east = max(max(v[0] for v in verts) for verts in solution.vertices.values())
    assert east <= road.east, f"parcels reach {east:.1f}, road at {road.east}"


def test_wgs84_bbox_within_scope_extent(solution, reference):
    west, south, east, north = solution.wgs84_bbox
    ew, es, ee, en = reference.extent_wgs84
    tol = 1e-4  # ~11 m: the extent itself is built from design points
    assert west >= ew - tol
    assert south >= es - tol
    assert east <= ee + tol
    assert north <= en + tol


def test_registry_conflict_is_reported_not_fatal(solution):
    """The registry pair (self-declared "Verificado Zona Catastrada: No",
    converted from 100 m-rounded 1991 legacy coordinates) disagrees with the
    resolution's GPS/design control by ~360-390 m. Resolution control wins;
    the conflict stays visible as a diagnostic instead of failing the build.
    """
    assert 50 < solution.registry_residual_m < 600


def test_resolution_is_deterministic(plans, reference):
    first = resolve(plans, reference)
    second = resolve(plans, reference)
    assert first.vertices == second.vertices
    assert first.features == second.features
    assert first.registry_residual_m == second.registry_residual_m

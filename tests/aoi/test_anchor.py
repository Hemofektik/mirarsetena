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


def test_river_control_point_aligns_west_boundaries(solution, reference):
    """Río General runs along the west: each plan's west edge sits near the
    channel inspection fix (RES-1333-2017, 2016 GPS)."""
    channel = reference.pois["channel"]
    for plan_id, verts in solution.vertices.items():
        west = min(v[0] for v in verts)
        delta = abs(west - channel.east)
        assert delta <= 50, f"{plan_id} west edge {west:.1f} vs channel {channel.east}: {delta:.1f} m"


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


def test_registry_control_residual_within_tolerance(solution):
    """Registry printouts self-declare "Verificado: No" — anchors must still
    be consistent to a sane bound (probe: best pair fits within ~24 m)."""
    assert solution.registry_residual_m < 50


def test_resolution_is_deterministic(plans, reference):
    first = resolve(plans, reference)
    second = resolve(plans, reference)
    assert first.vertices == second.vertices
    assert first.features == second.features
    assert first.registry_residual_m == second.registry_residual_m

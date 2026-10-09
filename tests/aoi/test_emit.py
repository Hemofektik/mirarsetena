"""B.3 — GeoJSON + POI emission.

Seam: tools.aoi.generate (the one-off generator per SCOPE R5-Q2) writing
canonical project geometry consumed by the config registry (aoi_path/pois_path).
Assertions reference external facts: official stated areas, the shared-edge
spec, Appendix B labels, and independently computed CRTM→WGS84 fixes.
"""
import itertools
import json
import math
import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from mirarsetena.aoi.traverse import compute, load_plans
from tools.aoi import generate

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_DIR = REPO_ROOT / "data" / "projects" / "cdp-rio-general"

INV_CRTM = Transformer.from_crs("EPSG:4326", "EPSG:5367", always_xy=True)

EXPECTED_POI_IDS = {
    "project-start",
    "project-end",
    "road-start",
    "road-end",
    "breaker",
    "dumper-ramp",
    "office",
    "storage",
    "channel",
    "quarry",
}


def _run_generator(tmp_path: Path) -> dict[str, Path]:
    shutil.copy(PROJECT_DIR / "derrotero.yaml", tmp_path / "derrotero.yaml")
    shutil.copy(PROJECT_DIR / "reference.yaml", tmp_path / "reference.yaml")
    return generate(tmp_path)


@pytest.fixture(scope="module")
def emitted(tmp_path_factory):
    paths = _run_generator(tmp_path_factory.mktemp("aoi"))
    aoi = json.loads(paths["aoi"].read_text(encoding="utf-8"))
    pois = json.loads(paths["pois"].read_text(encoding="utf-8"))
    return aoi, pois


def _by_id(aoi):
    return {f["properties"]["id"]: f for f in aoi["features"]}


def test_aoi_geojson_is_a_valid_feature_collection_of_two_parcels(emitted):
    aoi, _ = emitted
    assert aoi["type"] == "FeatureCollection"
    assert len(aoi["features"]) == 2
    by_id = _by_id(aoi)
    assert set(by_id) == {"SJ-980860-1991", "SJ-980861-1991"}
    for plan_id, feature in by_id.items():
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Polygon"
        assert feature["properties"]["finca"].startswith("1-")
        assert feature["properties"]["stated_area_m2"] > 100_000
        ring = feature["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1], f"{plan_id} ring not closed"
        assert len(ring) >= 4
        for lon, lat in ring:
            assert -180 <= lon <= 180 and -90 <= lat <= 90
            assert -83.68 < lon < -83.66, f"{plan_id} lon {lon} outside AOI"
            assert 9.37 < lat < 9.40, f"{plan_id} lat {lat} outside AOI"


def _cross(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _proper_intersection(a, b, c, d) -> bool:
    d1 = _cross(*a, *b, *c)
    d2 = _cross(*a, *b, *d)
    d3 = _cross(*c, *d, *a)
    d4 = _cross(*c, *d, *b)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _assert_simple(ring, plan_id: str):
    segments = list(itertools.pairwise(ring))
    n = len(segments)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # adjacent segments share a vertex by construction
            assert not _proper_intersection(segments[i][0], segments[i][1],
                                            segments[j][0], segments[j][1]), \
                f"{plan_id} polygon self-intersects (segments {i}, {j})"


def test_parcel_polygons_are_simple(emitted):
    aoi, _ = emitted
    for feature in aoi["features"]:
        _assert_simple(feature["geometry"]["coordinates"][0],
                       feature["properties"]["id"])


def test_pois_geojson_has_all_ten_points_with_groups_and_labels(emitted):
    _, pois = emitted
    assert pois["type"] == "FeatureCollection"
    assert len(pois["features"]) == 10
    groups = [f["properties"]["group"] for f in pois["features"]]
    assert groups.count("facilities") == 8
    assert groups.count("inspection") == 2
    ids = {f["properties"]["id"] for f in pois["features"]}
    assert ids == EXPECTED_POI_IDS
    for feature in pois["features"]:
        assert feature["properties"]["label"].strip(), "POI missing label"
        assert feature["geometry"]["type"] == "Point"
        lon, lat = feature["geometry"]["coordinates"]
        assert -83.68 < lon < -83.66 and 9.37 < lat < 9.40


def test_poi_coordinates_match_independent_reference_fixes(emitted):
    """Golden values from an independent CRTM05→WGS84 conversion of the
    RES-1333-2017 GPS/design fixes (session-verified literals)."""
    _, pois = emitted
    by_id = {f["properties"]["id"]: f for f in pois["features"]}
    channel = by_id["channel"]["geometry"]["coordinates"]
    assert channel[0] == pytest.approx(-83.669384, abs=1e-6)
    assert channel[1] == pytest.approx(9.385402, abs=1e-6)
    breaker = by_id["breaker"]["geometry"]["coordinates"]
    assert breaker[0] == pytest.approx(-83.669203, abs=1e-6)
    assert breaker[1] == pytest.approx(9.385362, abs=1e-6)


@pytest.mark.acceptance
def test_bdd_generator_produces_validated_geometry(tmp_path):
    """BDD (IMPLEMENTATION B.3): given the corrected Appendix A tables and
    Appendix B anchors, when the generator runs, then both parcels close < 2 m
    with area ±1 % of stated, the shared edge coincides within 0.5 m, and the
    emitted GeoJSON loads with exactly 8 facility + 2 inspection POIs."""
    paths = _run_generator(tmp_path)

    # Hard acceptance gates in CRTM (SCOPE §7) — fail loudly if violated.
    for plan in load_plans(paths["aoi"].parent / "derrotero.yaml"):
        compute(plan.legs).check(plan.stated_area_m2)

    aoi = json.loads(paths["aoi"].read_text(encoding="utf-8"))
    pois = json.loads(paths["pois"].read_text(encoding="utf-8"))

    # Shared edge verified on the EMITTED artifact (WGS84 -> back to CRTM).
    by_id = _by_id(aoi)

    def to_crtm(ring):
        return [INV_CRTM.transform(lon, lat) for lon, lat in ring]

    a = to_crtm(by_id["SJ-980860-1991"]["geometry"]["coordinates"][0])
    b = to_crtm(by_id["SJ-980861-1991"]["geometry"]["coordinates"][0])
    deviation = max(math.dist(a[0], b[3]), math.dist(a[12], b[4]))
    assert deviation < 0.5, f"emitted shared edge off by {deviation:.3f} m"

    groups = [f["properties"]["group"] for f in pois["features"]]
    assert len(pois["features"]) == 10
    assert groups.count("facilities") == 8
    assert groups.count("inspection") == 2


def test_alignment_snaps_parcels_to_quebrada_and_forest(tmp_path):
    """User (2026-10-09): rotate/translate the perimeter so the east edge
    is directly adjacent to the Quebrada Grande ravine and the south edge
    to the forest/orchard on the southern border. The generator owns this
    (hand-edits to aoi.geojson would be reverted on regeneration): a rigid
    transform fitted to both targets, applied before emission."""
    import math as _math

    paths = _run_generator(tmp_path)
    aoi = json.loads(paths["aoi"].read_text(encoding="utf-8"))
    by_id = _by_id(aoi)

    # the emitted parcels must carry the alignment note
    for f in aoi["features"]:
        assert "aligned" in f["properties"], "alignment provenance missing"

    # adjacency measured in CRTM05 metres against the reference polylines
    from mirarsetena.aoi.anchor import load_reference
    reference = load_reference(tmp_path / "reference.yaml")
    forest = [tuple(p) for poly in reference.forest for p in poly]  # north edge

    def dist_to_polyline(p, chain):
        best = float("inf")
        for a, b in itertools.pairwise(chain):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            t = 0 if L2 == 0 else max(0.0, min(1.0, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / L2))
            best = min(best, _math.hypot(p[0]-(a[0]+t*dx), p[1]-(a[1]+t*dy)))
        return best

    # east chain: plan 980861 vertices 1..4 + plan 980860 vertex 3 (ring idx 0..3, 2)
    east_ring = by_id["SJ-980861-1991"]["geometry"]["coordinates"][0]
    south_ring = by_id["SJ-980860-1991"]["geometry"]["coordinates"][0]
    east = [INV_CRTM.transform(*p) for p in east_ring[0:4] + [south_ring[2]]]
    south = [INV_CRTM.transform(*p) for p in south_ring[2:9]]
    quebrada = [tuple(p) for poly in reference.quebrada for p in poly]

    east_gaps = [dist_to_polyline(p, quebrada) for p in east]
    south_gaps = [dist_to_polyline(p, forest) for p in south]
    assert max(east_gaps) <= 15, f"east edge not adjacent to ravine: {east_gaps}"
    assert max(south_gaps) <= 25, f"south edge not adjacent to forest: {south_gaps}"

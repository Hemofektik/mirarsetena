"""B.1 — derrotero traverse core.

Seam: public API of mirarsetena.aoi.traverse.
Golden expectations come from docs/SCOPE.md Appendix A (validated 2026-10-06:
closures 0.02 m / 0.05 m, areas within 0.01% of stated) and from independent
worked examples (the 100 m square).
"""
from pathlib import Path

import pytest

from mirarsetena.aoi.traverse import (
    Leg,
    TraverseError,
    azimuth_to_decimal,
    compute,
    distance_m,
    load_plans,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DERROTERO = REPO_ROOT / "data" / "projects" / "cdp-rio-general" / "derrotero.yaml"


def test_azimuth_converts_degrees_minutes_to_decimal_degrees():
    assert azimuth_to_decimal(89, 19) == pytest.approx(89.316667, abs=1e-5)
    assert azimuth_to_decimal(269, 19) == pytest.approx(269.316667, abs=1e-5)
    assert azimuth_to_decimal(0, 59) == pytest.approx(0.983333, abs=1e-5)


def test_azimuth_rejects_out_of_range_components():
    with pytest.raises(TraverseError):
        azimuth_to_decimal(89, 60)
    with pytest.raises(TraverseError):
        azimuth_to_decimal(361, 0)


def test_distance_combines_metres_and_centimetres():
    assert distance_m(720, 62) == pytest.approx(720.62)
    assert distance_m(0, 68) == pytest.approx(0.68)
    with pytest.raises(TraverseError):
        distance_m(1, 100)
    with pytest.raises(TraverseError):
        distance_m(-1, 0)


def test_square_traverse_has_known_geometry():
    """Worked example: a 100 m square starting at the origin, heading north."""
    legs = [
        Leg("1", "2", 0.0, 100.0),
        Leg("2", "3", 90.0, 100.0),
        Leg("3", "4", 180.0, 100.0),
        Leg("4", "1", 270.0, 100.0),
    ]
    result = compute(legs)
    assert result.area_m2 == pytest.approx(10_000.0, rel=1e-9)
    assert result.closure_m == pytest.approx(0.0, abs=1e-9)
    assert result.perimeter_m == pytest.approx(400.0, rel=1e-9)
    assert len(result.vertices) == 5


def test_real_plans_reproduce_validated_measurements():
    plans = {p.id: p for p in load_plans(DERROTERO)}
    p860 = plans["SJ-980860-1991"]
    p861 = plans["SJ-980861-1991"]
    assert len(p860.legs) == 13
    assert len(p861.legs) == 9

    r860 = compute(p860.legs)
    r861 = compute(p861.legs)

    # Validated against the scans: 0.02 m and 0.05 m closure (gate here < 0.1 m)
    assert r860.closure_m < 0.1, f"980860 closure {r860.closure_m:.3f} m"
    assert r861.closure_m < 0.1, f"980861 closure {r861.closure_m:.3f} m"

    # Official stated areas from the registry printouts (independent source)
    assert p860.stated_area_m2 == 111_820.13
    assert p861.stated_area_m2 == 111_826.59
    for r, plan in ((r860, p860), (r861, p861)):
        err = abs(r.area_m2 - plan.stated_area_m2) / plan.stated_area_m2 * 100
        assert err < 0.1, f"{plan.id} area off by {err:.3f}%"

    # Perimeters summed from the derrotero distance column
    assert r860.perimeter_m == pytest.approx(1_821.12, abs=0.05)
    assert r861.perimeter_m == pytest.approx(1_896.98, abs=0.05)

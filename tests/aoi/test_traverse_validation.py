"""B.1 (continued) — hard acceptance gates for traverses.

Seam: mirarsetena.aoi.traverse compute/check contract per docs/SCOPE.md §7:
closure < 2 m and computed area ±1 % of stated are hard acceptance checks.
"""
from pathlib import Path

import pytest

from mirarsetena.aoi.traverse import TraverseError, compute, load_plans

REPO_ROOT = Path(__file__).resolve().parents[2]
DERROTERO = REPO_ROOT / "data" / "projects" / "cdp-rio-general" / "derrotero.yaml"


def test_empty_traverse_is_rejected():
    with pytest.raises(TraverseError, match="no legs"):
        compute([])


def test_open_traverse_is_rejected():
    """Dropping the closing leg 13-1 must fail loudly, not quietly skew area."""
    plans = {p.id: p for p in load_plans(DERROTERO)}
    open_legs = plans["SJ-980860-1991"].legs[:-1]
    with pytest.raises(TraverseError, match="open"):
        compute(open_legs)


def test_default_gates_pass_the_validated_plans():
    """SCOPE defaults (closure < 2 m, area ±1 %) must accept both real plans."""
    for plan in load_plans(DERROTERO):
        result = compute(plan.legs)
        result.check(stated_area_m2=plan.stated_area_m2)  # must not raise


def test_strict_closure_gate_reports_failure():
    plan = load_plans(DERROTERO)[0]
    result = compute(plan.legs)
    with pytest.raises(TraverseError, match="closure"):
        result.check(plan.stated_area_m2, max_closure_m=0.001)


def test_area_gate_reports_failure():
    plan = load_plans(DERROTERO)[0]
    result = compute(plan.legs)
    with pytest.raises(TraverseError, match="area"):
        result.check(plan.stated_area_m2 * 1.05, max_area_error_pct=1.0)

"""H.2 — SLC pair planner: same orbit AND state, 12-day nominal baseline.

Seam: mirarsetena.coherence.pairs (plan_pairs / pair_key).
Goldens: the committed S1 fixture — group (92, ascending) has 6 dates
(5 pairs), group (84, descending) has 2 dates (1 pair); the state change
between 2026-09-09 and 2026-09-21 must never be paired.
"""
import json
from pathlib import Path

from mirarsetena.coherence.pairs import pair_key, plan_pairs
from mirarsetena.pipeline.catalog import parse_scenes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _scenes():
    payload = json.loads((FIXTURES / "stac_s1_grd.json").read_text())
    return parse_scenes("sentinel-1-grd", payload)


def test_plans_six_pairs_from_fixture():
    pairs = plan_pairs(_scenes())
    assert len(pairs) == 6
    dated = {(p.first.date, p.second.date) for p in pairs}
    assert ("2026-07-11", "2026-07-23") in dated
    assert ("2026-08-28", "2026-09-09") in dated
    assert ("2026-09-21", "2026-10-03") in dated
    # state/orbit change on 2026-09-21 must not bridge
    assert ("2026-09-09", "2026-09-21") not in dated


def test_every_pair_shares_orbit_and_state():
    for pair in plan_pairs(_scenes()):
        assert pair.first.relative_orbit == pair.second.relative_orbit
        assert pair.first.orbit_state == pair.second.orbit_state


def test_pairs_are_sorted_and_keys_are_stable():
    pairs = plan_pairs(_scenes())
    assert [p.first.date for p in pairs] == sorted(p.first.date for p in pairs)
    first = pair_key(pairs[0])
    assert first.startswith("coherence:")
    assert first == pair_key(pairs[0])


def test_empty_input_yields_no_pairs():
    assert plan_pairs([]) == []

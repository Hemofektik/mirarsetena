"""Orbit-aware date catalog: persist viewing geometry per acquisition date.

The catalog already parses `sat:orbit_state` / `sat:relative_orbit`, but
refresh_index dropped them — so neither the baseline default nor the UI
could know that the default pair (2026-07-23 ascending vs 2026-10-03
descending) compares opposite look directions over mountainous terrain.
SCOPE pairing rule: same relative orbit + orbit state.

Seam: mirarsetena.pipeline.dates (refresh_index / list_dates /
default_baseline_date).
"""
import json
from datetime import date
from pathlib import Path

from mirarsetena.pipeline.catalog import Scene
from mirarsetena.pipeline.change import default_baseline_date
from mirarsetena.pipeline.dates import index_key, list_dates, refresh_index
from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.storage import LocalStore

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "projects"
CONFIG = ProjectRegistry(CONFIG_DIR).get("cdp-rio-general")


def _scene(day: str, state: str | None, rel: int | None) -> Scene:
    return Scene(
        id=f"S1X_IW_GRDH_1SDV_{day.replace('-', '')}T112229_2026100T120000_001",
        collection="sentinel-1-grd",
        datetime=f"{day}T11:22:29Z",
        date=day,
        cloud=None,
        assets={"vv": "https://example.invalid/vv.tif"},
        orbit_state=state,
        relative_orbit=rel,
    )


# The live shape: dual-track dates early on, ascending-only through the
# works start, descending-only for the two latest acquisitions.
SCENES = [
    _scene("2026-01-05", "ascending", 92),
    _scene("2026-01-05", "descending", 84),
    _scene("2026-04-11", "descending", 84),
    _scene("2026-05-05", "ascending", 92),
    _scene("2026-05-05", "descending", 84),
    _scene("2026-07-23", "ascending", 92),
    _scene("2026-09-09", "ascending", 92),
    _scene("2026-10-03", "descending", 84),
]


def _fake_search(collection, bbox, start, end):
    return SCENES if collection == "sentinel-1-grd" else []


def test_refresh_index_persists_orbit_geometry(tmp_path):
    storage = LocalStore(tmp_path)
    index = refresh_index(storage, CONFIG, search_fn=_fake_search)
    entries = {e["date"]: e for e in index["missions"]["sentinel-1-grd"]}
    assert entries["2026-10-03"]["orbits"] == [["descending", 84]]
    assert entries["2026-09-09"]["orbits"] == [["ascending", 92]]
    assert entries["2026-05-05"]["orbits"] == [
        ["ascending", 92],
        ["descending", 84],
    ]
    # schema version: pre-orbit indexes must re-refresh instead of being
    # treated as fresh for another 6h
    assert index.get("v") == 2
    stored = json.loads(storage.get(index_key(CONFIG.slug)))
    assert stored.get("v") == 2


def test_list_dates_passes_orbits_through(tmp_path):
    storage = LocalStore(tmp_path)
    refresh_index(storage, CONFIG, search_fn=_fake_search)
    result = list_dates(
        storage, CONFIG.slug, CONFIG, "sigma0",
        search_fn=_fake_search,
    )
    by_date = {e["date"]: e for e in result["dates"]}
    assert by_date["2026-10-03"]["orbits"] == [["descending", 84]]
    assert by_date["2026-01-05"]["orbits"] == [
        ["ascending", 92],
        ["descending", 84],
    ]


def test_default_baseline_prefers_pure_same_geometry():
    dates = [
        "2026-01-05", "2026-04-11", "2026-05-05",
        "2026-07-23", "2026-09-09", "2026-10-03",
    ]
    orbits = {
        "2026-01-05": [["ascending", 92], ["descending", 84]],
        "2026-04-11": [["descending", 84]],
        "2026-05-05": [["ascending", 92], ["descending", 84]],
        "2026-07-23": [["ascending", 92]],
        "2026-09-09": [["ascending", 92]],
        "2026-10-03": [["descending", 84]],
    }
    works = date(2026, 8, 1)
    # descending end: the pure descending pre-works date beats the newer
    # ascending one (geometry match over recency)
    assert (
        default_baseline_date(
            dates, works, end_date="2026-10-03", orbit_index=orbits
        )
        == "2026-04-11"
    )
    # ascending end: latest pure ascending pre-works date
    assert (
        default_baseline_date(
            dates, works, end_date="2026-09-09", orbit_index=orbits
        )
        == "2026-07-23"
    )


def test_default_baseline_falls_back_through_tiers_to_legacy():
    dates = ["2026-01-05", "2026-05-05", "2026-07-23", "2026-10-03"]
    works = date(2026, 8, 1)
    dual_only = {
        "2026-01-05": [["ascending", 92], ["descending", 84]],
        "2026-05-05": [["ascending", 92], ["descending", 84]],
        "2026-07-23": [["ascending", 92]],
        "2026-10-03": [["descending", 84]],
    }
    # no pure descending candidate: tier 2 accepts a date that CONTAINS
    # the end's geometry (dual-track date) over a mismatched one
    assert (
        default_baseline_date(
            dates, works, end_date="2026-10-03", orbit_index=dual_only
        )
        == "2026-05-05"
    )
    # no orbit information at all -> legacy behavior (latest pre-works)
    assert default_baseline_date(dates, works, end_date="2026-10-03") == "2026-07-23"
    assert (
        default_baseline_date(dates, works, end_date="2026-10-03", orbit_index={})
        == "2026-07-23"
    )
    # legacy contract without end/orbits stays untouched
    assert default_baseline_date(dates, works) == "2026-07-23"

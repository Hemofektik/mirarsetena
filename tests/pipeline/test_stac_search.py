"""D.1 — STAC search & scene grouping.

Seam: mirarsetena.pipeline.catalog (parse/group/search).
Golden values come from the committed fixtures recorded against the live
Earth Search catalog on 2026-10-07 for the CDP Río General bbox.
"""
import json
from pathlib import Path

import httpx
import pytest

from mirarsetena.pipeline.catalog import (
    CatalogError,
    group_by_date,
    parse_scenes,
    search,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BBOX = [-83.675, 9.378, -83.660, 9.397]


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_sentinel2_fixture():
    scenes = parse_scenes("sentinel-2-l2a", _load("stac_s2_l2a.json"))
    assert len(scenes) == 49
    assert len({s.date for s in scenes}) == 25
    by_id = {s.id: s for s in scenes}
    scene = by_id["S2C_16PHR_20261006_0_L2A"]
    assert scene.cloud == pytest.approx(44.522899)
    assert scene.date == "2026-10-06"
    assert {"blue", "green", "red", "nir", "swir16", "scl"} <= set(scene.assets)


def test_group_sentinel2_by_date_merges_dual_tiles():
    scenes = parse_scenes("sentinel-2-l2a", _load("stac_s2_l2a.json"))
    groups = group_by_date(scenes)
    assert len(groups) == 25
    assert [g.date for g in groups] == sorted(g.date for g in groups)
    assert groups[0].date == "2026-07-03"
    by_date = {g.date: g for g in groups}
    assert len(by_date["2026-10-01"].scenes) == 2  # dual-tile day
    assert len(by_date["2026-09-18"].scenes) == 1  # single-tile day
    assert len(by_date["2026-10-06"].scenes) == 2


def test_parse_sentinel1_fixture():
    scenes = parse_scenes("sentinel-1-grd", _load("stac_s1_grd.json"))
    assert len(scenes) == 8
    assert {s.date for s in scenes} == {
        "2026-07-11", "2026-07-23", "2026-08-04", "2026-08-16",
        "2026-08-28", "2026-09-09", "2026-09-21", "2026-10-03",
    }
    assert {s.relative_orbit for s in scenes} == {84, 92}
    assert {s.orbit_state for s in scenes} == {"ascending", "descending"}
    assert {s.datetime[11:16] for s in scenes} == {"23:47", "23:48", "11:22"}
    assert all({"vv", "vh"} <= set(s.assets) for s in scenes)
    assert all(s.cloud is None for s in scenes)
    assert len(group_by_date(scenes)) == 8


def test_search_issues_expected_request():
    payload = _load("stac_s2_l2a.json")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    scenes = search(
        "sentinel-2-l2a", BBOX, "2026-07-01", "2026-10-07", client=client
    )
    assert seen["path"] == "/v1/search"
    assert seen["body"]["collections"] == ["sentinel-2-l2a"]
    assert seen["body"]["bbox"] == BBOX
    assert seen["body"]["datetime"].startswith("2026-07-01")
    assert len(scenes) == 49


def test_search_wraps_http_failures():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )
    with pytest.raises(CatalogError):
        search("sentinel-2-l2a", BBOX, "2026-07-01", "2026-10-07", client=client)

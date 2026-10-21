"""L.9 — Plan vs. Realidad API: GET /p/{slug}/api/plan.

Seams: the FastAPI route, the project config's optional `plan_path`
(data/projects/<slug>/plan.json) and the storage-held per-date series
entries written beside each InSAR product. Timeline anchors come from the
config, milestones from the plan file, measured values from the series —
the three inputs the chart renders without inventing any of them.
"""
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.pipeline.insar import insar_series_key
from mirarsetena.storage import LocalStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"

SERIES_ENTRIES = [
    {"date": "2026-09-09", "los_range": 0.5, "pois": []},
    {
        "date": "2026-10-03",
        "los_range": 0.6,
        "pois": [{"id": "quarry", "los_cm": 0.05}],
    },
]


def make_client(tmp_path, config_dir=CONFIG_DIR):
    storage_root = tmp_path / "cache"
    store = LocalStore(storage_root)
    for entry in SERIES_ENTRIES:
        store.put(
            insar_series_key("cdp-rio-general", entry["date"]),
            json.dumps(entry).encode("utf-8"),
        )
    return TestClient(
        create_app(config_dir=config_dir, storage_root=storage_root)
    )


def _write_config(tmp_path, extra_lines=()):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = {
        "slug": "tmp-plan",
        "name": "Tmp",
        "aoi_path": "data/tmp/aoi.geojson",
        "pois_path": "data/tmp/pois.geojson",
        "bbox": [-83.7, 9.3, -83.6, 9.4],
        "layers": ["rgb"],
        "timeline": {"start": "2026-01-01", "works_start": "2026-06-01"},
        "cache": {
            "max_zoom": 18,
            "tile_budget_bytes": 1024,
            "scene_budget_bytes": 1024,
        },
        "satellite": {"s2_repeat_days": 5, "s1_repeat_days": 12},
    }
    for line in extra_lines:
        key, value = line.split("=", 1)
        config[key] = yaml.safe_load(value)
    (config_dir / "tmp-plan.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    return config_dir


def test_plan_returns_timeline_milestones_and_series(tmp_path):
    client = make_client(tmp_path)
    res = client.get("/p/cdp-rio-general/api/plan")
    assert res.status_code == 200
    body = res.json()
    assert body["timeline"] == {
        "start": "2026-04-14",
        "works_start": "2026-06-01",
    }
    # milestones come from the plan file (resolution TERCERO: works start
    # opens the first supervision-report period)
    dates = [m["date"] for m in body["milestones"]]
    assert "2026-06-01" in dates
    for milestone in body["milestones"]:
        assert milestone["es"] and milestone["en"]
    # series sorted ascending, payload passed through untouched
    assert body["series"] == SERIES_ENTRIES


def test_plan_unknown_project_is_404(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/p/no-such-project/api/plan").status_code == 404


def test_plan_without_plan_path_has_empty_milestones(tmp_path):
    config_dir = _write_config(tmp_path)
    client = make_client(tmp_path, config_dir=config_dir)
    res = client.get("/p/tmp-plan/api/plan")
    assert res.status_code == 200
    body = res.json()
    assert body["milestones"] == []
    # no series written for this project's namespace -> empty, still 200
    assert body["series"] == []
    assert body["timeline"]["works_start"] == "2026-06-01"


def test_plan_configured_but_missing_file_is_404(tmp_path):
    config_dir = _write_config(
        tmp_path, extra_lines=["plan_path=data/tmp/plan.json"]
    )
    client = make_client(tmp_path, config_dir=config_dir)
    res = client.get("/p/tmp-plan/api/plan")
    assert res.status_code == 404


def test_plan_stack_groups_series_by_chain(tmp_path):
    # SBAS aggregation: per-chain mean/sem per POI — S1A and S1D can never
    # be phase-tied, so their series stay separate to the stack summary.
    import json

    from mirarsetena.pipeline.insar import insar_series_key
    from mirarsetena.storage import LocalStore

    store = LocalStore(tmp_path / "cache")
    entries = [
        ("2026-03-06", "S1A-asc", [{"id": "quarry", "los_cm": -0.12}]),
        ("2026-03-18", "S1A-asc", [{"id": "quarry", "los_cm": 0.08}]),
        ("2026-08-04", "S1D-asc", [{"id": "quarry", "los_cm": 0.05}]),
    ]
    for date, chain, pois in entries:
        store.put(
            insar_series_key("cdp-rio-general", date),
            json.dumps({"date": date, "los_range": 0.7, "pois": pois,
                        "chain": chain}).encode(),
        )
    app = create_app(config_dir=CONFIG_DIR, storage_root=tmp_path / "cache")
    body = TestClient(app).get("/p/cdp-rio-general/api/plan").json()
    stack = {s["chain"]: s for s in body["stack"]}
    assert set(stack) == {"S1A-asc", "S1D-asc"}
    s1a = stack["S1A-asc"]
    assert s1a["n"] == 2 and s1a["start"] == "2026-03-06"
    assert s1a["end"] == "2026-03-18"
    quarry = next(p for p in s1a["pois"] if p["id"] == "quarry")
    assert quarry["mean"] == pytest.approx(-0.02)
    assert quarry["sem"] == pytest.approx(0.1)  # std/sqrt(2) = 0.1
    # a single-date chain has no spread to report
    single = stack["S1D-asc"]
    q2 = next(p for p in single["pois"] if p["id"] == "quarry")
    assert q2["mean"] == pytest.approx(0.05)
    assert q2["sem"] is None


def test_plan_serves_the_sbas_cumulative_series(tmp_path):
    # The SBAS inversion stores per-POI cumulative series; /api/plan
    # surfaces them so the chart can draw the trajectory line.
    import json

    from mirarsetena.storage import LocalStore

    store = LocalStore(tmp_path / "cache")
    store.put(
        "p/cdp-rio-general/sbas/series-S1D-asc.json",
        json.dumps({
            "chain": "S1D-asc", "method": "sbas",
            "dates": ["2026-07-11", "2026-07-23", "2026-08-04"],
            "pois": [
                {"id": "quarry", "los_cm": [0.0, 0.11, 0.21]},
                {"id": "office", "los_cm": [0.0, 0.25, 0.31]},
            ],
        }).encode(),
    )
    app = create_app(config_dir=CONFIG_DIR, storage_root=tmp_path / "cache")
    body = TestClient(app).get("/p/cdp-rio-general/api/plan").json()
    assert len(body["sbas"]) == 1
    entry = body["sbas"][0]
    assert entry["chain"] == "S1D-asc"
    assert entry["dates"] == ["2026-07-11", "2026-07-23", "2026-08-04"]
    quarry = next(p for p in entry["pois"] if p["id"] == "quarry")
    assert quarry["los_cm"] == [0.0, 0.11, 0.21]
    # no sbas files -> empty list, still 200
    app2 = create_app(config_dir=CONFIG_DIR, storage_root=tmp_path / "other")
    body2 = TestClient(app2).get("/p/cdp-rio-general/api/plan").json()
    assert body2["sbas"] == []

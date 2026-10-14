"""L.9 — data-prep tool: POI loading for the plan-vs-actual series.

tools/insar_product is not run in CI (it needs the spike's on-disk
products), but its POI loader is pure config/GeoJSON reading and pins the
contract the series writer depends on: one sample point per POI feature
with a stable `id` and WGS84 coordinates.
"""
from pathlib import Path

from tools.insar_product import load_pois

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "projects"


def test_load_pois_reads_project_points_with_ids():
    pois = load_pois("cdp-rio-general", config_dir=CONFIG_DIR)
    ids = [p["id"] for p in pois]
    # the plan-vs-actual chart defaults to the excavation POI
    assert "quarry" in ids
    assert "project-start" in ids
    assert len(ids) == len(set(ids))  # stable unique keys for the series
    for poi in pois:
        lon, lat = poi["coordinates"]
        assert -83.68 < lon < -83.65
        assert 9.37 < lat < 9.41

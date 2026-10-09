"""C.1 — config-driven project registry.

Seam: mirarsetena.projects.registry (ProjectRegistry load/lookup contract and
ProjectConfig schema), exercised against the real project configuration.
"""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mirarsetena.projects.registry import ProjectNotFound, ProjectRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"


def test_loads_the_real_project_configuration():
    registry = ProjectRegistry(CONFIG_DIR)
    cfg = registry.get("cdp-rio-general")
    assert cfg.slug == "cdp-rio-general"
    assert cfg.name == "CDP Río General"
    assert cfg.bbox == (-83.675, 9.378, -83.660, 9.397)
    assert cfg.timeline.start.isoformat() == "2026-01-01"
    assert cfg.timeline.works_start.isoformat() == "2026-06-01"
    assert {"rgb", "ndvi", "mndwi", "bsi", "sigma0", "coherence"} == set(cfg.layers)
    assert cfg.cache.max_zoom == 18
    assert cfg.cache.tile_budget_bytes == 10 * 1024**3
    assert cfg.cache.scene_budget_bytes == 10 * 1024**3
    assert cfg.aoi_path.endswith("aoi.geojson")
    assert cfg.satellite.s2_repeat_days == 5
    assert cfg.satellite.s1_repeat_days == 12


def test_unknown_slug_raises_project_not_found():
    registry = ProjectRegistry(CONFIG_DIR)
    with pytest.raises(ProjectNotFound) as excinfo:
        registry.get("does-not-exist")
    assert "does-not-exist" in str(excinfo.value)


def _minimal_project(slug: str) -> dict:
    return {
        "slug": slug,
        "name": slug,
        "aoi_path": "data/x/aoi.geojson",
        "pois_path": "data/x/pois.geojson",
        "bbox": [-83.68, 9.37, -83.66, 9.40],
        "layers": ["ndvi"],
        "timeline": {"start": "2026-07-01", "works_start": "2026-08-01"},
        "cache": {
            "max_zoom": 18,
            "tile_budget_bytes": 2147483648,
            "scene_budget_bytes": 2147483648,
        },
    }


def _write(tmp_path: Path, project: dict) -> Path:
    path = tmp_path / f"{project['slug']}.yaml"
    path.write_text(yaml.safe_dump(project), encoding="utf-8")
    return path


def test_missing_required_field_fails_validation(tmp_path):
    project = _minimal_project("broken")
    del project["timeline"]
    _write(tmp_path, project)
    with pytest.raises(ValidationError):
        ProjectRegistry(tmp_path)


def test_bbox_out_of_order_fails_validation(tmp_path):
    project = _minimal_project("reversed")
    project["bbox"] = [-83.66, 9.40, -83.68, 9.37]  # west > east, south > north
    _write(tmp_path, project)
    with pytest.raises(ValidationError):
        ProjectRegistry(tmp_path)


def test_routes_and_cache_keys_namespace_per_project():
    from mirarsetena.projects.registry import cache_key, route_prefix

    assert route_prefix("cdp-rio-general") == "/p/cdp-rio-general"
    assert route_prefix("otro-proyecto") == "/p/otro-proyecto"
    assert route_prefix("cdp-rio-general") != route_prefix("otro-proyecto")

    parts = ("tiles", "ndvi", "2026-07-03", "14/4621/6492.png")
    key_a = cache_key("cdp-rio-general", *parts)
    key_b = cache_key("otro-proyecto", *parts)
    assert key_a == "p/cdp-rio-general/tiles/ndvi/2026-07-03/14/4621/6492.png"
    assert key_a != key_b  # identical part sequences stay project-isolated

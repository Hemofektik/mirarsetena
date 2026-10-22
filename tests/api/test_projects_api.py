"""Bare-domain boot: the default project list (user 2026-10-22).

The SPA served at a bare domain (ms.hemofektik.de/) has no /p/{slug}/
path segment to parse, so it asks the backend which project to boot.

Seam: GET /projects.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from mirarsetena.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"
SLUG = "cdp-rio-general"


def _client(tmp_path, config_dir=CONFIG_DIR) -> TestClient:
    app = create_app(config_dir=config_dir, storage_root=tmp_path / "cache")
    return TestClient(app)


def test_projects_lists_every_project_and_names_a_default(tmp_path):
    body = _client(tmp_path).get("/projects").json()
    slugs = [project["slug"] for project in body["projects"]]
    assert SLUG in slugs
    assert body["default"] in slugs
    # names ride along so a future project picker can label itself
    entry = next(p for p in body["projects"] if p["slug"] == SLUG)
    assert entry["name"] == "CDP Río General"


def test_projects_never_returns_an_empty_default(tmp_path):
    # no config dir -> the SPA must fail loudly, not boot a ghost project
    body = _client(tmp_path, config_dir=tmp_path / "empty").get("/projects")
    assert body.status_code == 200
    assert body.json() == {"default": None, "projects": []}

"""Frontend hosting: the built SPA is served by the same origin (I.1).

The dist build is produced by `npm run build` (CI does this before pytest);
the test skips on a bare checkout without a built frontend.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mirarsetena.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"
DIST = REPO_ROOT / "frontend" / "dist"
SLUG = "cdp-rio-general"

pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file(),
    reason="frontend/dist missing — run `npm run build` in frontend/",
)


def _client(tmp_path) -> TestClient:
    app = create_app(config_dir=CONFIG_DIR, storage_root=tmp_path / "cache")
    return TestClient(app)


def test_project_entry_serves_the_spa(tmp_path):
    client = _client(tmp_path)
    response = client.get(f"/p/{SLUG}/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Mirar Setena</title>" in response.text
    assert 'id="map"' in response.text


def test_built_assets_are_served(tmp_path):
    client = _client(tmp_path)
    index = client.get(f"/p/{SLUG}/").text
    marker = '/assets/'
    start = index.find(marker)
    assert start != -1, "index.html references a hashed /assets/ bundle"
    end = index.find('"', start)
    asset_path = index[start:end]
    asset = client.get(asset_path)
    assert asset.status_code == 200
    assert "javascript" in asset.headers.get("content-type", "")


def test_api_routes_still_win_and_unknown_paths_404(tmp_path):
    client = _client(tmp_path)
    config = client.get(f"/p/{SLUG}/config")
    assert config.status_code == 200
    assert config.headers["content-type"].startswith("application/json")

    assert client.get("/p/unknown/").status_code == 404
    assert client.get("/definitely-not-a-route").status_code == 404

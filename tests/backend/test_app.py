"""C.3 — FastAPI app skeleton: app factory, healthz, project-scoped routes.

Seam: mirarsetena.app.create_app (mounted routes + state wiring).
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mirarsetena.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(
        config_dir=CONFIG_DIR,
        storage_root=tmp_path_factory.mktemp("cache"),
    )
    return TestClient(app)


def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_project_config_route_serves_the_loaded_project(client):
    response = client.get("/p/cdp-rio-general/config")
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "cdp-rio-general"
    assert body["timeline"]["works_start"] == "2026-08-01"
    assert body["cache"]["max_zoom"] == 18


def test_unknown_project_returns_404(client):
    response = client.get("/p/does-not-exist/config")
    assert response.status_code == 404


def test_state_wiring_exposes_registry_and_storage(client):
    app = client.app
    assert "cdp-rio-general" in app.state.registry.all()
    assert app.state.storage is not None

"""G.2 — catalog poll job + scheduled poller (SCOPE R2-Q5 hybrid freshness).

Seams: mirarsetena.jobs.poller (handler/poll_once/lifespan wiring).
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.jobs.poller import make_catalog_poll_handler, poll_once
from mirarsetena.jobs.runner import JobQueue
from mirarsetena.pipeline.catalog import CatalogError
from mirarsetena.pipeline.dates import index_key
from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.storage import LocalStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"


def test_catalog_poll_job_writes_index(tmp_path, fake_search):
    storage = LocalStore(tmp_path / "cache")
    registry = ProjectRegistry(CONFIG_DIR)
    queue = JobQueue(tmp_path / "jobs.db")
    handler = make_catalog_poll_handler(registry, storage, search_fn=fake_search)

    assert queue.enqueue(
        "catalog_poll", {"slug": "cdp-rio-general"},
        dedupe="catalog_poll:cdp-rio-general",
    )
    assert queue.run_once({"catalog_poll": handler}) == "done"

    index = json.loads(storage.get(index_key("cdp-rio-general")))
    assert index["updated_at"]
    assert len(index["missions"]["sentinel-2-l2a"]) == 25
    assert len(index["missions"]["sentinel-1-grd"]) == 8
    assert index["missions"].get("sentinel-1-slc", []) == []


def test_poll_once_enqueues_every_project_idempotently(tmp_path):
    registry = ProjectRegistry(CONFIG_DIR)
    queue = JobQueue(tmp_path / "jobs.db")
    assert poll_once(registry, queue) == 1
    assert poll_once(registry, queue) == 0  # dedupe
    assert queue.counts() == {"pending": 1}


def test_catalog_failure_degrades_to_empty_missions(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    registry = ProjectRegistry(CONFIG_DIR)
    queue = JobQueue(tmp_path / "jobs.db")

    def broken_search(collection, bbox, start, end):
        raise CatalogError("catalog down")

    handler = make_catalog_poll_handler(registry, storage, search_fn=broken_search)
    queue.enqueue("catalog_poll", {"slug": "cdp-rio-general"}, dedupe="x")
    assert queue.run_once({"catalog_poll": handler}) == "done"
    index = json.loads(storage.get(index_key("cdp-rio-general")))
    assert index["missions"]["sentinel-2-l2a"] == []


def test_lifespan_starts_the_poller(tmp_path, fake_search):
    app = create_app(
        config_dir=CONFIG_DIR,
        storage_root=tmp_path / "cache",
        search_fn=fake_search,
    )
    with TestClient(app) as client:
        assert client.app.state.poller is not None
        assert not client.app.state.poller.done() or client.app.state.poller.cancelled()
        assert client.get("/healthz").status_code == 200

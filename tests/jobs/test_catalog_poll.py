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
    # radar layers left the config (2026-10-12): no S1 mission to index
    assert "sentinel-1-grd" not in index["missions"]
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


def test_poller_registers_insar_pair_handler(tmp_path):
    """Gap #3 (INSAR-PLAN): coherence/insar pair jobs were enqueued by the
    trigger but never registered in the poller, so they parked forever. The
    cycle must expose an insar_pair handler so the queue can drain them."""
    from mirarsetena.jobs.poller import insar_handlers

    registry = ProjectRegistry(CONFIG_DIR)
    storage = LocalStore(tmp_path / "cache")
    handlers = insar_handlers(registry, storage, search_fn=fake_search_stub)
    assert "insar_pair" in handlers
    assert "insar_eager" in handlers
    assert callable(handlers["insar_pair"])


def fake_search_stub(collection, bbox, start, end):
    return []


def test_poll_cycle_appends_new_insar_pairs_after_first_view(tmp_path, fake_search):
    """INSAR-PLAN cadence: after the first view's eager job, each poll cycle
    discovers newly published SLC scenes (via GRD metadata) and appends one
    insar_pair job per not-yet-queued pair. Before the first view nothing is
    appended (the eager job owns the backlog)."""
    from mirarsetena.coherence.trigger import insar_viewed_flag_key
    from mirarsetena.jobs.poller import cycle

    storage = LocalStore(tmp_path / "cache")
    registry = ProjectRegistry(CONFIG_DIR)
    queue = JobQueue(tmp_path / "jobs.db")

    class _App:
        state = type("S", (), {})()

    app = _App()
    app.state.registry = registry
    app.state.storage = storage
    app.state.queue = queue
    app.state.search_fn = fake_search

    # before the first view: no insar_pair jobs (eager hasn't fired yet)
    cycle(app)
    assert queue.last_job("insar_pair") is None

    # first view marks the project as viewed (as /api/dates does)
    storage.put(
        insar_viewed_flag_key("cdp-rio-general"),
        b'{"viewed_at": "2026-10-12T00:00:00+00:00"}',
    )
    cycle(app)
    # 6 pairs from the GRD fixture, one job each — drained in the same cycle
    # and failed honestly (no processor/CDSE in the test env), never parked
    counts = queue.counts()
    assert counts.get("failed", 0) == 6
    assert queue.last_job("insar_pair") is not None

    # a second cycle adds nothing (dedupe: the pairs are already queued)
    cycle(app)
    assert queue.counts().get("failed", 0) == 6

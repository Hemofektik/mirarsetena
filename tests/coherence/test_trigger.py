"""H.4 — trigger policy (SCOPE R2-Q1 / IMPLEMENTATION H.4).

The first plain-data view starts ONE eager job covering the known window;
afterwards each newly discovered SLC scene appends exactly one pair job.
Pair handlers are idempotent (skip when the result is already cached).
"""
import json
from pathlib import Path

import pytest

from mirarsetena.coherence.pipeline import (
    make_eager_handler,
    make_pair_handler,
    pair_to_payload,
)
from mirarsetena.coherence.pairs import plan_pairs
from mirarsetena.coherence.trigger import (
    enqueue_new_pairs,
    on_plain_data_view,
)
from mirarsetena.jobs.runner import JobQueue
from mirarsetena.pipeline.catalog import parse_scenes
from mirarsetena.storage import LocalStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BBOX = (-83.675, 9.378, -83.660, 9.397)


def _pairs():
    payload = json.loads((FIXTURES / "stac_s1_grd.json").read_text())
    return plan_pairs(parse_scenes("sentinel-1-grd", payload))


def test_first_view_enqueues_one_eager_job_then_marks_viewed(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    queue = JobQueue(tmp_path / "jobs.db")

    assert on_plain_data_view(storage, "proj-a", _pairs(), queue) is True
    assert queue.counts() == {"pending": 1}
    assert queue.last_job("coherence_eager")["status"] == "pending"

    assert on_plain_data_view(storage, "proj-a", _pairs(), queue) is False
    assert queue.counts() == {"pending": 1}  # no second eager job


def test_new_pairs_append_exactly_once(tmp_path):
    queue = JobQueue(tmp_path / "jobs.db")
    pairs = _pairs()
    assert enqueue_new_pairs("proj-a", pairs, queue) == 6
    assert enqueue_new_pairs("proj-a", pairs, queue) == 0
    assert queue.counts() == {"pending": 6}


def _fakes(created: list, processed: list):
    def downloader(pair, workdir):
        paths = []
        for scene in (pair.first, pair.second):
            path = workdir / f"{scene.id}.zip"
            path.write_bytes(b"SLC" * 10)
            paths.append(path)
        created.append(workdir)
        return paths

    def processor(paths, bbox):
        processed.append(len(paths))
        return b"COHERENCE"

    return downloader, processor


def test_eager_handler_computes_all_pairs_and_purges(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    queue = JobQueue(tmp_path / "jobs.db")
    created, processed = [], []
    downloader, processor = _fakes(created, processed)

    on_plain_data_view(storage, "proj-a", _pairs(), queue)
    handler = make_eager_handler(
        storage, "proj-a", BBOX,
        downloader=downloader, processor=processor, workdir=tmp_path / "work",
    )
    job = queue.pending()
    assert queue.run_once({"coherence_eager": handler}) == "done"

    assert processed == [2] * 6  # all six pairs computed
    assert len(storage.list("p/proj-a/coherence/")) == 6
    assert all(not workdir.exists() for workdir in created)
    assert json.loads(job["payload"])["pairs"]  # pairs travelled in the payload


def test_pair_handler_skips_already_computed_results(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    created, processed = [], []
    downloader, processor = _fakes(created, processed)
    handler = make_pair_handler(
        storage, "proj-a", BBOX,
        downloader=downloader, processor=processor, workdir=tmp_path / "work",
    )
    pair = _pairs()[0]
    payload = {"pair": pair_to_payload(pair)}

    handler(payload)
    assert processed == [2]
    handler(payload)  # cached result -> skipped entirely
    assert processed == [2]
    assert created[0].exists() is False

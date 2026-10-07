"""G.1 — durable job runner (SQLite): enqueue, run, retry, dedupe.

Seam: mirarsetena.jobs.runner.JobQueue.
"""
from mirarsetena.jobs.runner import MAX_ATTEMPTS, JobQueue


def _queue(tmp_path) -> JobQueue:
    return JobQueue(tmp_path / "jobs.db")


def test_enqueue_run_done(tmp_path):
    queue = _queue(tmp_path)
    assert queue.enqueue("demo", {"value": 1}, dedupe="demo:1") is True
    ran = []

    def handler(payload):
        ran.append(payload["value"])

    outcome = queue.run_once({"demo": handler})
    assert outcome == "done"
    assert ran == [1]
    assert queue.counts() == {"done": 1}
    assert queue.pending() is None


def test_failing_job_retries_then_fails(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue("boom", {}, dedupe="boom:1")

    def handler(payload):
        raise RuntimeError("kaput")

    for _ in range(MAX_ATTEMPTS - 1):
        assert queue.run_once({"boom": handler}) == "pending"
    assert queue.run_once({"boom": handler}) == "failed"
    job = queue.last_job("boom")
    assert job["attempts"] == MAX_ATTEMPTS
    assert "kaput" in job["last_error"]


def test_dedupe_makes_enqueue_idempotent(tmp_path):
    queue = _queue(tmp_path)
    assert queue.enqueue("demo", {}, dedupe="same") is True
    assert queue.enqueue("demo", {}, dedupe="same") is False
    assert queue.counts().get("pending") == 1
    queue.run_once({"demo": lambda payload: None})
    # already done -> still idempotent
    assert queue.enqueue("demo", {}, dedupe="same") is False
    assert queue.counts() == {"done": 1}


def test_run_once_on_empty_queue_returns_none(tmp_path):
    assert _queue(tmp_path).run_once({}) is None


def test_jobs_without_handlers_are_parked_not_failed(tmp_path):
    """Coherence jobs must wait for their processor instead of failing
    when only the catalog handler is registered."""
    queue = _queue(tmp_path)
    queue.enqueue("coherence_eager", {"slug": "x"}, dedupe="e")
    assert queue.run_once({}) is None
    assert queue.counts() == {"pending": 1}  # parked, not failed

    queue.enqueue("demo", {}, dedupe="d")
    assert queue.run_once({"demo": lambda payload: None}) == "done"
    assert queue.counts() == {"pending": 1, "done": 1}

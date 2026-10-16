"""CDSE download resilience: resume + retry (measured failure 2026-10-15).

A 7 GB SLC stream stalled 23 minutes in (httpx.ReadTimeout) and the whole
pair failed — fatal for a future 25+ pair stack. Seam: download_product's
transport, injected as a fake client; the file on disk is the resume
state. Not run in CI beyond these fakes (no network).
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mirarsetena.coherence.cdse import CoherenceUnavailable
from mirarsetena.coherence.download import download_product, is_valid_zip

FULL = bytes(range(256)) * 8  # 2048 bytes


class FakeResponse:
    def __init__(self, status_code, body=b"", fail_after=None, exc=None,
                 headers=None):
        self.status_code = status_code
        self._body = body
        self._fail_after = fail_after
        self._exc = exc
        self.headers = dict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self):
        if self._fail_after is None:
            yield self._body
            return
        yield self._body[: self._fail_after]
        raise self._exc


class FakeClient:
    """stream(method, url, headers=..., timeout=...) -> scripted responses."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def stream(self, method, url, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {})})
        return self.script.pop(0)


class FakeAuth:
    enabled = True
    client = None

    def token(self):  # the real Authorization header is built from this
        return "fake-token"


def _auth():
    return FakeAuth()


def test_resumes_from_partial_with_range_after_a_stall(tmp_path):
    dest = tmp_path / "scene.zip"
    client = FakeClient(
        [
            FakeResponse(200, FULL, fail_after=400, exc=httpx.ReadTimeout("stall")),
            FakeResponse(206, FULL[400:]),
        ]
    )
    sleeps = []
    out = download_product(_auth(), "42", dest, client=client, sleep=sleeps.append)
    assert out == dest
    assert dest.read_bytes() == FULL  # complete despite the stall
    assert len(client.calls) == 2
    assert "Range" not in client.calls[0]["headers"]  # first attempt: no offset
    assert client.calls[1]["headers"]["Range"] == "bytes=400-"
    assert len(sleeps) == 1  # one backoff between the attempts


def test_server_ignoring_range_restarts_from_zero(tmp_path):
    dest = tmp_path / "scene.zip"
    client = FakeClient(
        [
            FakeResponse(200, FULL, fail_after=300, exc=httpx.ReadTimeout("stall")),
            FakeResponse(200, FULL),  # 200, not 206: restart cleanly
        ]
    )
    download_product(_auth(), "42", dest, client=client, sleep=lambda _s: None)
    assert dest.read_bytes() == FULL  # no duplicated prefix


def test_keeps_partial_and_raises_after_retries_exhausted(tmp_path):
    dest = tmp_path / "scene.zip"
    stalls = lambda: httpx.ReadTimeout("stall")
    client = FakeClient(
        [
            FakeResponse(200, FULL, fail_after=400, exc=stalls()),
            # resume attempts answer 206 (Range honored) and keep stalling
            FakeResponse(206, FULL[400:800], fail_after=400, exc=stalls()),
            FakeResponse(206, FULL[800:1200], fail_after=400, exc=stalls()),
            FakeResponse(206, FULL[1200:1600], fail_after=400, exc=stalls()),
        ]
    )
    with pytest.raises(CoherenceUnavailable, match="attempts"):
        download_product(_auth(), "42", dest, client=client, sleep=lambda _s: None)
    # the partial survives: the NEXT run resumes at byte 1600, not 0
    assert dest.read_bytes() == FULL[:1600]

def test_http_error_is_not_retried(tmp_path):
    dest = tmp_path / "scene.zip"
    client = FakeClient([FakeResponse(403, b"")])
    with pytest.raises(CoherenceUnavailable, match="403"):
        download_product(_auth(), "42", dest, client=client, sleep=lambda _s: None)
    assert len(client.calls) == 1


def test_416_on_resume_means_the_file_is_complete(tmp_path):
    # Measured 2026-10-15: the first attempt streamed every byte but the
    # connection dropped before EOF; the resume Range then hit 416 — the
    # RFC's answer for an offset at the end of the object. That means the
    # partial IS the whole file, not an error.
    dest = tmp_path / "scene.zip"
    client = FakeClient(
        [
            FakeResponse(200, FULL, fail_after=len(FULL), exc=httpx.ReadTimeout("eof stall")),
            FakeResponse(416, b""),
            # the probe confirms the offset IS the end of the object
            FakeResponse(206, b"x", headers={"Content-Range": f"bytes 0-0/{len(FULL)}"}),
        ]
    )
    out = download_product(_auth(), "42", dest, client=client, sleep=lambda _s: None)
    assert out == dest
    assert dest.read_bytes() == FULL


def test_cache_partial_survives_a_failed_run(monkeypatch, tmp_path):
    # The cache wrapper used to DELETE the .part on failure, throwing away
    # gigabytes the next run could resume (the 03-18 scene lost 7.6 GB).
    from mirarsetena.coherence import download as dl
    from mirarsetena.coherence.download import make_slc_downloader
    from mirarsetena.coherence.pairs import InterferometricPair
    from mirarsetena.pipeline.catalog import Scene

    cache = tmp_path / "cache"
    cache.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    monkeypatch.setattr(dl, "find_slc_product", lambda auth, sid: {"id": "x", "name": "S.zip"})

    def failing(auth, product_id, dest, **kw):
        Path(dest).write_bytes(b"partial-bytes")
        raise CoherenceUnavailable("stall")

    monkeypatch.setattr(dl, "download_product", failing)
    scene = Scene(
        id="S1A_TEST_20260101T000000_20260101T000030_0_X_X",
        collection="sentinel-1-grd",
        datetime="2026-01-01T00:00:00Z",
        date="2026-01-01",
        cloud=None,
    )
    scene2 = Scene(
        id="S1A_TEST_20260113T000000_20260113T000030_0_X_X",
        collection="sentinel-1-grd",
        datetime="2026-01-13T00:00:00Z",
        date="2026-01-13",
        cloud=None,
    )
    pair = InterferometricPair(first=scene, second=scene2)
    downloader = make_slc_downloader(_auth(), cache_dir=cache, budget_bytes=10**12)
    with pytest.raises(CoherenceUnavailable):
        downloader(pair, work)
    partials = list(cache.glob("*.part"))
    assert len(partials) == 1
    assert partials[0].read_bytes() == b"partial-bytes"


def test_truncated_stream_is_detected_and_resumed(tmp_path):
    # Measured 2026-10-16: the CDN closed the connection cleanly after
    # 1.56 GB of a 7.66 GB SLC — httpx saw a COMPLETE stream, no
    # exception, and the truncated file passed as done. The response's
    # Content-Length is the only truth that catches it.
    dest = tmp_path / "scene.zip"
    client = FakeClient(
        [
            FakeResponse(200, FULL[:400], headers={"Content-Length": str(len(FULL))}),
            FakeResponse(206, FULL[400:], headers={"Content-Range": f"bytes 400-{len(FULL)-1}/{len(FULL)}"}),
        ]
    )
    out = download_product(_auth(), "42", dest, client=client, sleep=lambda _s: None)
    assert out == dest
    assert dest.read_bytes() == FULL
    assert len(client.calls) == 2


def test_416_before_the_end_restarts_from_zero(tmp_path):
    # A spurious 416 with the file only PARTIAL must not finalize: the
    # total size is probed (Range: bytes=0-0) and the download restarts.
    dest = tmp_path / "scene.zip"
    client = FakeClient(
        [
            FakeResponse(200, FULL[:400], fail_after=400, exc=httpx.ReadTimeout("stall")),
            FakeResponse(416, b""),                              # spurious
            FakeResponse(206, b"x", headers={"Content-Range": f"bytes 0-0/{len(FULL)}"}),  # probe
            FakeResponse(200, FULL),                             # restart
        ]
    )
    download_product(_auth(), "42", dest, client=client, sleep=lambda _s: None)
    assert dest.read_bytes() == FULL
    assert client.calls[1]["headers"].get("Range") == "bytes=400-"
    assert client.calls[2]["headers"].get("Range") == "bytes=0-0"


def test_integrity_validate_catches_chunked_truncation(tmp_path):
    # CDSE streams some products chunked — no Content-Length, so the size
    # check has nothing to compare (measured: 07-23 truncated at 2.11 GB
    # and passed). With a zip integrity validator the incomplete archive
    # fails and the download resumes/restarts until it is a real zip.
    import io
    import zipfile

    def zip_bytes(payload=b"data"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("inner.txt", payload)
        return buf.getvalue()

    good = zip_bytes()
    dest = tmp_path / "scene.zip"
    client = FakeClient(
        [
            FakeResponse(200, good[:40]),  # truncated zip, NO size headers
            FakeResponse(200, good),       # server restarts on retry
        ]
    )
    download_product(
        _auth(), "42", dest, client=client, sleep=lambda _s: None,
        validate=is_valid_zip,
    )
    assert dest.read_bytes() == good
    assert is_valid_zip(dest)
    assert len(client.calls) == 2


def test_401_refreshes_the_token_and_retries(tmp_path):
    # A 10-minute token expires mid-download: the NEXT attempt must carry
    # a FRESH Authorization (auth.token() re-fetches past its expiry),
    # not the stale header captured before the loop (measured: pair 19
    # died on 401 after 17 minutes).
    dest = tmp_path / "scene.zip"
    auth = FakeAuth()
    tokens = iter(["tok-1", "tok-2"])
    auth.token = lambda: next(tokens)
    client = FakeClient(
        [
            FakeResponse(401, b""),
            FakeResponse(200, FULL),
        ]
    )
    download_product(auth, "42", dest, client=client, sleep=lambda _s: None)
    assert dest.read_bytes() == FULL
    first = client.calls[0]["headers"]["Authorization"]
    second = client.calls[1]["headers"]["Authorization"]
    assert first != second  # the retry carried the refreshed token


def test_416_with_unknown_total_restarts_clean(tmp_path):
    # Measured 2026-10-16: CDSE serves some products chunked AND 416s on
    # any Range — resume is impossible for them. A failed probe must NOT
    # finalize the partial (that passed a 3.07 GB truncation as done and
    # burned three relaunches): the file is discarded and the download
    # restarts clean.
    dest = tmp_path / "scene.zip"
    client = FakeClient(
        [
            FakeResponse(200, FULL[:400], fail_after=400, exc=httpx.ReadTimeout("stall")),
            FakeResponse(416, b""),                     # Range unsupported
            FakeResponse(500, b""),                     # probe fails -> unknown
            FakeResponse(200, FULL),                    # clean restart
        ]
    )
    out = download_product(_auth(), "42", dest, client=client, sleep=lambda _s: None)
    assert dest.read_bytes() == FULL
    # the partial was discarded, not finalized
    assert len(client.calls) == 4
    assert "Range" not in client.calls[3]["headers"]

"""CDSE download resilience: resume + retry (measured failure 2026-10-15).

A 7 GB SLC stream stalled 23 minutes in (httpx.ReadTimeout) and the whole
pair failed — fatal for a future 25+ pair stack. Seam: download_product's
transport, injected as a fake client; the file on disk is the resume
state. Not run in CI beyond these fakes (no network).
"""
from __future__ import annotations

import httpx
import pytest

from mirarsetena.coherence.cdse import CoherenceUnavailable
from mirarsetena.coherence.download import download_product

FULL = bytes(range(256)) * 8  # 2048 bytes


class FakeResponse:
    def __init__(self, status_code, body=b"", fail_after=None, exc=None):
        self.status_code = status_code
        self._body = body
        self._fail_after = fail_after
        self._exc = exc

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

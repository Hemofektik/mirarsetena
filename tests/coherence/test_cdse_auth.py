"""H.1 — CDSE OAuth client (coherence layer only; core works without it).

Seam: mirarsetena.coherence.cdse.CdseAuth.
"""
import json
from pathlib import Path

import httpx
import pytest

from mirarsetena.coherence.cdse import CdseAuth, CoherenceUnavailable
from mirarsetena.storage import LocalStore

TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
    "/protocol/openid-connect/token"
)


def _auth(tmp_path, *, client_id="cid", client_secret="sec", clock=None):
    storage = LocalStore(tmp_path / "cache")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        assert body["grant_type"] == "client_credentials"
        assert body["client_id"] == "cid"
        return httpx.Response(
            200, json={"access_token": f"tok-{calls['n']}", "expires_in": 600}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    auth = CdseAuth(
        client_id, client_secret,
        storage=storage, client=client,
        token_url=TOKEN_URL, now=clock or (lambda: 1_000_000.0),
    )
    return auth, calls, storage


def test_token_fetched_once_then_cached(tmp_path):
    auth, calls, storage = _auth(tmp_path)
    assert auth.token() == "tok-1"
    assert auth.token() == "tok-1"
    assert calls["n"] == 1
    cached = json.loads(storage.get("system/cdse_token.json"))
    assert cached["access_token"] == "tok-1"


def test_expired_token_is_refreshed(tmp_path):
    clock = {"t": 1_000_000.0}
    auth, calls, _ = _auth(tmp_path, clock=lambda: clock["t"])
    assert auth.token() == "tok-1"
    clock["t"] += 601  # beyond expires_in=600 + skew margin
    assert auth.token() == "tok-2"
    assert calls["n"] == 2


def test_missing_credentials_reports_unavailable(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    auth = CdseAuth(None, None, storage=storage)
    assert auth.enabled is False
    with pytest.raises(CoherenceUnavailable):
        auth.token()


def test_failed_token_fetch_raises_unavailable(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401))
    )
    auth = CdseAuth("cid", "sec", storage=storage, client=client)
    with pytest.raises(CoherenceUnavailable):
        auth.token()

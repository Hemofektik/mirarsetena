"""Copernicus CDSE OAuth (client credentials) for the coherence layer.

Core layers never touch this: with no credentials configured the auth
reports disabled and coherence degrades gracefully (SCOPE R4-Q1).
"""
from __future__ import annotations

import json
import os
import time

import httpx

from mirarsetena.storage import Storage

CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
    "/protocol/openid-connect/token"
)
TOKEN_KEY = "system/cdse_token.json"
REFRESH_MARGIN_SECONDS = 60


class CoherenceUnavailable(RuntimeError):
    """CDSE access is not configured or not working."""


class CdseAuth:
    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        *,
        storage: Storage,
        client: httpx.Client | None = None,
        token_url: str = CDSE_TOKEN_URL,
        now=time.time,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._storage = storage
        self._client = client or httpx.Client(timeout=30)
        self._token_url = token_url
        self._now = now

    @property
    def enabled(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def token(self) -> str:
        if not self.enabled:
            raise CoherenceUnavailable("CDSE credentials not configured")
        cached = self._storage.get(TOKEN_KEY)
        if cached:
            record = json.loads(cached)
            if record.get("expires_at", 0) > self._now() + REFRESH_MARGIN_SECONDS:
                return str(record["access_token"])
        return self._fetch()

    def _fetch(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = self._client.post(self._token_url, data=data)
        except httpx.HTTPError as exc:
            raise CoherenceUnavailable(f"CDSE token transport error: {exc}") from exc
        if response.status_code >= 400:
            raise CoherenceUnavailable(
                f"CDSE token request failed: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CoherenceUnavailable("CDSE token response is not JSON") from exc
        token = payload.get("access_token")
        if not token:
            raise CoherenceUnavailable("CDSE token response lacks access_token")
        expires_in = int(payload.get("expires_in", 600))
        self._storage.put(
            TOKEN_KEY,
            json.dumps(
                {"access_token": token, "expires_at": self._now() + expires_in}
            ).encode("utf-8"),
        )
        return str(token)


def auth_from_env(storage: Storage, **overrides) -> CdseAuth:
    return CdseAuth(
        os.environ.get("CDSE_CLIENT_ID"),
        os.environ.get("CDSE_CLIENT_SECRET"),
        storage=storage,
        **overrides,
    )

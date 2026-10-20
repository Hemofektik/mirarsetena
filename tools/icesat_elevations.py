"""Download ICESat-2 ATL06 granules and store elevation points (L: tracks).

Requires Earthdata Login credentials in the environment:
    EARTHDATA_USER / EARTHDATA_PASS   (free: urs.earthdata.nasa.gov)

For every ATL06 granule whose footprint crosses the AOI (open CMR
metadata), downloads the HDF5 via Earthdata's authenticated redirect
chain, extracts (lon, lat, elevation) points inside the bbox with
read_atl06, and stores them as a GeoJSON FeatureCollection at
p/{slug}/catalog/icesat-elevations.json — which the
/icesat-elevations.geojson route serves and the map layer draws.

Downloads are cached under data/ice/ (re-runs skip existing files);
the store is rewritten each run so new passes accumulate.

Usage (any env with h5py + httpx, repo root):
    python -m tools.icesat_elevations --slug cdp-rio-general
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def is_lockout(body: bytes) -> bool:
    """True when Earthdata says the profile is locked or credentials are
    rejected — the signal to ABORT the whole run (each attempt against a
    locked profile counts as another failed login and extends the
    lockout; measured 2026-10-20)."""
    text = body.decode("utf-8", "replace").lower()
    return (
        "maximum number of allowed login attempts" in text
        or "locked temporarily" in text
        or "credentials (username, password) are invalid" in text
    )


def _credentials() -> tuple[str | None, str | None, str | None]:
    """(user, password, bearer-token) — token preferred when present.

    Basic auth does NOT survive Earthdata's OAuth redirect chain (the
    URS /oauth/authorize endpoint answers 401 — measured 2026-10-20);
    downloads use a cookie session (URS login POST) or a Bearer token.
    """
    token = os.environ.get("EARTHDATA_TOKEN")
    user = os.environ.get("EARTHDATA_USER")
    password = os.environ.get("EARTHDATA_PASS")
    if not token and (not user or not password):
        raise SystemExit(
            "EARTHDATA_TOKEN (preferred) or EARTHDATA_USER/EARTHDATA_PASS "
            "not set — register free at https://urs.earthdata.nasa.gov "
            "and add them to .env"
        )
    return user, password, token


def _download(url: str, dest: Path, user, password, token) -> Path:
    """Stream one Earthdata URL to dest.

    Token: Authorization Bearer on every hop. Username/password: an URS
    cookie session (POST /login once, then stream with the jar) — the
    redirect chain to data.nsidc then authenticates via cookies.
    """
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(120.0, connect=30.0),
        headers=headers,
    ) as client:
        if not token:
            # the URS form is CSRF-protected: fetch it first, then POST
            # with its authenticity_token (without it the session is
            # rejected as "Invalid or expired session token" — measured
            # 2026-10-20)
            import re as _re

            page = client.get("https://urs.earthdata.nasa.gov/login")
            match = _re.search(
                r'name="authenticity_token"[^>]*value="([^"]+)"', page.text
            ) or _re.search(
                r'value="([^"]+)"[^>]*name="authenticity_token"', page.text
            )
            form = {"username": user, "password": password}
            if match:
                form["authenticity_token"] = match.group(1)
            login = client.post("https://urs.earthdata.nasa.gov/login", data=form)
            if login.status_code >= 400:
                raise RuntimeError(
                    f"URS login failed: HTTP {login.status_code}"
                )
        with client.stream("GET", url) as response:
            if response.status_code >= 400:
                body = b"".join(response.iter_bytes())
                if is_lockout(body):
                    raise LockoutError(body.decode("utf-8", "replace")[:200])
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request, response=response,
                )
            with open(dest, "wb") as handle:
                handle.writelines(response.iter_bytes())
    return dest


class LockoutError(RuntimeError):
    """Earthdata rejected the credentials — stop the run, do not retry."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default="cdp-rio-general")
    parser.add_argument("--config-dir", default="config/projects")
    parser.add_argument("--cache-dir", default="data/ice")
    parser.add_argument("--storage-root", default="data/cache-e2e")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO))
    import httpx

    from mirarsetena.icesat import CMR_GRANULES_URL, read_atl06
    from mirarsetena.projects.registry import ProjectRegistry, cache_key
    from mirarsetena.storage import LocalStore

    registry = ProjectRegistry(args.config_dir)
    config = registry.get(args.slug)
    bbox = tuple(config.bbox)
    user, password, token = _credentials()

    # open CMR metadata: which granules cross the AOI
    response = httpx.get(
        CMR_GRANULES_URL,
        params={
            "short_name": "ATL06",
            "bounding_box": ",".join(str(c) for c in bbox),
            "page_size": "200",
        },
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    response.raise_for_status()
    entries = response.json()["feed"]["entry"]
    print(f"CMR: {len(entries)} ATL06 granules over the AOI", flush=True)

    cache_dir = Path(args.cache_dir)
    store = LocalStore(Path(args.storage_root))
    features: list[dict] = []
    for entry in entries:
        date = (entry.get("time_start") or "")[:10]
        granule = entry.get("producer_granule_id", "")
        if not date or not granule:
            continue
        # the download URL: the granule's first online link (data pool)
        links = [
            link.get("href", "")
            for link in entry.get("links", [])
            if link.get("href", "").endswith(".h5")
        ]
        if not links:
            print(f"  {granule}: no .h5 link — skipping", flush=True)
            continue
        local = cache_dir / granule
        if not local.is_file():
            print(f"  downloading {granule} ...", flush=True)
            try:
                _download(links[0], local, user, password, token)
            except LockoutError as exc:
                print(f"    LOCKOUT — aborting run: {exc}", flush=True)
                return 2
            except httpx.HTTPError as exc:
                print(f"    FAILED: {exc}", flush=True)
                continue
        try:
            points = read_atl06(local, bbox)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            print(f"    read failed: {exc}", flush=True)
            continue
        print(f"  {granule}: {len(points)} points in bbox", flush=True)
        for lon, lat, elev in points:
            features.append({
                "type": "Feature",
                "properties": {"date": date, "elev_m": round(elev, 3),
                               "product": "ATL06", "granule": granule},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            })

    features.sort(key=lambda f: (f["properties"]["date"],
                                 f["geometry"]["coordinates"][1]))
    collection = {"type": "FeatureCollection", "features": features}
    key = cache_key(args.slug, "catalog", "icesat-elevations.json")
    store.put(key, json.dumps(collection, ensure_ascii=False).encode("utf-8"))
    print(f"\nstored {len(features)} elevation points at {key}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

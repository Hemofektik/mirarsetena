"""CDSE SLC downloader: OData catalogue search + streamed SAFE download.

The interferometry jobs need the SLC twins of the GRD scenes the catalog
knows: both fly the same pass, so the SLC SAFE name is derivable from the
GRD id (INSAR-PLAN L.2). Search pins the derived prefix on the CDSE OData
catalogue; download streams `Products(<id>)/$value` to disk with the OAuth
bearer token.

Everything degrades to CoherenceUnavailable when credentials are absent or
the network/quota fails — jobs fail honestly instead of fabricating data
(SCOPE R4-Q1). No credentials or network in tests: httpx MockTransport.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from mirarsetena.coherence.cdse import CdseAuth, CoherenceUnavailable
from mirarsetena.coherence.pairs import InterferometricPair
from mirarsetena.coherence.pipeline import safe_name

ODATA_CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1"
ODATA_DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1"


def parse_grd_id(grd_id: str) -> dict:
    """Split a Sentinel-1 GRD SAFE id into its stable join fields.

    `S1D_IW_GRDH_1SDV_20261003T112229_20261003T112254_004850_0091DC`
    → platform S1D, start 20261003T112229, datatake 004850, product 0091DC.

    The datatake+product tail is IDENTICAL between the GRD and SLC renditions
    of one acquisition; only the start second can drift by 1–2 s (measured:
    GRD ...T112229 vs SLC ...T112228). So the tail is the join key and the
    start time is only a tiebreaker between adjacent slices.
    """
    parts = grd_id.split("_")
    if len(parts) < 7 or parts[2] != "GRDH":
        raise CoherenceUnavailable(f"not a Sentinel-1 GRD id: {grd_id!r}")
    return {
        "platform": parts[0],
        "mode": parts[1],
        "polarisation": parts[3],
        "start": parts[4],
        "datatake": parts[6],
        "product": parts[7] if len(parts) > 7 else "",
    }


def slc_name_prefix(grd_id: str) -> str:
    """CDSE OData filter fragment pinning the SLC twin of a GRD acquisition.

    Join on the stable datatake+product tail (not the start timestamp, which
    drifts 1–2 s between the GRD and SLC renditions).
    """
    meta = parse_grd_id(grd_id)
    return f"_{meta['datatake']}_{meta['product']}_"


def find_slc_product(auth: CdseAuth, grd_id: str) -> dict:
    """Locate the SLC twin of a GRD acquisition on the CDSE catalogue.

    Joins on the stable datatake+product tail (the start timestamp drifts
    1–2 s between renditions), restricted to the same platform + IW + SLC.
    Several adjacent slices share the tail, so the match nearest the GRD
    start time wins — the slice that actually covers the same ground.
    """
    if not auth.enabled:
        raise CoherenceUnavailable("CDSE credentials not configured")
    meta = parse_grd_id(grd_id)
    tail = slc_name_prefix(grd_id)
    filter_expr = (
        f"contains(Name,'{tail}') and contains(Name,'_IW_SLC__') "
        f"and startswith(Name,'{meta['platform']}_')"
    )
    url = f"{ODATA_CATALOGUE_URL}/Products"
    try:
        response = auth.client.get(
            url,
            params={"$filter": filter_expr, "$select": "Id,Name"},
            headers={"Authorization": f"Bearer {auth.token()}"},
        )
    except httpx.HTTPError as exc:
        raise CoherenceUnavailable(f"CDSE catalogue transport error: {exc}") from exc
    if response.status_code >= 400:
        raise CoherenceUnavailable(
            f"CDSE catalogue query failed: HTTP {response.status_code}"
        )
    try:
        values = response.json().get("value", [])
    except ValueError as exc:
        raise CoherenceUnavailable("CDSE catalogue response is not JSON") from exc
    if not values:
        raise CoherenceUnavailable(
            f"no SLC product with tail {tail!r} for platform {meta['platform']}"
        )
    # pick the slice whose start timestamp is closest to the GRD start
    def start_of(name: str) -> str:
        for field in name.split("_"):
            if len(field) == 15 and field[8] == "T":
                return field
        return ""

    target = meta["start"]
    best = min(values, key=lambda v: _ts_distance(target, start_of(str(v["Name"]))))
    return {"id": str(best["Id"]), "name": str(best.get("Name", ""))}


def _ts_distance(a: str, b: str) -> int:
    """Absolute second-gap between two `YYYYMMDDTHHMMSS` stamps (∞ if malformed)."""
    if len(a) != 15 or len(b) != 15:
        return 10**9
    try:
        da = datetime.strptime(a, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        db = datetime.strptime(b, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 10**9
    return abs(int((da - db).total_seconds()))


DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_READ_TIMEOUT_S = 60.0
_BACKOFF_S = (5, 15, 45)  # between attempts; the read timeout per chunk


def download_product(
    auth: CdseAuth,
    product_id: str,
    dest: Path,
    *,
    client=None,
    sleep=time.sleep,
) -> Path:
    """Stream one CDSE product (SAFE zip) to `dest`, resumable + retrying.

    CDSE stalls occasionally mid-stream (measured 2026-10-15: a 7 GB read
    timed out 23 minutes in and killed the pair). Each attempt resumes
    from the bytes already on `dest` via HTTP Range, so a stall costs the
    gap — not the whole scene — and a partial file survives a final
    failure for the next run to resume. HTTP >= 400 is not retried.
    """
    if not auth.enabled:
        raise CoherenceUnavailable("CDSE credentials not configured")
    dest = Path(dest)
    client = client or auth.client
    url = f"{ODATA_DOWNLOAD_URL}/Products({product_id})/$value"
    last_exc = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        offset = dest.stat().st_size if dest.is_file() else 0
        extra = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with client.stream(
                "GET", url,
                headers={"Authorization": f"Bearer {auth.token()}", **extra},
                timeout=httpx.Timeout(DOWNLOAD_READ_TIMEOUT_S, connect=30.0),
            ) as response:
                if response.status_code >= 400:
                    raise CoherenceUnavailable(
                        f"CDSE download failed: HTTP {response.status_code}"
                    )
                appending = offset > 0 and response.status_code == 206
                if not appending:
                    offset = 0
                with open(dest, "ab" if appending else "wb") as handle:
                    handle.writelines(response.iter_bytes())
            return dest
        except CoherenceUnavailable:
            raise
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt == DOWNLOAD_ATTEMPTS - 1:
                break
            sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
    raise CoherenceUnavailable(
        f"CDSE download transport error after {DOWNLOAD_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


def _link_into(src: Path, dst: Path) -> None:
    """Hardlink src at dst; fall back to a copy across filesystems."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:  # cross-device or unsupported: plain copy
        shutil.copy2(src, dst)


def _evict_lru(cache: Path, budget_bytes: int, keep: set[str]) -> None:
    """Delete least-recently-used zips until within budget, never `keep`."""
    files = sorted(
        (p for p in cache.glob("*.zip") if p.name not in keep),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in cache.glob("*.zip"))
    for path in files:
        if total <= budget_bytes:
            break
        total -= path.stat().st_size
        path.unlink(missing_ok=True)


def make_slc_downloader(
    auth: CdseAuth,
    *,
    cache_dir: str | Path | None = None,
    budget_bytes: int | None = None,
):
    """Downloader(pair, workdir) -> [zip paths], matching Phase H's contract.

    With `cache_dir` the scene zips live there keyed by scene id and the
    workdir paths are hardlinks into the cache: chained pairs [(A,B), (B,C)]
    reuse scene B's bytes instead of re-downloading ~7 GB — plan_pairs pairs
    consecutively so no temporal delta is missed, and the cache removes the
    duplicate download that chaining would otherwise cost (user 2026-10-13).
    After each call the cache is evicted LRU to `budget_bytes`, never touching
    the running pair's scenes; the job's `finally: rmtree(workdir)` removes
    only the links, so cached bytes survive for the next pair.
    """
    cache = Path(cache_dir) if cache_dir else None

    def download(pair: InterferometricPair, workdir: Path) -> list[Path]:
        keep: set[str] = set()
        results: list[Path] = []
        for scene in (pair.first, pair.second):
            if cache is None:
                product = find_slc_product(auth, scene.id)
                dest = Path(workdir) / f"{product['name'] or scene.id}.zip"
                results.append(download_product(auth, product["id"], dest))
                continue

            cached = cache / f"{safe_name(scene.id)}.zip"
            keep.add(cached.name)
            if cached.is_file():
                cached.touch()  # LRU: this scene is wanted right now
                link = Path(workdir) / cached.name
                _link_into(cached, link)
                results.append(link)
                continue

            product = find_slc_product(auth, scene.id)
            cache.mkdir(parents=True, exist_ok=True)
            staging = cache / f"{cached.name}.part"  # atomic: no partial zips
            try:
                download_product(auth, product["id"], staging)
            except BaseException:
                staging.unlink(missing_ok=True)
                raise
            os.replace(staging, cached)
            link = Path(workdir) / cached.name
            _link_into(cached, link)
            results.append(link)

        if cache is not None and budget_bytes:
            _evict_lru(cache, budget_bytes, keep)
        return results

    return download

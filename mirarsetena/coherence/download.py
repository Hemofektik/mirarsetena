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

from datetime import datetime, timezone
from pathlib import Path

import httpx

from mirarsetena.coherence.cdse import CdseAuth, CoherenceUnavailable
from mirarsetena.coherence.pairs import InterferometricPair

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


def download_product(auth: CdseAuth, product_id: str, dest: Path) -> Path:
    """Stream one CDSE product (SAFE zip) to `dest`."""
    if not auth.enabled:
        raise CoherenceUnavailable("CDSE credentials not configured")
    url = f"{ODATA_DOWNLOAD_URL}/Products({product_id})/$value"
    try:
        with auth.client.stream(
            "GET", url, headers={"Authorization": f"Bearer {auth.token()}"}
        ) as response:
            if response.status_code >= 400:
                raise CoherenceUnavailable(
                    f"CDSE download failed: HTTP {response.status_code}"
                )
            with open(dest, "wb") as handle:
                handle.writelines(response.iter_bytes())
    except httpx.HTTPError as exc:
        raise CoherenceUnavailable(f"CDSE download transport error: {exc}") from exc
    return dest


def make_slc_downloader(auth: CdseAuth):
    """Downloader(pair, workdir) -> [zip paths], matching Phase H's contract."""

    def download(pair: InterferometricPair, workdir: Path) -> list[Path]:
        paths = []
        for scene in (pair.first, pair.second):
            product = find_slc_product(auth, scene.id)
            dest = Path(workdir) / f"{product['name'] or scene.id}.zip"
            paths.append(download_product(auth, product["id"], dest))
        return paths

    return download

"""INSAR-PLAN L.2 — CDSE SLC downloader (OData search + streamed download).

Seam: mirarsetena.coherence.download. SLC products are found on the CDSE
OData catalogue by deriving the SLC SAFE-name prefix from the GRD scene id
(ground acquisitions share the pass), then streamed from the download
endpoint. Everything is httpx-MockTransport tested — no credentials, no
network.
"""
import urllib.parse

import httpx
import pytest

from mirarsetena.coherence.cdse import CdseAuth, CoherenceUnavailable
from mirarsetena.coherence.download import (
    download_product,
    find_slc_product,
    make_slc_downloader,
    slc_name_prefix,
)
from mirarsetena.coherence.pairs import InterferometricPair
from mirarsetena.pipeline.catalog import Scene
from mirarsetena.storage import LocalStore

GRD_ID = "S1D_IW_GRDH_1SDV_20261003T112229_20261003T112254_004850_0091DC"




def _zip_body(payload: bytes) -> bytes:
    """A VALID zip carrying `payload`: the SLC downloader now runs an
    integrity check (is_valid_zip), so the mock CDN must serve archives."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("scene.bin", payload)
    return buf.getvalue()

def test_slc_name_prefix_is_the_stable_tail():
    # The GRD→SLC join key is the datatake+product tail, NOT the start
    # timestamp (which drifts 1–2 s between renditions; measured real CDSE:
    # GRD ...T112229 vs SLC ...T112228). The tail is identical.
    assert slc_name_prefix(GRD_ID) == "_004850_0091DC_"
    assert slc_name_prefix(
        "S1A_IW_GRDH_1SDV_20260723T234759_20260723T234824_065000_088ABC_1234"
    ) == "_065000_088ABC_"


def test_parse_grd_id_fields():
    from mirarsetena.coherence.download import parse_grd_id

    meta = parse_grd_id(GRD_ID)
    assert meta["platform"] == "S1D"
    assert meta["start"] == "20261003T112229"
    assert meta["datatake"] == "004850"
    assert meta["product"] == "0091DC"


def _auth(tmp_path, handler):
    storage = LocalStore(tmp_path / "cache")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    auth = CdseAuth(
        "cid", "sec", storage=storage, client=client,
        now=lambda: 1_000_000.0,
    )
    return auth, storage


def test_find_slc_product_queries_catalogue_with_bearer(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 600}
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    # real-style SLC name: same tail as GRD, start 1 s earlier
                    {"Id": "prod-123",
                     "Name": "S1D_IW_SLC__1SDV_20261003T112228_20261003T112254_004850_0091DC_540E.SAFE"}
                ]
            },
        )

    auth, _ = _auth(tmp_path, handler)
    product = find_slc_product(auth, GRD_ID)
    assert product["id"] == "prod-123"
    # the OData filter joins on the stable tail, not the drifting timestamp
    decoded = urllib.parse.unquote(seen["url"])
    assert "contains(Name,'_004850_0091DC_')" in decoded
    assert "contains(Name,'_IW_SLC__')" in decoded
    assert seen["auth"] == "Bearer tok"


def test_find_slc_product_picks_slice_nearest_in_time(tmp_path):
    """Several slices share the tail; the one closest to the GRD start time
    (112229) must win — not the first returned."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 600}
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    # 26 s earlier — the adjacent slice, must lose
                    {"Id": "far", "Name": "S1D_IW_SLC__1SDV_20261003T112203_20261003T112230_004850_0091DC_1BF0.SAFE"},
                    # 1 s earlier — the true twin, must win
                    {"Id": "near", "Name": "S1D_IW_SLC__1SDV_20261003T112228_20261003T112254_004850_0091DC_540E.SAFE"},
                ]
            },
        )

    auth, _ = _auth(tmp_path, handler)
    assert find_slc_product(auth, GRD_ID)["id"] == "near"


def test_find_slc_product_missing_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 600}
            )
        return httpx.Response(200, json={"value": []})

    auth, _ = _auth(tmp_path, handler)
    with pytest.raises(CoherenceUnavailable, match="no SLC product"):
        find_slc_product(auth, GRD_ID)


def test_download_product_streams_zip(tmp_path):
    payload = b"PK\x03\x04" + b"SLC" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 600}
            )
        assert "/odata/v1/Products(prod-123)/$value" in str(request.url)
        assert request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(200, content=payload)

    auth, _ = _auth(tmp_path, handler)
    dest = tmp_path / "scene.zip"
    path = download_product(auth, "prod-123", dest)
    assert path == dest
    assert dest.read_bytes() == payload


def test_download_http_error_raises_unavailable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 600}
            )
        return httpx.Response(403, text="quota exceeded")

    auth, _ = _auth(tmp_path, handler)
    with pytest.raises(CoherenceUnavailable, match="403"):
        download_product(auth, "prod-123", tmp_path / "x.zip")


def test_make_slc_downloader_fills_pair_workdir(tmp_path):
    """The Downloader matches run_insar_pair_job's contract: given a pair,
    both SLC zips land in the workdir and their paths come back."""
    ids_by_tail = {
        "_065000_088A4D_": "prod-first",
        "_065000_088ABC_": "prod-second",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 600}
            )
        decoded = urllib.parse.unquote(str(request.url))
        if "/$value" in decoded:
            product_id = decoded.split("Products(")[1].split(")")[0]
            return httpx.Response(200, content=_zip_body(product_id.encode() * 10))
        # catalogue search: answer with the product matching the tail
        tail = decoded.split("contains(Name,'")[1].split("')")[0]
        product_id = next(pid for t, pid in ids_by_tail.items() if t == tail)
        name = f"S1A_IW_SLC__1SDV_20260711T234758_20260711T234823{tail}5A1F.SAFE"
        return httpx.Response(200, json={"value": [{"Id": product_id, "Name": name}]})

    auth, _ = _auth(tmp_path, handler)
    first = Scene(
        id="S1A_IW_GRDH_1SDV_20260711T234758_20260711T234823_065000_088A4D_5A1F",
        collection="sentinel-1-slc",
        datetime="2026-07-11T23:47:58Z", date="2026-07-11", cloud=None,
        assets={}, orbit_state="ascending", relative_orbit=92,
    )
    second = Scene(
        id="S1A_IW_GRDH_1SDV_20260723T234759_20260723T234824_065000_088ABC_1234",
        collection="sentinel-1-slc",
        datetime="2026-07-23T23:47:59Z", date="2026-07-23", cloud=None,
        assets={}, orbit_state="ascending", relative_orbit=92,
    )
    workdir = tmp_path / "pair"
    workdir.mkdir()
    downloader = make_slc_downloader(auth)
    paths = downloader(InterferometricPair(first=first, second=second), workdir)
    assert len(paths) == 2
    assert all(path.exists() for path in paths)
    assert all(path.parent == workdir for path in paths)


def test_disabled_auth_raises_unavailable(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    auth = CdseAuth(None, None, storage=storage)
    with pytest.raises(CoherenceUnavailable):
        find_slc_product(auth, GRD_ID)

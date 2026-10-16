"""INSAR-PLAN cadence fix — SLC scene cache (user 2026-10-13).

`plan_pairs` chains acquisitions [(A,B), (B,C), ...] so no temporal delta is
missed, but the shared scene would be downloaded twice under compute-and-
purge. The downloader now caches each scene zip under the storage root:
a second pair hitting scene B reuses the bytes instead of re-downloading
~7 GB. Disk stays LRU-bounded; the workdir links die with the job's
`finally: rmtree`, the cached bytes survive.
"""
import httpx

from mirarsetena.coherence.cdse import CdseAuth
from mirarsetena.coherence.download import make_slc_downloader
from mirarsetena.coherence.pairs import InterferometricPair
from mirarsetena.pipeline.catalog import Scene
from mirarsetena.storage import LocalStore

# Scene ids are GRD-style (shared by plan_pairs metadata); the cache keys
# on them, the catalogue answers by tail as in test_download.py.
SCENE_A = Scene(
    id="S1A_IW_GRDH_1SDV_20260711T234758_20260711T234823_065000_088A4D_5A1F",
    collection="sentinel-1-slc", datetime="2026-07-11T23:47:58Z",
    date="2026-07-11", cloud=None, assets={},
)
SCENE_B = Scene(
    id="S1A_IW_GRDH_1SDV_20260723T234759_20260723T234824_065000_088ABC_1234",
    collection="sentinel-1-slc", datetime="2026-07-23T23:47:59Z",
    date="2026-07-23", cloud=None, assets={},
)
SCENE_C = Scene(
    id="S1A_IW_GRDH_1SDV_20260804T234759_20260804T234824_066000_089ABC_1235",
    collection="sentinel-1-slc", datetime="2026-08-04T23:47:59Z",
    date="2026-08-04", cloud=None, assets={},
)


def _zip_body(payload: bytes) -> bytes:
    """A VALID zip carrying `payload`: the SLC downloader now runs an
    integrity check (is_valid_zip), so the mock CDN must serve archives."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("scene.bin", payload)
    return buf.getvalue()


def _pair(first, second):
    return InterferometricPair(first=first, second=second)


def _auth(tmp_path):
    """CDSE auth whose catalogue answers every tail query and whose
    $value endpoint returns the PRODUCT ID as bytes (so scenes are
    distinguishable), counting real downloads."""
    storage = LocalStore(tmp_path / "cache")
    stats = {"downloads": 0, "queries": 0}
    tails = {
        "_065000_088A4D_": "prod-a",
        "_065000_088ABC_": "prod-b",
        "_066000_089ABC_": "prod-c",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 600}
            )
        url = str(request.url)
        if "/$value" in url:
            stats["downloads"] += 1
            product_id = url.split("Products(")[1].split(")")[0]
            return httpx.Response(200, content=_zip_body(product_id.encode() * 100))
        stats["queries"] += 1
        for tail, pid in tails.items():
            if tail in url:
                return httpx.Response(
                    200,
                    json={"value": [{"Id": pid, "Name": f"S1A_IW_SLC__1SDV_x{tail}z.SAFE"}]},
                )
        return httpx.Response(200, json={"value": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    auth = CdseAuth("cid", "sec", storage=storage, client=client, now=lambda: 1_000_000.0)
    return auth, stats


def test_chain_pair_reuses_shared_scene_download(tmp_path):
    auth, stats = _auth(tmp_path)
    cache_dir = tmp_path / "slc"
    downloader = make_slc_downloader(
        auth, cache_dir=cache_dir, budget_bytes=10 * 1024**3
    )

    # pair 1 (A, B): both scenes download fresh
    work1 = tmp_path / "job1"
    work1.mkdir()
    paths1 = downloader(_pair(SCENE_A, SCENE_B), work1)
    assert stats["downloads"] == 2
    assert all(p.exists() for p in paths1)
    b_bytes = paths1[1].read_bytes()

    # the job's finally-block purges its workdir ...
    import shutil

    shutil.rmtree(work1)

    # pair 2 (B, C): B comes from the cache — only C downloads
    work2 = tmp_path / "job2"
    work2.mkdir()
    paths2 = downloader(_pair(SCENE_B, SCENE_C), work2)
    assert stats["downloads"] == 3  # not 4: B was NOT downloaded twice
    assert paths2[0].read_bytes() == b_bytes  # same bytes, via cache
    assert all(p.exists() for p in paths2)


def test_cache_survives_workdir_purge(tmp_path):
    auth, _ = _auth(tmp_path)
    cache_dir = tmp_path / "slc"
    downloader = make_slc_downloader(
        auth, cache_dir=cache_dir, budget_bytes=10 * 1024**3
    )
    work = tmp_path / "job"
    work.mkdir()
    downloader(_pair(SCENE_A, SCENE_B), work)
    assert len(list(cache_dir.glob("*.zip"))) == 2

    import shutil

    shutil.rmtree(work)  # job finally-block: links go, cache stays
    assert len(list(cache_dir.glob("*.zip"))) == 2


def test_cache_evicts_lru_but_never_the_running_pair(tmp_path):
    auth, _ = _auth(tmp_path)
    cache_dir = tmp_path / "slc"
    # each mock zip is 600 B; budget fits one scene only — eviction is forced
    budget = 1000
    downloader = make_slc_downloader(
        auth, cache_dir=cache_dir, budget_bytes=budget
    )
    work1 = tmp_path / "job1"
    work1.mkdir()
    downloader(_pair(SCENE_A, SCENE_B), work1)  # A, B cached (keep-set wins)

    work2 = tmp_path / "job2"
    work2.mkdir()
    paths2 = downloader(_pair(SCENE_B, SCENE_C), work2)

    cached = {p.name for p in cache_dir.glob("*.zip")}
    # the running pair (B, C) is never evicted; the oldest free scene is
    assert any("088ABC" in name for name in cached)  # B kept
    assert any("089ABC" in name for name in cached)  # C kept
    assert not any("088A4D" in name for name in cached)  # A evicted LRU
    # bounded by budget + the two kept scenes (the keep-set floor)
    total = sum(p.stat().st_size for p in cache_dir.glob("*.zip"))
    assert total <= budget + 1200
    assert all(p.exists() for p in paths2)  # links intact


def test_workdir_paths_are_hardlinks_when_same_fs(tmp_path):
    auth, _ = _auth(tmp_path)
    cache_dir = tmp_path / "slc"
    downloader = make_slc_downloader(
        auth, cache_dir=cache_dir, budget_bytes=10 * 1024**3
    )
    work = tmp_path / "job"
    work.mkdir()
    paths = downloader(_pair(SCENE_A, SCENE_B), work)
    import os

    for path in paths:
        cached = cache_dir / path.name
        assert cached.is_file()
        assert os.stat(path).st_ino == os.stat(cached).st_ino  # hardlink


def test_downloader_without_cache_still_works(tmp_path):
    auth, stats = _auth(tmp_path)
    downloader = make_slc_downloader(auth)  # no cache: legacy behavior
    work = tmp_path / "job"
    work.mkdir()
    paths = downloader(_pair(SCENE_A, SCENE_B), work)
    assert stats["downloads"] == 2
    assert all(p.exists() for p in paths)

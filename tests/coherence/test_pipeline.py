"""H.3 — compute-and-purge coherence pipeline (SCOPE R4-Q4).

The SLC sources exist only for the duration of one pair job: downloaded,
processed, result cached, sources deleted — on success AND on failure.
"""

import pytest

from mirarsetena.coherence.pairs import InterferometricPair
from mirarsetena.coherence.pipeline import find_processor, run_pair_job
from mirarsetena.pipeline.catalog import Scene
from mirarsetena.storage import LocalStore

BBOX = (-83.675, 9.378, -83.660, 9.397)


def _pair() -> InterferometricPair:
    first = Scene(
        id="S1A_TEST_1", collection="sentinel-1-slc",
        datetime="2026-07-11T23:47:58Z", date="2026-07-11",
        cloud=None, assets={},
    )
    second = Scene(
        id="S1A_TEST_2", collection="sentinel-1-slc",
        datetime="2026-07-23T23:47:59Z", date="2026-07-23",
        cloud=None, assets={},
    )
    return InterferometricPair(first=first, second=second)


def _downloader(created: list):
    def download(pair, workdir):
        paths = []
        for scene in (pair.first, pair.second):
            path = workdir / f"{scene.id}.zip"
            path.write_bytes(b"SLC" * 1000)
            paths.append(path)
        created.append(workdir)
        return paths

    return download


def test_run_pair_job_caches_result_and_purges_sources(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    created = []

    def processor(paths, bbox):
        assert all(path.exists() for path in paths)
        assert bbox == BBOX
        return b"COHERENCE-GEOTIFF"

    result = run_pair_job(
        storage, "proj-a", _pair(), bbox=BBOX,
        downloader=_downloader(created), processor=processor,
        workdir=tmp_path / "work",
    )

    assert result["key"].startswith("p/proj-a/coherence/")
    assert storage.get(result["key"]) == b"COHERENCE-GEOTIFF"
    assert result["downloaded_bytes"] == 2 * 3000
    assert not created[0].exists()  # sources purged after success


def test_sources_purged_even_when_processor_fails(tmp_path):
    storage = LocalStore(tmp_path / "cache")
    created = []

    def processor(paths, bbox):
        raise RuntimeError("interferogram failed")

    with pytest.raises(RuntimeError, match="interferogram failed"):
        run_pair_job(
            storage, "proj-a", _pair(), bbox=BBOX,
            downloader=_downloader(created), processor=processor,
            workdir=tmp_path / "work",
        )

    assert not created[0].exists()  # purge happens on failure too
    assert storage.list("p/proj-a/coherence/") == []


def test_find_processor_is_host_dependent_but_safe():
    processor = find_processor()
    assert processor is None or callable(processor)

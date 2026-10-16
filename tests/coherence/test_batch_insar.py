"""Batch InSAR history builder seams (tools/batch_insar).

Seams: resolve_pairs (which pairs --pair-indexes selects, mirroring the
spike's catalog window), pair_is_done (resume skip: BOTH the product and
its series must exist), and purge_candidates (what a pair dir may free —
merged/ and unwrap2/ must NEVER appear: they are the only inputs for
re-deriving the product, like the 2026-10-03 rebuild). Not run against
CDSE/ISCE in CI — only the pure logic.
"""
import json
from pathlib import Path

import pytest

from mirarsetena.pipeline.catalog import parse_scenes
from mirarsetena.storage import LocalStore
from tools.batch_insar import pair_is_done, purge_candidates, resolve_pairs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fake_search(collection, bbox, start, end):
    if collection != "sentinel-1-grd":
        return []
    payload = json.loads((FIXTURES / "stac_s1_grd.json").read_text(encoding="utf-8"))
    return parse_scenes(collection, payload)


BBOX = (-83.675, 9.378, -83.660, 9.397)


def test_resolve_pairs_orders_oldest_first_and_indexes_the_fixture():
    pairs = resolve_pairs(fake_search, BBOX, "2026-01-01", "2026-10-12")
    # 6 pairs from the 8-scene fixture (same list the spike indexes)
    ends = [pair.second.date for pair in pairs]
    assert ends == [
        "2026-07-23", "2026-08-04", "2026-08-16",
        "2026-08-28", "2026-09-09", "2026-10-03",
    ]
    # the batch default --pair-indexes -5,-4,-3,-2 = the four pairs BEFORE
    # the already-built 10-03 spike product, oldest first
    picked = [pairs[i].second.date for i in (-5, -4, -3, -2)]
    assert picked == ["2026-08-04", "2026-08-16", "2026-08-28", "2026-09-09"]


def test_pair_is_done_requires_product_and_series(tmp_path):
    from mirarsetena.pipeline.indices import layer_key
    from mirarsetena.pipeline.insar import insar_series_key

    store = LocalStore(tmp_path)
    slug, date = "cdp-rio-general", "2026-09-09"
    product = layer_key(slug, "sentinel-1-slc", date, "insar")
    series = insar_series_key(slug, date)

    assert not pair_is_done(store, slug, date)
    store.put(product, b"tif")
    assert not pair_is_done(store, slug, date)  # product alone: rebuild the series
    store.put(series, b"{}")
    assert pair_is_done(store, slug, date)


def test_purge_candidates_keep_the_rebuild_inputs(tmp_path):
    # merged/lat.rdr.full.vrt stitches ../geom_reference/*.vrt — purging
    # geom_reference broke the 10-03 pair's re-derivation (measured
    # 2026-10-14), so it is a rebuild input like merged/ and unwrap2/.
    keep = ["merged", "unwrap2", "geom_reference"]
    heavy = [
        "fine_coreg", "fine_offsets", "fine_interferogram",
        "coarse_coreg", "coarse_offsets", "coarse_interferogram",
        "ESD", "reference", "secondary", "PICKLE", "unwrap",
    ]
    for name in keep + heavy:
        (tmp_path / name).mkdir()
    (tmp_path / "S1D_IW_SLC__x.SAFE.zip").write_bytes(b"zip")
    (tmp_path / "S1D_IW_SLC__x.SAFE.unpack").mkdir()

    names = {p.name for p in purge_candidates(tmp_path)}
    for name in heavy:
        assert name in names
    assert "S1D_IW_SLC__x.SAFE.zip" in names
    assert "S1D_IW_SLC__x.SAFE.unpack" in names
    # the product-rebuild inputs are never on the purge list
    for name in keep:
        assert name not in names


def _slant_grid(west_edge, east_edge, lat0, lat1, lines=10, pixels=21):
    """Synthetic curvilinear geolocation grid: (line, pixel, lon, lat),
    columns slanted eastward with latitude (like a real IW strip)."""
    pts = []
    for i in range(lines):
        lat = lat0 + (lat1 - lat0) * i / (lines - 1)
        shift = 0.15 * (lat - lat0)  # slant: strip drifts east going north
        for j in range(pixels):
            lon = (west_edge + (east_edge - west_edge) * j / (pixels - 1)
                   + shift)
            pts.append((i * 1500, j * 1223, lon, lat))
    return pts


def test_lon_band_interpolates_between_grid_nodes():
    from tools.spike_insar import lon_band_at

    # strip: lon -84.04..-83.18 at lat 9.3875 (the REAL 08-04 IW2 numbers)
    pts = _slant_grid(-84.041, -83.182, 9.235, 10.915)
    band = lon_band_at(pts, 9.3875)
    assert band is not None
    lo, hi = band
    assert lo == pytest.approx(-84.041 + 0.15 * (9.3875 - 9.235), abs=0.01)
    assert hi == pytest.approx(-83.182 + 0.15 * (9.3875 - 9.235), abs=0.01)
    # outside the strip's latitude range: no band at all
    assert lon_band_at(pts, 11.5) is None


def test_swath_covering_uses_the_strip_not_sparse_nodes():
    from tools.spike_insar import swath_covering

    # the real 2026-07-23 IW2 case: nodes are ~0.11 deg apart, so the old
    # node-inside-bbox count was 0 even though the strip covers the AOI
    pts = _slant_grid(-84.041, -83.182, 9.235, 10.915)
    aoi = (-83.675, 9.378, -83.660, 9.397)
    assert swath_covering(pts, aoi) is True
    # a strip 0.4 deg west of the AOI does not cover it
    assert swath_covering(_slant_grid(-84.5, -84.0, 9.2, 10.9), aoi) is False


def test_deploy_commands_target_the_live_cache_root(tmp_path):
    # A wrong destination silently undid a deploy after a full pair build
    # (2026-10-14): the docker cp path must be /app/data/cache/<key>.
    from tools.batch_insar import deploy_commands

    cmds = deploy_commands(
        "2026-08-04", "cdp-rio-general", tmp_path, "mirarsetena-api-1"
    )
    tiles_rm = cmds[0]
    assert tiles_rm[:4] == ["docker", "exec", "mirarsetena-api-1", "rm"]
    assert "/tiles/insar/2026-08-04" in tiles_rm[-1]
    copies = cmds[1:]
    assert len(copies) == 2
    for cmd in copies:
        assert cmd[:2] == ["docker", "cp"]
        src, dest = cmd[2], cmd[3]
        assert src.startswith(str(tmp_path / "p/cdp-rio-general/layers/"))
        assert dest.startswith("mirarsetena-api-1:/app/data/cache/p/")
        # product and series both travel, and their names are distinct
    names = sorted(cmd[2].rsplit("/", 1)[-1] for cmd in copies)
    assert names == [
        "sentinel-1-slc_2026-08-04_insar.tif",
        "sentinel-1-slc_2026-08-04_insar_series.json",
    ]


def test_disk_headroom_guard(monkeypatch, tmp_path):
    # topsApp SIGBUS'd at rc=-7 when the disk hit 0 bytes mid-pair
    # (2026-10-15, pair 16): the driver must refuse to START a pair it
    # cannot finish, with a message that says how much is needed.
    import shutil as _shutil

    from tools.batch_insar import MIN_FREE_BYTES, has_disk_headroom

    def usage(_path):
        return _shutil._ntuple_diskusage(total=1, used=0, free=MIN_FREE_BYTES - 1)

    monkeypatch.setattr("tools.batch_insar.shutil.disk_usage", usage)
    assert has_disk_headroom(tmp_path) is False

    def plenty(_path):
        return _shutil._ntuple_diskusage(total=1, used=0, free=MIN_FREE_BYTES)

    monkeypatch.setattr("tools.batch_insar.shutil.disk_usage", plenty)
    assert has_disk_headroom(tmp_path) is True


def test_plan_pairs_supports_24day_triangles():
    # SBAS needs redundant baselines: on a consecutive 12-day chain the
    # per-date atmosphere is indistinguishable from motion (rank-deficient
    # for MintPy). 24-day pairs close triangles (t0,t1),(t1,t2),(t0,t2).
    import json as _json

    from mirarsetena.coherence.pairs import plan_pairs

    payload = _json.loads((FIXTURES / "stac_s1_grd.json").read_text(encoding="utf-8"))
    scenes = parse_scenes("sentinel-1-grd", payload)
    twelve = plan_pairs(scenes)
    assert len(twelve) == 6  # default stays the consecutive 12-day chain
    twentyfour = plan_pairs(scenes, min_days=23, max_days=25)
    ends = [(p.first.date, p.second.date) for p in twentyfour]
    # fixture asc chain: 07-11, 07-23, 08-04, 08-16, 08-28, 09-09 — the
    # 24-day pairs are exactly the off-diagonal triangle closers
    assert ("2026-07-11", "2026-08-04") in ends
    assert ("2026-08-16", "2026-09-09") in ends
    assert all(
        23 <= (_dt(p.second.datetime) - _dt(p.first.datetime)).days <= 25
        for p in twentyfour
    )
    # and the two planners never mix: no 12-day pair in the 24-day list
    assert all(p.first.date != p.second.date for p in twentyfour)


def _dt(iso):
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_resolve_pairs_passes_the_baseline_window():
    import json as _json

    payload = _json.loads((FIXTURES / "stac_s1_grd.json").read_text(encoding="utf-8"))

    def fake(collection, bbox, start, end):
        return parse_scenes(collection, payload)

    twelve = resolve_pairs(fake, BBOX, "2026-01-01", "2026-10-12")
    twentyfour = resolve_pairs(
        fake, BBOX, "2026-01-01", "2026-10-12", min_days=23, max_days=25
    )
    assert len(twelve) == 6
    assert len(twentyfour) == 4  # the asc triangle closers


def test_pair_processed_needs_both_chain_outputs(tmp_path):
    from tools.batch_insar import pair_processed

    assert not pair_processed(tmp_path)
    (tmp_path / "merged").mkdir()
    (tmp_path / "merged" / "filt_topophase.flat").write_bytes(b"x")
    assert not pair_processed(tmp_path)  # unwrap still missing
    (tmp_path / "unwrap2").mkdir()
    (tmp_path / "unwrap2" / "crop.unw").write_bytes(b"x")
    assert pair_processed(tmp_path)

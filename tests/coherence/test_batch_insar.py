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

from mirarsetena.coherence.pairs import plan_pairs
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


def test_pair_dirs_never_collide_across_baselines():
    # Measured 2026-10-16: end-date-only dirs made the sbas-only gate skip
    # all four 24-day closers (their end dates matched processed 12-day
    # pairs) and a rerun would have clobbered the 12-day artifacts.
    import json as _json

    from tools.batch_insar import pair_dir_name

    payload = _json.loads((FIXTURES / "stac_s1_grd.json").read_text(encoding="utf-8"))
    scenes = parse_scenes("sentinel-1-grd", payload)
    twelve = {(p.first.date, p.second.date): pair_dir_name(p)
              for p in plan_pairs(scenes)}
    twentyfour = {(p.first.date, p.second.date): pair_dir_name(p)
                  for p in plan_pairs(scenes, min_days=23, max_days=25)}
    # identical end dates, different windows -> different dirs
    ends_12 = {e for _s, e in twelve}
    ends_24 = {e for _s, e in twentyfour}
    assert ends_12 & ends_24  # they DO share end dates (08-04, 08-16, ...)
    assert set(twelve.values()).isdisjoint(twentyfour.values())


def test_sbas_inverts_a_triangle_network():
    # The core SBAS claim: on a triangle (t0,t1),(t1,t2),(t0,t2) the
    # per-date displacement is separable from the observations — the
    # closing edge makes the solve REDUNDANT (atmosphere of one pair can
    # no longer masquerade as motion). Synthetic truth: linear motion
    # 0, -1, -2 cm; the closing edge is 2 cm total.
    import numpy as np

    from mirarsetena.coherence.sbas import solve_sbas

    truth = np.array([0.0, -1.0, -2.0])  # per-date cumulative
    edges = [
        ("2026-01-01", "2026-01-13", np.array(-1.0)),
        ("2026-01-13", "2026-01-25", np.array(-1.0)),
        ("2026-01-01", "2026-01-25", np.array(-2.0)),
    ]
    dates, fields, valid = solve_sbas(
        ["2026-01-01", "2026-01-13", "2026-01-25"],
        [(a, b, v) for a, b, v in edges],
    )
    assert dates == ["2026-01-01", "2026-01-13", "2026-01-25"]
    assert valid
    for t, expect in enumerate(truth):
        assert float(fields[t]) == pytest.approx(expect, abs=1e-6)
    # the reference date is pinned at zero
    assert float(fields[0]) == pytest.approx(0.0, abs=1e-9)


def test_sbas_averages_conflicting_triangle_paths():
    # The atmosphere enters one edge as an inconsistent 0.4 cm: the LSQ
    # spreads it over the network (the closing edge is only ONE of three
    # observations of the same displacement).
    import numpy as np

    from mirarsetena.coherence.sbas import solve_sbas

    _dates, fields, valid = solve_sbas(
        ["2026-01-01", "2026-01-13", "2026-01-25"],
        [
            ("2026-01-01", "2026-01-13", np.array(-1.0)),
            ("2026-01-13", "2026-01-25", np.array(-1.0)),
            ("2026-01-01", "2026-01-25", np.array(-1.6)),  # -2 + 0.4 APS
        ],
    )
    assert valid
    # unweighted LSQ: minimize (x1+1)^2 + (x2-x1+1)^2 + (x2+1.6)^2 ->
    # 2x1 - x2 = 0 and -x1 + 2x2 = -2.6 -> x1 = -2.6/3, x2 = -5.2/3
    assert float(fields[1]) == pytest.approx(-2.6 / 3, abs=1e-6)
    assert float(fields[2]) == pytest.approx(-5.2 / 3, abs=1e-6)


def test_sbas_pixelwise_masks_and_chain_degeneracy():
    # A pure chain (no closers) still solves — but its solution is just
    # the integration (rank T-1, zero redundancy); pixels missing an
    # edge are solved from what remains; pixels below the edge count
    # for their dates are nodata.
    import numpy as np

    from mirarsetena.coherence.sbas import solve_sbas

    dates = ["2026-01-01", "2026-01-13", "2026-01-25"]
    d = np.array([1.0, 1.0])
    masked = np.array([1.0, np.nan])
    dates_out, fields, valid = solve_sbas(
        dates,
        [
            ("2026-01-01", "2026-01-13", d),
            ("2026-01-13", "2026-01-25", masked),
        ],
    )
    assert dates_out == dates
    # pixel 0 integrates the chain exactly
    assert valid[0]
    assert float(fields[1][0]) == pytest.approx(1.0)
    assert float(fields[2][0]) == pytest.approx(2.0)
    # pixel 1 has one edge only -> first two dates solve, the last is
    # unobservable (x2 is a leaf without its edge) -> nodata there
    assert valid[1]
    assert float(fields[2][1]) != float(fields[2][1])  # NaN


def test_triangle_closures_quantify_network_inconsistency():
    # The network's quality metric: a triangle's closure
    # d(i,k) - d(i,j) - d(j,k) is zero only when the three observations
    # agree — per-edge atmosphere and reference drift show up here.
    import numpy as np

    from mirarsetena.coherence.sbas import closure_stats

    edges = [
        ("a", "b", np.array([1.0, 2.0])),
        ("b", "c", np.array([1.0, 2.0])),
        ("a", "c", np.array([2.0, 4.0])),   # perfectly consistent
    ]
    stats = closure_stats(edges)
    assert len(stats) == 1
    assert stats[0]["triangle"] == ("a", "b", "c")
    assert stats[0]["median_abs"] == 0.0

    edges[2] = ("a", "c", np.array([2.4, 4.4]))  # +0.4 cm on one path
    stats = closure_stats(edges)
    assert stats[0]["median_abs"] == pytest.approx(0.4)
    assert stats[0]["p90_abs"] == pytest.approx(0.4)


def test_rereference_edges_uses_one_fixed_pixel_set():
    # Per-pair referencing injects per-edge constant offsets; re-referencing
    # ALL edges to the SAME reference pixels makes their zero levels
    # mutually consistent — the closure metric should shrink.
    import numpy as np

    from mirarsetena.coherence.sbas import rereference_edges

    ref = np.array([True, True, False, False])
    e1 = np.array([1.5, 1.7, 9.9, 9.9])    # median over ref = 1.6
    e2 = np.array([-0.1, 0.1, 5.5, 5.5])   # median over ref = 0.0
    (r1, r2), offsets = rereference_edges([e1, e2], ref)
    assert offsets == pytest.approx([1.6, 0.0])
    assert float(np.median(r1[ref])) == pytest.approx(0.0)
    assert float(np.median(r2[ref])) == pytest.approx(0.0)
    # values elsewhere shift by the same constant (structure preserved)
    assert float(r1[2] - e1[2]) == pytest.approx(-1.6)
    # an edge with NO valid reference pixel is left untouched (honest)
    e3 = np.array([np.nan, np.nan, 5.5, 5.5])
    (rr,), off = rereference_edges([e3], ref)
    assert off == [None]
    assert np.all(np.isnan(rr[:2])) and float(rr[2]) == 5.5


def test_pair_dirs_disambiguate_tracks():
    # Measured 2026-10-19: dates-only dirs let a DESCENDING pair's
    # processed dir mask the ASCENDING pair with the same dates (the
    # S1A batch skipped idx 1/4 as "already processed" and I applied
    # chain-filtered indexes to the unfiltered list). The dir name
    # carries the track.
    from mirarsetena.coherence.pairs import InterferometricPair
    from mirarsetena.pipeline.catalog import Scene
    from tools.batch_insar import pair_dir_name

    def scene(date, dt, orbit, rel):
        return Scene(
            id=f"S1A_IW_GRDH_1SDV_{dt}_{dt[9:]}_0_X_X",
            collection="sentinel-1-grd", datetime=f"{date}T{dt[9:11]}:00:00Z",
            date=date, cloud=None, orbit_state=orbit, relative_orbit=rel,
        )

    asc = InterferometricPair(
        first=scene("2026-01-05", "20260105T234830", "ascending", 92),
        second=scene("2026-01-29", "20260129T234830", "ascending", 92),
    )
    desc = InterferometricPair(
        first=scene("2026-01-05", "20260105T112311", "descending", 84),
        second=scene("2026-01-29", "20260129T112311", "descending", 84),
    )
    assert pair_dir_name(asc) != pair_dir_name(desc)
    assert "asc92" in pair_dir_name(asc)
    assert "desc84" in pair_dir_name(desc)


def test_pair_dir_for_accepts_legacy_dirs_only_for_the_matching_track(tmp_path):
    # Legacy date-only dirs (pre-track naming) are reused ONLY when their
    # runconfig proves they were built from THIS pair's pass — otherwise a
    # same-dates opposite-track dir would feed the wrong geometry.
    from mirarsetena.coherence.pairs import InterferometricPair
    from mirarsetena.pipeline.catalog import Scene
    from tools.batch_insar import pair_dir_for

    def pair(orbit, rel, hhmm):
        first = Scene(
            id=f"S1A_IW_GRDH_1SDV_20260105T{hhmm}_20260105T{hhmm}_0_X_X",
            collection="sentinel-1-grd",
            datetime=f"2026-01-05T{hhmm[:2]}:{hhmm[2:]}:00Z",
            date="2026-01-05", cloud=None, orbit_state=orbit, relative_orbit=rel,
        )
        second = Scene(
            id=f"S1A_IW_GRDH_1SDV_20260129T{hhmm}_20260129T{hhmm}_0_X_X",
            collection="sentinel-1-grd",
            datetime=f"2026-01-29T{hhmm[:2]}:{hhmm[2:]}:00Z",
            date="2026-01-29", cloud=None, orbit_state=orbit, relative_orbit=rel,
        )
        return InterferometricPair(first=first, second=second)

    legacy = tmp_path / "2026-01-05_2026-01-29"
    legacy.mkdir()
    (legacy / "topsapp_runconfig.xml").write_text(
        "<!-- SAFE S1A_IW_SLC__1SDV_20260105T234829_... -->"
    )
    asc = pair("ascending", 92, "234830")
    desc = pair("descending", 84, "112311")

    # matching track (23:48 in the runconfig) -> the legacy dir is reused
    assert pair_dir_for(asc, tmp_path) == legacy
    # mismatched track (11:23 vs the dir's 23:48) -> a fresh modern dir
    fresh = pair_dir_for(desc, tmp_path)
    assert fresh != legacy
    assert "desc84" in fresh.name


def test_filter_pairs_by_chain():
    # Measured 2026-10-19: chain-filtered probe indexes were applied to
    # the UNFILTERED list — the S1A-asc batch processed descending pairs.
    # The driver now filters by chain before indexing.
    import json as _json

    from tools.batch_insar import filter_chain

    payload = _json.loads((FIXTURES / "stac_s1_grd.json").read_text(encoding="utf-8"))
    scenes = parse_scenes("sentinel-1-grd", payload)
    pairs = plan_pairs(scenes)
    asc = filter_chain(pairs, "S1A-asc")
    assert all(p.second.orbit_state == "ascending" for p in asc)
    assert len(asc) < len(pairs)  # the fixture mixes asc and desc
    # no filter -> unchanged
    assert filter_chain(pairs, None) == pairs


def test_spike_select_pair_honors_chain():
    # The batch driver filters by chain but passes only --pair-index to
    # spike, which re-planned WITHOUT the filter and picked the
    # descending pair again (measured 2026-10-19, caught before an
    # hour of topsApp). The selection is one shared function now.
    import json as _json

    from tools.spike_insar import select_pair

    payload = _json.loads((FIXTURES / "stac_s1_grd.json").read_text(encoding="utf-8"))
    scenes = parse_scenes("sentinel-1-grd", payload)
    pair = select_pair(scenes, index=0, min_days=23, max_days=25, chain="S1D-asc")
    assert pair.second.orbit_state == "ascending"
    # no chain -> the unfiltered list's index 0 (fixture: first 24-day pair)
    unfiltered = select_pair(scenes, index=0, min_days=23, max_days=25, chain=None)
    assert unfiltered is not None


def test_sbas_references_each_component_at_its_own_first_date():
    # The 36-day bridge decorrelated (0 valid px, wet-season transition),
    # splitting the S1A-asc chain: component 2 must still solve —
    # referenced at ITS first date, not reported as NaN forever.
    import numpy as np

    from mirarsetena.coherence.sbas import solve_sbas

    dates = ["2026-01-05", "2026-01-17", "2026-04-23", "2026-05-05"]
    edges = [
        ("2026-01-05", "2026-01-17", np.array(1.0)),
        ("2026-04-23", "2026-05-05", np.array(-2.0)),  # disconnected
    ]
    dates_out, fields, valid = solve_sbas(dates, edges)
    assert valid
    # component 1 referenced at 01-05
    assert float(fields[0][0]) == 0.0
    assert float(fields[1][0]) == pytest.approx(1.0)
    # component 2 referenced at ITS first date (04-23), not NaN
    assert float(fields[2][0]) == 0.0
    assert float(fields[3][0]) == pytest.approx(-2.0)

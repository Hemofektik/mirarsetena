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
    keep = ["merged", "unwrap2"]
    heavy = [
        "fine_coreg", "fine_offsets", "fine_interferogram",
        "coarse_coreg", "coarse_offsets", "coarse_interferogram",
        "ESD", "geom_reference", "reference", "secondary", "PICKLE", "unwrap",
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

"""D.4 — layer-driven date list API.

Seams: mirarsetena.pipeline.dates.list_dates (service, injectable clock and
catalog) and the FastAPI route GET /p/{slug}/api/dates.
Golden values from the committed STAC fixtures (25 S2 dates / 8 S1 dates).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mirarsetena.app import create_app
from mirarsetena.pipeline.catalog import parse_scenes
from mirarsetena.pipeline.dates import list_dates, mission_for_layer
from mirarsetena.pipeline.window import scene_meta_key
from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.storage import LocalStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def fake_search(collection, bbox, start, end):
    name = {
        "sentinel-2-l2a": "stac_s2_l2a.json",
        "sentinel-1-grd": "stac_s1_grd.json",
    }.get(collection)
    if name is None:
        return []
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return parse_scenes(collection, payload)


@pytest.fixture
def storage(tmp_path):
    return LocalStore(tmp_path / "cache")


@pytest.fixture
def registry():
    return ProjectRegistry(CONFIG_DIR)


def test_layer_implies_mission():
    assert mission_for_layer("ndvi") == "sentinel-2-l2a"
    assert mission_for_layer("rgb") == "sentinel-2-l2a"
    assert mission_for_layer("sigma0") == "sentinel-1-grd"
    assert mission_for_layer("coherence") == "sentinel-1-slc"


def test_s2_layer_returns_sorted_dates_with_cloud(storage, registry):
    result = list_dates(
        storage, "cdp-rio-general", registry.get("cdp-rio-general"),
        "ndvi", search_fn=fake_search, now=NOW,
    )
    dates = result["dates"]
    assert len(dates) == 25
    assert [d["date"] for d in dates] == sorted(d["date"] for d in dates)
    assert dates[0]["date"] == "2026-07-03"
    assert all(isinstance(d["cloud"], float) for d in dates)
    assert result["mission"] == "sentinel-2-l2a"
    assert result["timeline"]["start"] == "2026-01-01"
    assert result["timeline"]["works_start"] == "2026-06-01"


def _with_radar(config, layers):
    """Config copy with radar layers re-added: the display config dropped
    them (2026-10-12) but the backend S1 machinery stays covered."""
    return config.model_copy(update={"layers": list(layers)})


def test_s1_layer_returns_eight_dates(storage, registry):
    config = _with_radar(
        registry.get("cdp-rio-general"),
        ["ndvi", "rgb", "mndwi", "bsi", "sigma0"],
    )
    result = list_dates(
        storage, "cdp-rio-general", config,
        "sigma0", search_fn=fake_search, now=NOW,
    )
    assert len(result["dates"]) == 8
    assert all(d["cloud"] is None for d in result["dates"])


def test_coherence_layer_is_empty_before_slc_catalog_exists(storage, registry):
    config = _with_radar(
        registry.get("cdp-rio-general"), ["ndvi", "coherence"]
    )
    result = list_dates(
        storage, "cdp-rio-general", config,
        "coherence", search_fn=fake_search, now=NOW,
    )
    assert result["dates"] == []
    assert result["mission"] == "sentinel-1-slc"


def test_insar_layer_dates_come_from_interferometric_pairs(storage, registry):
    # SLC is not catalog-searchable; InSAR dates are the END dates of the
    # planned 12-day interferometric pairs (GRD metadata shares the passes).
    config = _with_radar(
        registry.get("cdp-rio-general"), ["ndvi", "insar"]
    )
    result = list_dates(
        storage, "cdp-rio-general", config,
        "insar", search_fn=fake_search, now=NOW,
    )
    dates = [d["date"] for d in result["dates"]]
    assert result["mission"] == "sentinel-1-slc"
    # 6 pairs from the 8-scene GRD fixture (5 asc rel-92 + 1 desc rel-84)
    assert dates == [
        "2026-07-23", "2026-08-04", "2026-08-16",
        "2026-08-28", "2026-09-09", "2026-10-03",
    ]
    # cloud is always None for radar-derived (no SCL); each carries orbits
    assert all(d["cloud"] is None for d in result["dates"])
    assert all(d["processed"] is False for d in result["dates"])


def test_max_cloud_filters_dates(storage, registry):
    config = registry.get("cdp-rio-general")
    result = list_dates(
        storage, "cdp-rio-general", config, "ndvi",
        max_cloud=5, search_fn=fake_search, now=NOW,
    )
    assert all(d["cloud"] <= 5 for d in result["dates"])
    full = list_dates(
        storage, "cdp-rio-general", config, "ndvi", search_fn=fake_search, now=NOW
    )
    assert len(result["dates"]) < len(full["dates"])


def test_processed_cloud_overrides_catalog_cloud(storage, registry):
    config = registry.get("cdp-rio-general")
    meta_key = scene_meta_key("cdp-rio-general", "sentinel-2-l2a", "2026-07-03")
    storage.put(meta_key, json.dumps({"cloud": 12.34, "date": "2026-07-03"}).encode())
    result = list_dates(
        storage, "cdp-rio-general", config, "ndvi", search_fn=fake_search, now=NOW
    )
    first = result["dates"][0]
    assert first["date"] == "2026-07-03"
    assert first["cloud"] == pytest.approx(12.34)
    assert first["processed"] is True


def test_index_is_reused_within_ttl_then_refreshed(storage, registry):
    config = registry.get("cdp-rio-general")
    calls = {"n": 0}

    def counting_search(collection, bbox, start, end):
        calls["n"] += 1
        return fake_search(collection, bbox, start, end)

    list_dates(storage, "cdp-rio-general", config, "ndvi",
               search_fn=counting_search, now=NOW)
    after_first = calls["n"]
    assert after_first >= 1

    list_dates(storage, "cdp-rio-general", config, "ndvi",
               search_fn=counting_search, now=NOW + timedelta(hours=5))
    assert calls["n"] == after_first  # fresh index, no re-query

    list_dates(storage, "cdp-rio-general", config, "ndvi",
               search_fn=counting_search, now=NOW + timedelta(hours=7))
    assert calls["n"] > after_first  # stale -> refresh


def test_route_serves_dates_and_maps_errors(tmp_path):
    app = create_app(
        config_dir=CONFIG_DIR, storage_root=tmp_path, search_fn=fake_search
    )
    client = TestClient(app)
    ok = client.get("/p/cdp-rio-general/api/dates", params={"layer": "ndvi"})
    assert ok.status_code == 200
    body = ok.json()
    assert len(body["dates"]) == 25

    bad_layer = client.get("/p/cdp-rio-general/api/dates", params={"layer": "nope"})
    assert bad_layer.status_code == 400

    missing = client.get("/p/unknown/api/dates", params={"layer": "ndvi"})
    assert missing.status_code == 404

    filtered = client.get(
        "/p/cdp-rio-general/api/dates",
        params={"layer": "ndvi", "max_cloud": 5},
    )
    assert filtered.status_code == 200
    assert all(d["cloud"] <= 5 for d in filtered.json()["dates"])


def test_first_dates_fetch_triggers_the_eager_coherence_job(tmp_path, fake_search):
    """SCOPE R2-Q1 + INSAR-PLAN L.5: first plain-data view starts exactly one
    eager coherence job AND one eager InSAR job (independent stacks)."""
    app = create_app(
        config_dir=CONFIG_DIR,
        storage_root=tmp_path / "cache",
        search_fn=fake_search,
    )
    client = TestClient(app)

    client.get("/p/cdp-rio-general/api/dates", params={"layer": "ndvi"})
    assert app.state.queue.counts() == {"pending": 2}
    assert app.state.queue.last_job("coherence_eager") is not None
    assert app.state.queue.last_job("insar_eager") is not None

    client.get("/p/cdp-rio-general/api/dates", params={"layer": "ndvi"})
    assert app.state.queue.counts() == {"pending": 2}  # still exactly one each


def test_insar_dates_carry_los_range_from_stored_products(storage, registry):
    # The legend labels and the tile ramp must agree, so the dates response
    # carries the adaptive +-cm limit computed from the stored products
    # (INSAR-PLAN decision 5 gate: the fixed +-5 cm ramp rendered the real
    # product all-white).
    import numpy as np
    from rasterio.transform import from_origin

    from mirarsetena.pipeline.indices import layer_key

    config = _with_radar(registry.get("cdp-rio-general"), ["ndvi", "insar"])

    def encode(values):
        from rasterio.io import MemoryFile

        h, w = values.shape
        profile = {
            "driver": "GTiff", "dtype": "float32", "count": 1,
            "height": h, "width": w, "crs": "EPSG:4326",
            "transform": from_origin(-83.675, 9.397, 0.00375, 0.00475),
            "nodata": -9999.0, "compress": "deflate",
        }
        with MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(values.astype(np.float32), 1)
            return mem.read()

    # one product with a known spread: -1 .. +3 cm
    values = np.full((6, 6), 1.0, dtype=np.float32)
    values[0, :] = -1.0
    values[1, :] = 3.0
    key = layer_key("cdp-rio-general", "sentinel-1-slc", "2026-10-03", "insar")
    storage.put(key, encode(values))

    result = list_dates(
        storage, "cdp-rio-general", config,
        "insar", search_fn=fake_search, now=NOW,
    )
    limit = result.get("los_range")
    assert limit is not None, "insar dates must carry the adaptive ramp limit"
    assert limit >= 3.0          # covers the +3 cm pixels
    assert limit <= 6.0          # symmetric-ish, not absurdly wide


def test_insar_date_entries_carry_their_own_los_range(storage, registry):
    # The legend follows the SELECTED date, so each processed entry carries
    # its own limit (a layer-wide max would mislabel a smaller product).
    import numpy as np
    from rasterio.transform import from_origin

    from mirarsetena.pipeline.indices import layer_key

    config = _with_radar(registry.get("cdp-rio-general"), ["ndvi", "insar"])

    def encode(values):
        from rasterio.io import MemoryFile

        h, w = values.shape
        profile = {
            "driver": "GTiff", "dtype": "float32", "count": 1,
            "height": h, "width": w, "crs": "EPSG:4326",
            "transform": from_origin(-83.675, 9.397, 0.00375, 0.00475),
            "nodata": -9999.0, "compress": "deflate",
        }
        with MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(values.astype(np.float32), 1)
            return mem.read()

    # two products with DIFFERENT spreads: +-1 cm and +-3 cm
    small = np.full((6, 6), 1.0, dtype=np.float32)
    big = np.full((6, 6), 3.0, dtype=np.float32)
    storage.put(
        layer_key("cdp-rio-general", "sentinel-1-slc", "2026-08-16", "insar"),
        encode(small),
    )
    storage.put(
        layer_key("cdp-rio-general", "sentinel-1-slc", "2026-10-03", "insar"),
        encode(big),
    )

    result = list_dates(
        storage, "cdp-rio-general", config,
        "insar", search_fn=fake_search, now=NOW,
    )
    by_date = {d["date"]: d for d in result["dates"]}
    assert by_date["2026-08-16"]["los_range"] == pytest.approx(1.0, abs=1e-3)
    assert by_date["2026-10-03"]["los_range"] == pytest.approx(3.0, abs=1e-3)
    # unprocessed dates carry no range (nothing rendered yet)
    assert by_date["2026-07-23"]["los_range"] is None
    # layer-wide summary = max over processed products
    assert result["los_range"] == pytest.approx(3.0, abs=1e-3)


def test_insar_dates_carry_pair_with_start_dates(storage, registry):
    # An insar date is a PAIR's end date: the product shows the DELTA from
    # the previous same-track acquisition, and the UI must be able to say
    # which dates that delta spans (user 2026-10-14).
    config = _with_radar(
        registry.get("cdp-rio-general"), ["ndvi", "insar"]
    )
    result = list_dates(
        storage, "cdp-rio-general", config,
        "insar", search_fn=fake_search, now=NOW,
    )
    pair_with = {d["date"]: d["pair_with"] for d in result["dates"]}
    # golden values from the 8-scene GRD fixture (5 asc + 1 desc pairs,
    # each 12 days): end date -> start dates of the delta
    assert pair_with == {
        "2026-07-23": ["2026-07-11"],
        "2026-08-04": ["2026-07-23"],
        "2026-08-16": ["2026-08-04"],
        "2026-08-28": ["2026-08-16"],
        "2026-09-09": ["2026-08-28"],
        "2026-10-03": ["2026-09-21"],
    }

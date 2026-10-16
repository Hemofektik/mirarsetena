"""L.3/L.6 — InSAR phase → LOS displacement product (the "height delta").

Seam: mirarsetena.pipeline.insar (phase→cm conversion, coherence mask,
temporal accumulation, full product build from SNAP outputs). SNAP itself is
the injectable processor (Phase H convention); everything here is the
pure-data side that turns an unwrapped interferogram + coherence into a
colour-renderable LOS-centimetre product — no SNAP/CDSE needed to test.
"""
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mirarsetena.pipeline.indices import NODATA
from mirarsetena.pipeline.insar import (
    C_BAND_WAVELENGTH_M,
    accumulate_los,
    build_los_product,
    los_range,
    mask_by_coherence,
    phase_to_los_cm,
    reference_los,
)

# C-band Sentinel-1 carrier wavelength; one full fringe = λ/2 = 2.77 cm.
LAMBDA = 0.055465763


def test_wavelength_is_sentinel1_c_band():
    assert C_BAND_WAVELENGTH_M == pytest.approx(5.5465763e-2)


def test_phase_to_los_goldens():
    # A full fringe (φ = 2π) equals λ/2 of range change = 2.773 cm.
    assert phase_to_los_cm(2 * np.pi) == pytest.approx(2.7732881, abs=1e-6)
    # φ = +π (half a fringe, range increasing) → +λ/4 = +1.3866 cm.
    assert phase_to_los_cm(np.pi) == pytest.approx(1.3866441, abs=1e-6)
    # Zero phase → zero displacement.
    assert phase_to_los_cm(0.0) == pytest.approx(0.0)
    # Sign flips with phase sign.
    assert phase_to_los_cm(-np.pi) == pytest.approx(-1.3866441, abs=1e-6)


def test_phase_to_los_handles_arrays():
    values = np.array([0.0, np.pi, -np.pi, 2 * np.pi], dtype=np.float32)
    out = phase_to_los_cm(values)
    assert out.dtype == np.float32
    assert out[0] == pytest.approx(0.0)
    assert out[1] == pytest.approx(1.3866441, abs=1e-5)
    assert out[3] == pytest.approx(2.7732881, abs=1e-5)


def test_mask_by_coherence_nulls_low_coherence():
    los = np.array([1.0, -2.0, 0.5], dtype=np.float32)
    coherence = np.array([0.8, 0.1, 0.35], dtype=np.float32)
    masked = mask_by_coherence(los, coherence, threshold=0.35)
    assert masked[0] == pytest.approx(1.0)  # coherent, kept
    assert masked[1] == NODATA               # 0.1 < 0.35 → nodata
    # threshold is inclusive at the boundary (coherence == threshold kept)
    assert masked[2] == pytest.approx(0.5)


def test_mask_by_coherence_respects_existing_nodata():
    los = np.array([NODATA, 1.0], dtype=np.float32)
    coherence = np.array([0.9, 0.9], dtype=np.float32)
    masked = mask_by_coherence(los, coherence, threshold=0.35)
    assert masked[0] == NODATA  # already nodata stays nodata


def test_accumulate_los_sums_over_time_from_reference():
    # Three pairwise LOS products (each vs the previous date); the
    # accumulated series is the running sum from a zero reference.
    a = np.array([1.0, 0.0, -1.0], dtype=np.float32)
    b = np.array([0.5, 0.0, -0.5], dtype=np.float32)
    c = np.array([2.0, 1.0, 0.0], dtype=np.float32)
    series = accumulate_los([a, b, c])
    assert len(series) == 2  # n-1 accumulated deltas for n products
    # series[0] = a+b = [1.5, 0.0, -1.5]
    assert series[0][0] == pytest.approx(1.5)
    assert series[0][2] == pytest.approx(-1.5)
    # series[1] = a+b+c = [3.5, 1.0, -1.5]
    assert series[1][0] == pytest.approx(3.5)
    assert series[1][1] == pytest.approx(1.0)
    assert series[1][2] == pytest.approx(-1.5)


def test_accumulate_los_skips_nodata_not_zero_fill():
    a = np.array([1.0, NODATA, 3.0], dtype=np.float32)
    b = np.array([1.0, 5.0, NODATA], dtype=np.float32)
    series = accumulate_los([a, b])
    assert series[0][0] == pytest.approx(2.0)  # 1+1
    # pixel 1: a is nodata, b is 5 → sum = 5 (not masked out, not zero)
    assert series[0][1] == pytest.approx(5.0)
    # pixel 2: a is 3, b is nodata → sum = 3
    assert series[0][2] == pytest.approx(3.0)


def test_accumulate_los_all_nodata_stays_nodata():
    a = np.array([NODATA, 1.0], dtype=np.float32)
    b = np.array([NODATA, 1.0], dtype=np.float32)
    series = accumulate_los([a, b])
    assert series[0][0] == NODATA
    assert series[0][1] == pytest.approx(2.0)


def _geo(height=6, width=6, crs="EPSG:4326"):
    transform = from_origin(-83.675, 9.397, 0.0025, 0.00316)
    return {"crs": crs, "transform": transform, "height": height, "width": width}


def _encode(values, like, nodata=NODATA):
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": like["height"], "width": like["width"],
        "crs": like["crs"], "transform": like["transform"],
        "nodata": nodata, "compress": "deflate",
    }
    from rasterio.io import MemoryFile

    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(values.astype(np.float32), 1)
        return mem.read()


def test_build_los_product_converts_and_masks(tmp_path):
    like = _geo()
    phase = np.full((like["height"], like["width"]), np.pi, dtype=np.float32)
    coherence = np.full((like["height"], like["width"]), 0.9, dtype=np.float32)
    coherence[0, 0] = 0.05  # one incoherent pixel

    phase_bytes = _encode(phase, like, nodata=None)
    coherence_bytes = _encode(coherence, like, nodata=None)

    product = build_los_product(phase_bytes, coherence_bytes, threshold=0.35)
    with rasterio.io.MemoryFile(product) as mem, mem.open() as src:
        data = src.read(1)
        assert src.crs.to_string() == "EPSG:4326"
        assert src.nodata == NODATA
    # coherent pixels carry the LOS height delta (+λ/4 cm)
    assert data[3, 3] == pytest.approx(1.3866441, abs=1e-5)
    # the incoherent pixel is masked to nodata
    assert data[0, 0] == NODATA


def test_reference_los_subtracts_median_of_reference_area():
    # An unwrap's absolute offset is arbitrary (measured: the spike's raw
    # product sat at -6.20 cm median, 100% saturated at the +-5 cm ramp).
    # Referencing to a stable area re-zeroes it: median of the coherent
    # reference pixels is subtracted from every valid pixel.
    like = _geo()
    phase = np.full((like["height"], like["width"]), 2 * np.pi, dtype=np.float32)
    coherence = np.full((like["height"], like["width"]), 0.9, dtype=np.float32)
    product = build_los_product(
        _encode(phase, like, nodata=None),
        _encode(coherence, like, nodata=None),
        threshold=0.35,
    )
    # reference = the first ROW (all coherent, all +2.773 cm)
    reference = np.zeros((like["height"], like["width"]), dtype=bool)
    reference[0, :] = True

    out = reference_los(product, reference)
    with rasterio.io.MemoryFile(out) as mem, mem.open() as src:
        data = src.read(1)
    # the reference median (+2.773) is removed -> everything reads 0
    assert data[3, 3] == pytest.approx(0.0, abs=1e-5)
    assert data[1, 1] == pytest.approx(0.0, abs=1e-5)


def test_reference_los_keeps_nodata_and_ignores_nodata_reference():
    like = _geo()
    phase = np.full((like["height"], like["width"]), np.pi, dtype=np.float32)
    coherence = np.full((like["height"], like["width"]), 0.9, dtype=np.float32)
    coherence[0, 0] = 0.05  # masked -> nodata, and it sits IN the reference
    product = build_los_product(
        _encode(phase, like, nodata=None),
        _encode(coherence, like, nodata=None),
        threshold=0.35,
    )
    reference = np.zeros((like["height"], like["width"]), dtype=bool)
    reference[0, :] = True  # includes the nodata pixel at [0, 0]

    out = reference_los(product, reference)
    with rasterio.io.MemoryFile(out) as mem, mem.open() as src:
        data = src.read(1)
    # nodata reference pixels must not drag the median: the coherent
    # reference pixels are all +1.3866 -> subtracted exactly
    assert data[3, 3] == pytest.approx(0.0, abs=1e-5)
    # the masked pixel stays nodata (never fabricated into a value)
    assert data[0, 0] == NODATA


def test_reference_los_without_coherent_reference_is_unchanged():
    like = _geo()
    phase = np.full((like["height"], like["width"]), np.pi, dtype=np.float32)
    coherence = np.full((like["height"], like["width"]), 0.9, dtype=np.float32)
    product = build_los_product(
        _encode(phase, like, nodata=None),
        _encode(coherence, like, nodata=None),
        threshold=0.35,
    )
    reference = np.zeros((like["height"], like["width"]), dtype=bool)
    # reference area is entirely nodata (e.g. all water) -> nothing to
    # re-zero against; the product must come back untouched, honestly.
    with rasterio.io.MemoryFile(product) as mem, mem.open() as src:
        before = src.read(1)
    out = reference_los(product, reference)
    with rasterio.io.MemoryFile(out) as mem, mem.open() as src:
        after = src.read(1)
    np.testing.assert_array_equal(after, before)


def test_los_range_is_symmetric_around_zero_from_valid_percentiles():
    # The fixed +-5 cm ramp rendered the real product all-white: the
    # referenced product spans only +-0.56 cm, so every pixel landed on the
    # ramp's white centre. The range must come from the DATA (plan decision
    # 5 gate: "adaptive only after real data says +-5 is wrong").
    like = _geo()
    phase = np.full((like["height"], like["width"]), 0.0, dtype=np.float32)
    coherence = np.full((like["height"], like["width"]), 0.9, dtype=np.float32)
    # a spread of real values: -2 .. +4 cm worth of phase
    phase[0, :] = -2 * 4 * np.pi / 0.055465763 * 0.01   # ~ -2 cm
    phase[1, :] = 4 * 4 * np.pi / 0.055465763 * 0.01    # ~ +4 cm
    product = build_los_product(
        _encode(phase, like, nodata=None),
        _encode(coherence, like, nodata=None),
        threshold=0.35,
    )
    limit = los_range(product)
    # symmetric around 0 (the diverging ramp is centred on 0)
    assert limit > 0
    # covers the data: the +4 cm pixels must not clip at the top
    # (float32 product values -> tiny tolerance)
    assert limit >= 4.0 - 1e-4
    # but is not absurdly wider than the data (p99-based, not max-based)
    assert limit <= 8.0


def test_los_range_ignores_nodata_and_handles_empty():
    like = _geo()
    coherence = np.full((like["height"], like["width"]), 0.9, dtype=np.float32)
    coherence[0, 0] = 0.05  # -> nodata
    phase = np.full((like["height"], like["width"]), np.pi, dtype=np.float32)
    product = build_los_product(
        _encode(phase, like, nodata=None),
        _encode(coherence, like, nodata=None),
        threshold=0.35,
    )
    limit = los_range(product)
    # nodata (-9999) must not drag the range: a lone masked pixel would
    # otherwise blow the ramp to -9999
    assert 0 < limit < 10

    # fully-masked product -> no valid pixels -> no range to report
    masked = build_los_product(
        _encode(phase, like, nodata=None),
        _encode(np.full_like(coherence, 0.01), like, nodata=None),
        threshold=0.35,
    )
    assert los_range(masked) is None


def test_sample_pois_reads_values_nulls_nodata_and_outside():
    """L.9 Plan vs. Realidad: per-POI LOS values come from the product.

    A POI on a coherence-masked pixel or outside the product footprint is
    an honest null (a gap the chart must NOT interpolate across), not 0.
    """
    from mirarsetena.pipeline.insar import sample_pois

    like = _geo()
    grid = np.arange(36, dtype=np.float32).reshape(6, 6) / 10.0
    grid[0, 0] = NODATA
    product = _encode(grid, like)

    pois = [
        {"id": "inside", "coordinates": [-83.670, 9.390]},   # pixel (2,2)=1.4
        {"id": "gap", "coordinates": [-83.674, 9.396]},      # pixel (0,0) nodata
        {"id": "outside", "coordinates": [-83.500, 9.500]},  # beyond footprint
    ]
    out = sample_pois(product, pois)
    assert out == [
        {"id": "inside", "los_cm": 1.4},
        {"id": "gap", "los_cm": None},
        {"id": "outside", "los_cm": None},
    ]


def test_write_insar_series_stores_sampled_pois_beside_product(tmp_path):
    """L.9: the series entry lives next to the product under a stable key."""
    import json

    from mirarsetena.pipeline.insar import write_insar_series
    from mirarsetena.storage import LocalStore

    like = _geo()
    grid = np.full((like["height"], like["width"]), 0.25, dtype=np.float32)
    grid[0, 0] = NODATA
    product = _encode(grid, like)

    store = LocalStore(tmp_path)
    key = write_insar_series(
        store,
        "cdp-rio-general",
        "2026-10-03",
        product,
        [
            {"id": "inside", "coordinates": [-83.670, 9.390]},
            {"id": "gap", "coordinates": [-83.674, 9.396]},
        ],
    )
    assert key == (
        "p/cdp-rio-general/layers/sentinel-1-slc_2026-10-03_insar_series.json"
    )
    payload = json.loads(store.get(key))
    assert payload["date"] == "2026-10-03"
    assert payload["pois"] == [
        {"id": "inside", "los_cm": 0.25},
        {"id": "gap", "los_cm": None},
    ]
    # the per-date ramp limit rides along so the chart axis can match the map
    assert payload["los_range"] is not None and payload["los_range"] >= 0.5



def test_write_insar_series_carries_the_chain_identity(tmp_path):
    # SBAS aggregation groups measurements by CHAIN (satellite + track):
    # S1A and S1D pairs can never be phase-tied, so their series must stay
    # separable all the way to the chart.
    import json

    from mirarsetena.pipeline.insar import write_insar_series
    from mirarsetena.storage import LocalStore

    like = _geo()
    product = _encode(np.full((like["height"], like["width"]), 0.25, dtype=np.float32), like)
    store = LocalStore(tmp_path)
    key = write_insar_series(
        store, "cdp-rio-general", "2026-03-06", product,
        [{"id": "quarry", "coordinates": [-83.670, 9.390]}],
        chain="S1A-asc",
    )
    payload = json.loads(store.get(key))
    assert payload["chain"] == "S1A-asc"
    # no chain passed -> field absent (older series stay valid)
    key2 = write_insar_series(
        store, "cdp-rio-general", "2026-10-03", product,
        [{"id": "quarry", "coordinates": [-83.670, 9.390]}],
    )
    assert "chain" not in json.loads(store.get(key2))


def test_edge_key_keeps_sbас_products_out_of_layer_namespace():
    # 24-day closer products share END DATES with served 12-day products;
    # their geocoded edges live in the sbas/ namespace, never under the
    # layer key the tiles/series read.
    from mirarsetena.pipeline.insar import edge_key

    key = edge_key("cdp-rio-general", "2026-07-23", "2026-08-16")
    assert key == "p/cdp-rio-general/sbas/edges/2026-07-23_2026-08-16.tif"
    assert "/layers/" not in key

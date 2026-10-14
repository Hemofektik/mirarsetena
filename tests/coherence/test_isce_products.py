"""ISCE2 product readers: sidecar-driven layout + snaphu output format.

Seam: mirarsetena.coherence.isce_products.

Two measured bugs this pins (spike run 2026-10-03):

1. `topophase.cor` is declared **BIL, 2 bands** in its `.xml` sidecar, but
   the old reader reshaped it as BSQ band 0 — which is the *magnitude* band,
   not coherence. snaphu then saw ~49% illegal correlation values
   ("49075 illegal correlation values clipped to [0,1]").
2. snaphu's unwrapped output is **BIL per line, band 1 = phase, band 0 =
   amplitude** (verified against a synthetic ramp: mod-2pi invariant holds
   at frac 1.0 only under that layout). The old reader used BIP, so the
   reported LOS statistics were garbage (p2p 197805 cm instead of 12.6 cm).

The readers take the layout from the sidecar (never a hardcoded guess) and
`read_snaphu_unw` pins snaphu's documented ALT_LINE_DATA output.
"""
from __future__ import annotations

import numpy as np
import pytest

from mirarsetena.coherence.isce_products import (
    read_isce_image,
    read_snaphu_unw,
    sidecar_layout,
)

# The real sidecar from the spike run: BIL, 2 bands, FLOAT, width 1286,
# length 1748 — the layout the old reader got wrong.
REAL_SIDECAR = (
    "data/spike/spike-insar/2026-10-03/merged/topophase.cor.xml"
)


def _write_sidecar(path, *, width, length, scheme, bands, data_type="FLOAT"):
    path.write_text(
        f"""<?xml version="1.0"?>
<imageFile>
    <property name="width"><value>{width}</value></property>
    <property name="length"><value>{length}</value></property>
    <property name="scheme"><value>{scheme}</value></property>
    <property name="number_bands"><value>{bands}</value></property>
    <property name="data_type"><value>{data_type}</value></property>
    <property name="byte_order"><value>l</value></property>
    <component name="coordinate1">
        <property name="size"><value>999</value></property>
    </component>
</imageFile>
"""
    )


def test_sidecar_layout_reads_declared_geometry(tmp_path):
    xml = tmp_path / "img.xml"
    _write_sidecar(xml, width=4, length=3, scheme="BIL", bands=2)
    layout = sidecar_layout(xml)
    assert layout.width == 4
    assert layout.length == 3
    assert layout.scheme == "BIL"
    assert layout.bands == 2
    assert layout.dtype == np.float32


def test_sidecar_layout_ignores_nested_coordinate_sizes(tmp_path):
    # coordinate1/coordinate2 carry their own `size` properties (the radar
    # burst geometry); only the TOP-LEVEL width/length describe the raster.
    xml = tmp_path / "img.xml"
    _write_sidecar(xml, width=4, length=3, scheme="BIP", bands=1)
    layout = sidecar_layout(xml)
    assert (layout.width, layout.length) == (4, 3)


def test_read_isce_image_bil_two_bands(tmp_path):
    # BIL = band-interleaved-by-line: [row, band, col].
    width, length, bands = 4, 3, 2
    data = np.arange(length * bands * width, dtype=np.float32)
    bin_path = tmp_path / "cor"
    bin_path.write_bytes(data.tobytes())
    _write_sidecar(
        tmp_path / "cor.xml", width=width, length=length,
        scheme="BIL", bands=bands,
    )
    out = read_isce_image(bin_path)
    assert out.shape == (bands, length, width)
    # band 1 of BIL is the second half of EACH line, not the second half
    # of the file (that would be BSQ).
    expected_band1 = data.reshape(length, 2, width)[:, 1, :]
    np.testing.assert_array_equal(out[1], expected_band1)


def test_read_isce_image_bsq_two_bands(tmp_path):
    width, length, bands = 4, 3, 2
    data = np.arange(length * bands * width, dtype=np.float32)
    (tmp_path / "img").write_bytes(data.tobytes())
    _write_sidecar(
        tmp_path / "img.xml", width=width, length=length,
        scheme="BSQ", bands=bands,
    )
    out = read_isce_image(tmp_path / "img")
    np.testing.assert_array_equal(out[1], data.reshape(bands, length, width)[1])


def test_read_isce_image_single_band_returns_2d(tmp_path):
    width, length = 4, 3
    data = np.arange(length * width, dtype=np.float32)
    (tmp_path / "phsig").write_bytes(data.tobytes())
    _write_sidecar(
        tmp_path / "phsig.xml", width=width, length=length,
        scheme="BIP", bands=1,
    )
    out = read_isce_image(tmp_path / "phsig")
    assert out.shape == (length, width)


def test_read_isce_image_rejects_size_mismatch(tmp_path):
    width, length, bands = 4, 3, 2
    (tmp_path / "short").write_bytes(b"\x00" * 8)  # far too small
    _write_sidecar(
        tmp_path / "short.xml", width=width, length=length,
        scheme="BIL", bands=bands,
    )
    with pytest.raises(ValueError, match="size"):
        read_isce_image(tmp_path / "short")


def test_read_snaphu_unw_returns_phase_and_amplitude(tmp_path):
    # snaphu ALT_LINE_DATA output: per line, all amplitude then all phase
    # (band 0 = amplitude, band 1 = phase — pinned by the mod-2pi invariant
    # on two real unwraps and a synthetic ramp: band 1 reproduces the input
    # wrapped phase at frac 1.0000).
    ch, cw = 5, 7
    phase = np.linspace(-3.0, 3.0, ch * cw, dtype=np.float32).reshape(ch, cw)
    amp = np.full((ch, cw), 42.0, dtype=np.float32)
    interleaved = np.empty((ch, 2, cw), dtype=np.float32)
    interleaved[:, 0, :] = amp
    interleaved[:, 1, :] = phase
    unw = tmp_path / "crop.unw"
    unw.write_bytes(interleaved.tobytes())

    got_phase, got_amp = read_snaphu_unw(unw, (ch, cw))
    np.testing.assert_array_equal(got_phase, phase.reshape(ch, cw))
    np.testing.assert_array_equal(got_amp, amp.reshape(ch, cw))


def test_read_snaphu_unw_rejects_wrong_size(tmp_path):
    unw = tmp_path / "crop.unw"
    unw.write_bytes(b"\x00" * 16)
    with pytest.raises(ValueError, match="size"):
        read_snaphu_unw(unw, (5, 7))


@pytest.mark.skipif(
    not __import__("pathlib").Path(REAL_SIDECAR).exists(),
    reason="spike sidecar not present",
)
def test_real_topophase_cor_sidecar_declares_bil_two_bands():
    # Regression pin for bug #1: the actual spike sidecar says BIL/2 bands,
    # so the reader must never guess BSQ band 0.
    layout = sidecar_layout(REAL_SIDECAR)
    assert layout.scheme == "BIL"
    assert layout.bands == 2
    assert (layout.width, layout.length) == (1286, 1748)

def _write_geo_vrts(merged, lat, lon):
    """Write the two ISCE geolocation rasters as GDAL-readable files with
    the production names (GDAL identifies the driver by content)."""
    from rasterio.io import MemoryFile

    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": lat.shape[0], "width": lat.shape[1],
    }
    for name, arr in (("lat.rdr.full.vrt", lat), ("lon.rdr.full.vrt", lon)):
        with MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(arr.astype(np.float32), 1)
            (merged / name).write_bytes(mem.read())


def _ascending_swath(length=60, width=80):
    """ASC-pass-shaped geolocation grid: lat AND lon increase with the
    indices (the 2026-07-23 ASC geometry the 08-04 batch pair used)."""
    lat = 9.2 + 0.005 * np.arange(length)[:, None] * np.ones((1, width))
    lon = -83.9 + 0.005 * np.arange(width)[None, :] * np.ones((length, 1))
    return lat, lon


def test_radar_window_never_touches_the_grid_edges(tmp_path):
    # Measured live failure (2026-10-14, 2026-08-04 pair): a window whose
    # r0 clipped to 0 fed a geolocation array whose FIRST ROW was included
    # — GDAL's geoloc warp then returned ZERO coverage (all-NODATA
    # product); dropping that one row restored 100%. The shared window
    # keeps a >=1 inset on every side.
    from mirarsetena.coherence.isce_products import radar_window

    lat, lon = _ascending_swath()
    merged = tmp_path / "merged"
    merged.mkdir()
    _write_geo_vrts(merged, lat, lon)
    length, width = lat.shape

    # bbox in the NORTH of the swath: rows.min() - margin clips to 0
    r0, r1, c0, c1 = radar_window(
        merged, (-83.675, 9.378, -83.660, 9.397), width, length
    )
    assert 1 <= r0 < r1 <= length - 1
    assert 1 <= c0 < c1 <= width - 1
    # the bbox itself stays inside the window
    assert r0 <= 35 and 44 <= r1  # rows of lat 9.378..9.397
    assert c0 <= 43 and 52 <= c1  # cols of lon -83.675..-83.660


def test_radar_window_then_geocode_covers_the_whole_bbox(tmp_path):
    # End-to-end pin of the inset: the windowed crop of an ASC swath
    # geocodes with FULL coverage (the pre-inset window measured 0/35404).
    from rasterio.io import MemoryFile

    from mirarsetena.coherence.geocode import geocode_radar_band
    from mirarsetena.coherence.isce_products import radar_window

    lat, lon = _ascending_swath()
    merged = tmp_path / "merged"
    merged.mkdir()
    _write_geo_vrts(merged, lat, lon)
    length, width = lat.shape
    bbox = (-83.675, 9.378, -83.660, 9.397)
    r0, r1, c0, c1 = radar_window(merged, bbox, width, length)

    tb = geocode_radar_band(
        np.ones((r1 - r0, c1 - c0), dtype=np.float32),
        lat[r0:r1, c0:c1], lon[r0:r1, c0:c1],
        bbox, resolution=0.0005,
    )
    with MemoryFile(tb) as mem, mem.open() as src:
        g = src.read(1)
    from mirarsetena.pipeline.indices import NODATA

    assert (g != NODATA).sum() == g.size  # every pixel covered

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
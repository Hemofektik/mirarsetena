"""Readers for ISCE2 product rasters — layout from the sidecar, never guessed.

ISCE writes raw binary alongside an `.xml` sidecar that declares the raster
geometry (`width`, `length`, `scheme`, `number_bands`, `data_type`). The
sidecar is the single source of truth: `topophase.cor` is BIL/2-band while
`phsig.cor` is BIP/1-band, and reading either as a hardcoded layout silently
produces garbage (measured: snaphu saw 49075 illegal correlation values when
the BIL coherence was read as BSQ band 0 — the magnitude band).

snaphu's unwrapped output has a fixed documented format (ALT_LINE_DATA):
per line, phase then amplitude. `read_snaphu_unw` pins that layout, which is
what makes the unwrap's mod-2pi invariant checkable.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_DTYPES = {
    "FLOAT": np.float32,
    "DOUBLE": np.float64,
    "CFLOAT": np.complex64,
    "CDOUBLE": np.complex128,
    "BYTE": np.uint8,
    "SHORT": np.int16,
    "INT": np.int32,
}


@dataclass(frozen=True)
class SidecarLayout:
    """Declared geometry of one ISCE raw-binary raster."""

    width: int
    length: int
    scheme: str  # BIP | BIL | BSQ
    bands: int
    dtype: np.dtype


def sidecar_layout(xml_path: str | Path) -> SidecarLayout:
    """Parse the top-level raster geometry from an ISCE `.xml` sidecar.

    Only TOP-LEVEL `width`/`length` count: the nested `coordinate1`/
    `coordinate2` components carry their own `size` properties (radar burst
    geometry) that must not be mistaken for the raster shape.
    """
    root = ET.parse(str(xml_path)).getroot()

    def top_level(name: str) -> str | None:
        for prop in root.findall("property"):
            if prop.get("name") == name:
                return prop.findtext("value")
        return None

    width = top_level("width")
    length = top_level("length")
    if width is None or length is None:
        raise ValueError(f"sidecar {xml_path} lacks width/length")
    scheme = (top_level("scheme") or "BIP").upper()
    bands = int(top_level("number_bands") or 1)
    dtype_name = (top_level("data_type") or "FLOAT").upper()
    if dtype_name not in _DTYPES:
        raise ValueError(f"unsupported ISCE data_type {dtype_name!r}")
    return SidecarLayout(
        width=int(width), length=int(length),
        scheme=scheme, bands=bands, dtype=np.dtype(_DTYPES[dtype_name]),
    )


def read_isce_image(bin_path: str | Path) -> np.ndarray:
    """Read a raw ISCE raster per its sidecar; (bands, length, width) or 2-D.

    The sidecar is `<bin_path>.xml`. Raises ValueError when the file size
    does not match the declared geometry (a truncated or misnamed file must
    not be silently reshaped).
    """
    bin_path = Path(bin_path)
    layout = sidecar_layout(Path(str(bin_path) + ".xml"))
    expected = layout.width * layout.length * layout.bands * layout.dtype.itemsize
    actual = bin_path.stat().st_size
    if actual != expected:
        raise ValueError(
            f"{bin_path.name}: size {actual} != declared "
            f"{layout.width}x{layout.length}x{layout.bands} "
            f"{layout.dtype.name} ({expected} bytes)"
        )
    flat = np.fromfile(bin_path, dtype=layout.dtype)
    if layout.bands == 1:
        return flat.reshape(layout.length, layout.width)
    if layout.scheme == "BIL":
        # band-interleaved-by-line: [row, band, col]
        return flat.reshape(layout.length, layout.bands, layout.width).transpose(
            1, 0, 2
        )
    if layout.scheme == "BIP":
        return flat.reshape(layout.length, layout.width, layout.bands).transpose(
            2, 0, 1
        )
    # BSQ: band sequential
    return flat.reshape(layout.bands, layout.length, layout.width)


def read_snaphu_unw(
    unw_path: str | Path, shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """snaphu ALT_LINE_DATA output → (phase, amplitude), each (rows, cols).

    snaphu writes per line: amplitude float32, phase float32 (BIL). The band
    roles are pinned by two independent measurements: on both real spike
    unwraps and on a synthetic ramp, band 1 reproduces the input wrapped
    phase mod 2π at frac 1.0000 while band 0 correlates 1.0000 with the
    interferogram magnitude. Reading it the other way round (or as BIP/BSQ)
    scrambles both bands — the measured cause of the spike's absurd 197805 cm
    peak-to-peak displacement.
    """
    rows, cols = shape
    expected = rows * cols * 2 * 4
    actual = Path(unw_path).stat().st_size
    if actual != expected:
        raise ValueError(
            f"{Path(unw_path).name}: size {actual} != {rows}x{cols} "
            f"ALT_LINE_DATA ({expected} bytes)"
        )
    raw = np.fromfile(unw_path, dtype=np.float32).reshape(rows, 2, cols)
    return raw[:, 1, :].copy(), raw[:, 0, :].copy()  # (phase, amplitude)
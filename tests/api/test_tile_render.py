"""F.1 — tile renderer: Web Mercator math + styles to RGBA PNG.

Seam: mirarsetena.tiles.render (bounds/validate/render/encode).
"""
import io

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_origin

from mirarsetena.pipeline.indices import NODATA
from mirarsetena.tiles.render import (
    TileError,
    encode_png,
    render_tile,
    tile_bounds_wgs84,
    validate_tile,
)

RAMP_STYLE = {
    "kind": "ramp",
    "stops": [(-1.0, (165, 15, 21)), (0.0, (255, 255, 191)), (1.0, (0, 104, 55))],
}
RGB_STYLE = {"kind": "rgb"}
LUT_STYLE = {
    "kind": "lut",
    "lut": {
        0: (215, 48, 39), 1: (253, 174, 97), 2: (254, 224, 144),
        3: (240, 240, 240), 4: (171, 217, 233), 5: (116, 173, 209),
        6: (69, 117, 180), 255: None,
    },
}


def test_tile_bounds_goldens():
    west, south, east, north = tile_bounds_wgs84(0, 0, 0)
    assert west == pytest.approx(-180.0)
    assert east == pytest.approx(180.0)
    assert north == pytest.approx(85.051129, abs=1e-5)
    assert south == pytest.approx(-85.051129, abs=1e-5)

    west, south, east, north = tile_bounds_wgs84(1, 0, 0)
    assert west == pytest.approx(-180.0)
    assert east == pytest.approx(0.0)
    assert south == pytest.approx(0.0, abs=1e-9)
    assert north == pytest.approx(85.051129, abs=1e-5)


def test_zoom_cap_and_tile_range():
    validate_tile(18, 0, 0, max_zoom=18)  # at the cap: ok
    with pytest.raises(TileError):
        validate_tile(19, 0, 0, max_zoom=18)
    with pytest.raises(TileError):
        validate_tile(1, 2, 0, max_zoom=18)  # x beyond 2**z
    with pytest.raises(TileError):
        validate_tile(1, 0, -1, max_zoom=18)


def _product(count, dtype, values, nodata=None, crs="EPSG:4326") -> bytes:
    """World-covering geotiff (per-band constant array of shape (h, w))."""
    height, width = (18, 36) if crs == "EPSG:4326" else (90, 180)
    transform = from_origin(-180, 85.051129, 360 / width, 170.102258 / height)
    profile = {
        "driver": "GTiff", "dtype": dtype, "count": count,
        "height": height, "width": width, "crs": crs,
        "transform": transform, "nodata": nodata, "compress": "deflate",
    }
    with rasterio.io.MemoryFile() as mem:
        with mem.open(**profile) as dst:
            for band in range(1, count + 1):
                dst.write(np.full((height, width), values[band - 1], dtype=dtype), band)
        return mem.read()


def _png(data: bytes):
    return Image.open(io.BytesIO(data)).convert("RGBA")


def test_ramp_render_produces_rgba_png_with_expected_color():
    product = _product(1, "float32", [0.5])
    png = render_tile(product, 0, 0, 0, RAMP_STYLE)
    img = _png(png)
    assert img.size == (256, 256)
    assert img.mode == "RGBA"
    # 0.5 midway between stop (0, (255,255,191)) and (1, (0,104,55))
    r, g, b, a = img.getpixel((128, 128))
    assert abs(r - 128) <= 2 and abs(g - 179) <= 2 and abs(b - 123) <= 2
    assert a == 255


def test_nodata_renders_transparent():
    # left half of the world NODATA, right half 0.5
    height, width = 18, 36
    data = np.full((height, width), 0.5, dtype=np.float32)
    data[:, : width // 2] = NODATA
    transform = from_origin(-180, 85.051129, 360 / width, 170.102258 / height)
    with rasterio.io.MemoryFile() as mem:
        with mem.open(
            driver="GTiff", dtype="float32", count=1, height=height, width=width,
            crs="EPSG:4326", transform=transform, nodata=NODATA,
        ) as dst:
            dst.write(data, 1)
        product = mem.read()

    img = _png(render_tile(product, 0, 0, 0, RAMP_STYLE))
    assert img.getpixel((64, 128))[3] == 0   # west: transparent
    assert img.getpixel((192, 128))[3] == 255  # east: opaque


def test_rgb_style_keeps_band_values():
    product = _product(3, "uint8", [10, 20, 30])
    img = _png(render_tile(product, 0, 0, 0, RGB_STYLE))
    assert img.getpixel((128, 128)) == (10, 20, 30, 255)


def test_rgb_style_masks_outside_aoi_fill():
    """Zero-filled area (outside the scene window) renders transparent so
    the overlay keeps the AOI shape instead of a black rectangle."""
    product = _product(3, "uint8", [0, 0, 0])
    img = _png(render_tile(product, 0, 0, 0, RGB_STYLE))
    assert img.getpixel((128, 128))[3] == 0


def test_lut_style_maps_classes_and_leaves_unclassified_transparent():
    product = _product(1, "uint8", [6], nodata=255)
    img = _png(render_tile(product, 0, 0, 0, LUT_STYLE))
    assert img.getpixel((128, 128)) == (69, 117, 180, 255)  # class 6

    unclassified = _product(1, "uint8", [255], nodata=255)
    img2 = _png(render_tile(unclassified, 0, 0, 0, LUT_STYLE))
    assert img2.getpixel((128, 128))[3] == 0


def test_encode_png_signature():
    rgba = np.zeros((4, 2, 2), dtype=np.uint8)
    png = encode_png(rgba)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

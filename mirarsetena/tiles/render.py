"""Web Mercator tile rendering: bounds, validation, styling, PNG encoding."""
from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.warp import reproject as warp_reproject

from mirarsetena.pipeline.indices import NODATA

TILE_SIZE = 256
MERCATOR_LIMIT_DEG = 85.051129

_TO_MERCATOR = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


class TileError(ValueError):
    """A tile request is outside the served range."""


def tile_bounds_wgs84(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of one slippy-map tile."""
    n = 2**z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0

    def latitude(row: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    return (west, latitude(y + 1), east, latitude(y))


def validate_tile(z: int, x: int, y: int, *, max_zoom: int) -> None:
    if not 0 <= z <= max_zoom:
        raise TileError(f"zoom {z} outside [0, {max_zoom}]")
    n = 2**z
    if not (0 <= x < n and 0 <= y < n):
        raise TileError(f"tile {z}/{x}/{y} out of range")


def _tile_dst_transform(z: int, x: int, y: int):
    west, south, east, north = tile_bounds_wgs84(z, x, y)
    x0, y_top = _TO_MERCATOR.transform(west, north)
    x1, y_bottom = _TO_MERCATOR.transform(east, south)
    return from_bounds(x0, y_bottom, x1, y_top, TILE_SIZE, TILE_SIZE)


def _reproject(
    product_bytes: bytes,
    z: int,
    x: int,
    y: int,
    *,
    resampling: Resampling,
    dst_nodata,
) -> np.ndarray:
    with MemoryFile(product_bytes) as memfile, memfile.open() as src:
        data = src.read()
        destination = np.zeros(
            (src.count, TILE_SIZE, TILE_SIZE), dtype=data.dtype
        )
        warp_reproject(
            source=data,
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=_tile_dst_transform(z, x, y),
            dst_crs="EPSG:3857",
            dst_nodata=dst_nodata,
            resampling=resampling,
        )
        return destination


def _ramp_rgba(values: np.ndarray, style: dict) -> np.ndarray:
    stops = style["stops"]
    xs = [stop[0] for stop in stops]
    channels = [
        np.interp(values, xs, [stop[1][band] for stop in stops])
        for band in range(3)
    ]
    rgba = np.zeros((4, TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    valid = values != NODATA
    for band, channel in enumerate(channels):
        rgba[band][valid] = np.round(channel[valid]).astype(np.uint8)
    rgba[3][valid] = 255
    return rgba


def _lut_rgba(classes: np.ndarray, style: dict) -> np.ndarray:
    table = np.zeros((256, 4), dtype=np.uint8)
    for code, color in style["lut"].items():
        table[code] = (*color, 255) if color is not None else (0, 0, 0, 0)
    return np.moveaxis(table[classes], 2, 0)


def _rgb_rgba(rgb: np.ndarray) -> np.ndarray:
    rgba = np.zeros((4, TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    rgba[:3] = rgb
    # Zero-filled pixels are outside the scene window (nodata-less RGB):
    # keep the overlay AOI-shaped instead of a black rectangle.
    rgba[3] = np.where(rgb.max(axis=0) == 0, 0, 255)
    return rgba


def encode_png(rgba_chw: np.ndarray) -> bytes:
    image = Image.fromarray(np.moveaxis(rgba_chw, 0, -1))
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_tile(
    product_bytes: bytes, z: int, x: int, y: int, style: dict
) -> bytes:
    """Render one layer product into a 256x256 RGBA PNG tile."""
    kind = style["kind"]
    if kind == "ramp":
        values = _reproject(
            product_bytes, z, x, y,
            resampling=Resampling.bilinear, dst_nodata=NODATA,
        )[0]
        return encode_png(_ramp_rgba(values, style))
    if kind == "lut":
        classes = _reproject(
            product_bytes, z, x, y,
            resampling=Resampling.nearest, dst_nodata=255,
        )[0]
        return encode_png(_lut_rgba(classes, style))
    if kind == "rgb":
        rgb = _reproject(
            product_bytes, z, x, y,
            resampling=Resampling.bilinear, dst_nodata=None,
        )
        return encode_png(_rgb_rgba(rgb))
    raise TileError(f"unknown style kind {kind!r}")

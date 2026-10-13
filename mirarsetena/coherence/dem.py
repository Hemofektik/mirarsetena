"""Local DEM for topsApp from AWS terrarium tiles (INSAR-PLAN L.1 spike).

ISCE2's default DEM source (USGS e4ftl01.cr.usgs.gov) needs Earthdata login
and is unreachable on this network; the AWS elevation-tiles-prod terrarium
tiles ARE reachable (they already power the app's 3D terrain view). This
builds a small north-up EPSG:4326 GeoTIFF DEM over the AOI bbox that
topsApp's `demfilename` accepts for topographic-phase removal over the
~1169 m relief.

Terrarium encoding: elevation = R*256 + G + B/256 - 32768.
The HTTP fetch is injectable so tests run offline.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import httpx
import numpy as np
from PIL import Image
from rasterio.transform import from_bounds

TERRARIUM_URL = (
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
)
TILE_SIZE = 256

# Sea-floor depths below this are treated as water: terrarium decodes ocean
# as bathymetry (down to -14 km across the IW2 swath), which ISCE's geoid
# corrector turns into FLT_MAX garbage (3.4e38) — topo then overflows into
# the NaNs that abort snaphu. The radar sees the SEA SURFACE over water.
SEA_FLOOR_BELOW_M = -50.0

Bbox = tuple[float, float, float, float]


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    """Terrarium PNG RGB → elevation in metres (float32)."""
    rgb = rgb.astype(np.float32)
    return (rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0).astype(
        np.float32
    )


def terrarium_url(z: int, x: int, y: int) -> str:
    return TERRARIUM_URL.format(z=z, x=x, y=y)


def _lon_to_tile(lon: float, z: int) -> int:
    return math.floor((lon + 180.0) / 360.0 * 2**z)


def _lat_to_tile(lat: float, z: int) -> int:
    lat_rad = math.radians(lat)
    return math.floor(
        (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * 2**z
    )


def aoi_tiles(bbox: Bbox, *, zoom: int) -> list[tuple[int, int, int]]:
    """Slippy tiles (z, x, y) covering the bbox at `zoom`."""
    west, south, east, north = bbox
    x0, x1 = _lon_to_tile(west, zoom), _lon_to_tile(east, zoom)
    y0, y1 = _lat_to_tile(north, zoom), _lat_to_tile(south, zoom)
    return [
        (zoom, x, y)
        for x in range(min(x0, x1), max(x0, x1) + 1)
        for y in range(min(y0, y1), max(y0, y1) + 1)
    ]


def _default_fetch(z: int, x: int, y: int) -> np.ndarray:
    response = httpx.get(terrarium_url(z, x, y), timeout=60)
    response.raise_for_status()
    image = Image.open(io.BytesIO(response.content))
    return np.asarray(image.convert("RGB"), dtype=np.uint8)


def build_dem(
    bbox: Bbox,
    out_path: str | Path,
    *,
    zoom: int = 13,
    fetch=None,
) -> Path:
    """Assemble terrarium tiles covering `bbox` into a north-up EPSG:4326
    GeoTIFF DEM at `out_path`. Returns the path.

    `fetch(z, x, y) -> uint8 RGB array` is injectable (tests stay offline).
    """
    fetch = fetch or _default_fetch
    tiles = aoi_tiles(bbox, zoom=zoom)
    if not tiles:
        raise ValueError(f"no tiles for bbox {bbox} at z{zoom}")

    xs = sorted({t[1] for t in tiles})
    ys = sorted({t[2] for t in tiles})
    cols = {x: i for i, x in enumerate(xs)}
    rows = {y: i for i, y in enumerate(ys)}
    mosaic = np.full(
        (len(ys) * TILE_SIZE, len(xs) * TILE_SIZE), np.nan, dtype=np.float32
    )
    for z, x, y in tiles:
        rgb = fetch(z, x, y)
        elev = decode_terrarium(rgb)
        r0, c0 = rows[y] * TILE_SIZE, cols[x] * TILE_SIZE
        mosaic[r0 : r0 + TILE_SIZE, c0 : c0 + TILE_SIZE] = elev

    # geographic bounds of the mosaic (tile grid, north-up)
    n = 2**zoom
    west = xs[0] / n * 360.0 - 180.0
    east = (xs[-1] + 1) / n * 360.0 - 180.0

    def lat(row: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    north = lat(ys[0])
    south = lat(ys[-1] + 1)
    transform = from_bounds(west, south, east, north, mosaic.shape[1], mosaic.shape[0])

    # keep only SRTM-like land heights (see SEA_FLOOR_BELOW_M)
    mosaic = np.where(mosaic < SEA_FLOOR_BELOW_M, 0.0, mosaic)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ENVI, not GeoTIFF: ISCE's Image class memmaps this file as raw BSQ
    # float32 using the sidecar gdal2isce_xml writes — it never decodes
    # GeoTIFF, and Image.createImage hard-exits unless the file is exactly
    # width*length*4 bytes. A deflate GeoTIFF is 15 MB where 77 MB of raw
    # floats are expected, so ISCE silently read only the first 1047 rows
    # (lat 9.709 -> 9.354) and produced NaN geometry north of that — exactly
    # the burst-1 void that blanked the AOI (spike run 2026-10-13).
    profile = {
        "driver": "ENVI",
        "dtype": "float32",
        "count": 1,
        "height": mosaic.shape[0],
        "width": mosaic.shape[1],
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -9999.0,
    }
    import rasterio

    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(np.where(np.isnan(mosaic), -9999.0, mosaic).astype(np.float32), 1)
    return out_path
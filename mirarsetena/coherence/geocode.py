"""Geocode a radar-space band to a north-up EPSG:4326 grid.

topsApp's own geocode step never completed on this host (full-swath snaphu
was pathological — INSAR-PLAN L.1), so the production chain geocodes the
AOI crop itself. The radar grid is curvilinear: a global affine fit over the
spike's lat/lon.rdr left up to 17.7 px of error, while GDAL's geolocation-
array warp (`src_geoloc_array`) measured max 0.48 px — sub-pixel, and it
needs no extra dependency (rasterio >= 1.3).

The caller passes the per-pixel WGS84 lat/lon arrays for the SAME radar
window as `values`, so source and geolocation share one pixel grid.

GDAL interprets a geolocation array as pixel-CORNER coordinates, while ISCE
hands out pixel-CENTRE coordinates. Feeding centres straight in shifts every
sampled value by exactly half a source pixel (measured: 0.500 px on an
axis-aligned grid); converting to corners first removes it (7e-7 deg).
"""
from __future__ import annotations

import math

import numpy as np
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import reproject

from mirarsetena.pipeline.indices import NODATA

Bbox = tuple[float, float, float, float]  # west, south, east, north


def _centers_to_corners(grid: np.ndarray) -> np.ndarray:
    """Pixel-centre coordinates → pixel-corner coordinates.

    Corner[r, c] = centre[r, c] − ½·d(row) − ½·d(col), with the local
    spacing taken from the grid's own gradient (the radar grid is
    curvilinear, so a single constant spacing would not do).
    """
    d_row = np.gradient(grid, axis=0)
    d_col = np.gradient(grid, axis=1)
    return grid - 0.5 * d_row - 0.5 * d_col


def geocode_radar_band(
    values: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    bbox: Bbox,
    *,
    resolution: float,
) -> bytes:
    """Radar-space `values` → north-up EPSG:4326 GeoTIFF bytes covering bbox.

    `lat`/`lon` are the per-pixel WGS84 coordinates of `values` (same shape).
    Pixels with no source coverage become NODATA; the output transform is
    north-up with the requested resolution, anchored at bbox's west/north.
    """
    values = np.asarray(values, dtype=np.float32)
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)
    if values.shape != lat.shape or values.shape != lon.shape:
        raise ValueError(
            f"values {values.shape} vs lat {lat.shape} / lon {lon.shape}"
        )
    west, south, east, north = bbox
    if not (east > west and north > south):
        raise ValueError(f"bbox not (W,S,E,N): {bbox}")
    if resolution <= 0:
        raise ValueError(f"resolution must be positive, got {resolution}")

    width = max(1, math.ceil((east - west) / resolution))
    height = max(1, math.ceil((north - south) / resolution))
    dst_transform = from_origin(west, north, resolution, resolution)

    # GDAL geolocation arrays: (2, H, W) = (lon, lat) at pixel CORNERS.
    geoloc = np.stack([_centers_to_corners(lon), _centers_to_corners(lat)])
    destination = np.full((height, width), NODATA, dtype=np.float32)
    reproject(
        source=values,
        destination=destination,
        src_geoloc_array=geoloc,
        src_crs=CRS.from_epsg(4326),
        src_nodata=NODATA,
        dst_transform=dst_transform,
        dst_crs=CRS.from_epsg(4326),
        dst_nodata=NODATA,
        resampling=Resampling.bilinear,
    )

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": height,
        "width": width,
        "crs": "EPSG:4326",
        "transform": dst_transform,
        "nodata": NODATA,
        "compress": "deflate",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(destination, 1)
        return mem.read()
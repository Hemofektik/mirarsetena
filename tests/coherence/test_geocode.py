"""Radar-space → geographic geocoding via GDAL geolocation arrays.

Seam: mirarsetena.coherence.geocode.geocode_radar_band.

ISCE's topsApp geocode step never completed on this host (full-swath snaphu
was pathological), so the production chain geocodes the AOI crop itself.
The radar grid is curvilinear — a global affine leaves up to 17 px of error
(measured on the spike's lat/lon.rdr) — so the warp must go through the
per-pixel lat/lon arrays (GDAL `src_geoloc_array`), which measured sub-pixel
(max 0.48 px) on the real spike geometry.
"""
from __future__ import annotations

import numpy as np
import rasterio
from rasterio.io import MemoryFile

from mirarsetena.coherence.geocode import geocode_radar_band
from mirarsetena.pipeline.indices import NODATA


def _radar_grid(rows=6, cols=8):
    """A curvilinear WGS84 radar grid (lat rises with row AND column)."""
    r = np.arange(rows, dtype=np.float64)[:, None]
    c = np.arange(cols, dtype=np.float64)[None, :]
    lat = 9.380 + r * 0.002 + c * 0.0003
    lon = -83.670 + c * 0.002 + r * 0.0004
    lat = np.broadcast_to(lat, (rows, cols)).copy()
    lon = np.broadcast_to(lon, (rows, cols)).copy()
    return lat, lon


def test_geocode_places_values_at_their_geographic_positions():
    lat, lon = _radar_grid()
    bbox = (lon.min(), lat.min(), lon.max(), lat.max())
    resolution = 0.0005

    # Geocode the LATITUDE field: every output pixel must then read the
    # latitude of its own cell centre — i.e. source values landed where
    # their lat/lon said, not transposed, shifted or affine-flattened.
    product = geocode_radar_band(
        lat.astype(np.float32), lat, lon, bbox, resolution=resolution
    )
    with MemoryFile(product) as mem, mem.open() as src:
        assert src.crs.to_epsg() == 4326
        data = src.read(1)
        rows_i, cols_i = np.mgrid[0 : src.height, 0 : src.width]
        _out_lon, out_lat = src.transform * (cols_i + 0.5, rows_i + 0.5)
        valid = data != src.nodata
        assert valid.any()
        # tolerance: half a source radar pixel (row spacing 0.002 deg)
        err = np.abs(data[valid] - out_lat[valid])
        assert err.max() < 0.5 * 0.002


def test_geocode_transports_values_and_marks_gaps_nodata():
    lat, lon = _radar_grid()
    # bbox extends 0.006 deg west of the radar grid -> that strip has no
    # source pixels and must read NODATA, not 0 and not the payload.
    bbox = (lon.min() - 0.006, lat.min(), lon.max(), lat.max())
    values = np.full(lat.shape, 42.0, dtype=np.float32)

    product = geocode_radar_band(values, lat, lon, bbox, resolution=0.0005)
    with MemoryFile(product) as mem, mem.open() as src:
        assert src.nodata == NODATA
        data = src.read(1)
        covered = data != NODATA
        assert covered.any()
        assert np.allclose(data[covered], 42.0)
        # the western strip beyond the radar grid is entirely nodata
        rows_i, cols_i = np.mgrid[0 : src.height, 0 : src.width]
        out_lon, _ = src.transform * (cols_i + 0.5, rows_i + 0.5)
        beyond = out_lon < lon.min() - 0.003
        assert beyond.any()
        assert (data[beyond] == NODATA).all()


def test_geocode_output_covers_the_requested_bbox():
    lat, lon = _radar_grid()
    bbox = (lon.min(), lat.min(), lon.max(), lat.max())
    product = geocode_radar_band(
        lat.astype(np.float32), lat, lon, bbox, resolution=0.001
    )
    with MemoryFile(product) as mem, mem.open() as src:
        west, south, east, north = src.bounds
        assert west == rasterio.coords.BoundingBox(*bbox).left or abs(west - bbox[0]) < 0.001
        assert abs(north - bbox[3]) < 0.001
        assert abs(east - bbox[2]) < 0.001
        assert abs(south - bbox[1]) < 0.001
        # coupled grid spans 0.016 deg lon (16 cols) x 0.0121 deg lat (13 rows)
        assert src.width == 16 and src.height == 13
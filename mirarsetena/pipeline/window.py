"""Level-1 cache: scene AOI windows (mosaicked per acquisition date).

Windows are clipped from source COGs in their native CRS and re-encoded as
in-memory GeoTIFF bytes; the mosaicked daily product is stored under a
project-namespaced key so future S3Store swaps are transparent.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge as rio_merge
from rasterio.transform import from_bounds, from_gcps
from rasterio.warp import reproject as warp_reproject
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from rasterio.windows import transform as window_transform

from mirarsetena.projects.registry import cache_key
from mirarsetena.storage import Storage

Bbox = tuple[float, float, float, float]  # west, south, east, north (WGS84)
Opener = Callable[[str, Bbox], bytes]


def window_key(slug: str, collection: str, date: str, band: str | None = None) -> str:
    """Level-1 cache key for one project + collection + acquisition date."""
    suffix = f"_{band}" if band else ""
    return cache_key(slug, "scenes", f"{collection}_{date}{suffix}.tif")


def scene_meta_key(slug: str, collection: str, date: str) -> str:
    """Level-1 metadata key (cloud score etc.) for one acquisition date."""
    return cache_key(slug, "scenes", f"{collection}_{date}.json")


def _default_opener(uri: str, bbox: Bbox) -> bytes:
    with rasterio.open(uri) as src:
        crs, geotransform = src.crs, src.transform
        if crs is None:
            # Sentinel-1 measurement COGs georeference via GCPs only.
            gcps, gcrs = src.gcps
            if gcrs is None or not gcps:
                raise ValueError(f"source has no georeferencing: {uri}")
            crs = gcrs
            geotransform = from_gcps(gcps)
        native_bbox = transform_bounds("EPSG:4326", crs, *bbox, densify_pts=21)
        # Orientation-tolerant window: GRD range axis can run east->west
        # (descending passes), which makes from_bounds reject negative widths.
        inverse = ~geotransform
        col_west, row_north = inverse * (native_bbox[0], native_bbox[3])
        col_east, row_south = inverse * (native_bbox[2], native_bbox[1])
        window = Window(
            col_off=min(col_west, col_east),
            row_off=min(row_north, row_south),
            width=abs(col_east - col_west),
            height=abs(row_south - row_north),
        )
        fill = src.nodata if src.nodata is not None else 0
        data = src.read(window=window, boundless=True, fill_value=fill)
        profile = src.profile.copy()
        profile.update(
            crs=crs,
            height=data.shape[1],
            width=data.shape[2],
            transform=window_transform(window, geotransform),
            compress="deflate",
        )
        with MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(data)
            return mem.read()


def read_window(uri: str, bbox: Bbox, *, opener: Opener | None = None) -> bytes:
    """Read the bbox window of one source raster as GeoTIFF bytes."""
    return (opener or _default_opener)(uri, bbox)


def _reproject_window_to(window_bytes: bytes, target_crs) -> bytes:
    """Re-encode one window into the target CRS at equivalent native
    resolution (nearest: preserves class and reflectance values)."""
    with MemoryFile(window_bytes) as memfile, memfile.open() as src:
        if src.crs == target_crs:
            return window_bytes
        px_left, px_bottom = src.bounds.left, src.bounds.bottom
        px_west, px_south, px_east, px_north = transform_bounds(
            src.crs,
            target_crs,
            px_left,
            px_bottom,
            px_left + abs(src.transform.a),
            px_bottom + abs(src.transform.e),
            densify_pts=5,
        )
        res_x = max(abs(px_east - px_west), 1e-9)
        res_y = max(abs(px_north - px_south), 1e-9)
        west, south, east, north = transform_bounds(
            src.crs, target_crs, *src.bounds, densify_pts=21
        )
        width = max(1, math.ceil((east - west) / res_x))
        height = max(1, math.ceil((north - south) / res_y))
        transform = from_bounds(
            west, north - height * res_y, west + width * res_x, north, width, height
        )
        data = src.read()
        fill = src.nodata if src.nodata is not None else 0
        destination = np.full((src.count, height, width), fill, dtype=data.dtype)
        warp_reproject(
            source=data,
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=transform,
            dst_crs=target_crs,
            dst_nodata=src.nodata,
            resampling=Resampling.nearest,
        )
        profile = src.profile.copy()
        profile.update(
            crs=target_crs,
            transform=transform,
            width=width,
            height=height,
            compress="deflate",
        )
        with MemoryFile() as out:
            with out.open(**profile) as dst:
                dst.write(destination)
            return out.read()


def mosaic(*window_bytes: bytes) -> bytes:
    """Merge windows into one product; mixed CRS inputs are reprojected
    onto the first window's CRS first (the AOI straddles MGRS zones)."""
    if len(window_bytes) == 1:
        return window_bytes[0]

    crs_list = []
    for raw in window_bytes:
        with MemoryFile(raw) as memfile, memfile.open() as src:
            crs_list.append(src.crs)
    target_crs = crs_list[0]
    aligned = [
        raw if crs == target_crs else _reproject_window_to(raw, target_crs)
        for raw, crs in zip(window_bytes, crs_list)
    ]

    memfiles = [MemoryFile(chunk) for chunk in aligned]
    sources = []
    try:
        sources = [memfile.open() for memfile in memfiles]
        data, transform = rio_merge(sources)
        profile = sources[0].profile.copy()
        profile.update(
            height=data.shape[1],
            width=data.shape[2],
            count=data.shape[0],
            transform=transform,
            compress="deflate",
        )
        with MemoryFile() as out:
            with out.open(**profile) as dst:
                dst.write(data)
            return out.read()
    finally:
        for source in sources:
            source.close()
        for memfile in memfiles:
            memfile.close()


def cached_window(
    storage: Storage,
    key: str,
    uris: Sequence[str],
    bbox: Bbox,
    *,
    opener: Opener | None = None,
) -> bytes:
    """Return the level-1 window product, fetching + mosaicking on first use."""
    cached = storage.get(key)
    if cached is not None:
        return cached
    windows = [read_window(uri, bbox, opener=opener) for uri in uris]
    product = windows[0] if len(windows) == 1 else mosaic(*windows)
    storage.put(key, product)
    return product

"""Level-1 cache: scene AOI windows (mosaicked per acquisition date).

Windows are clipped from source COGs in their native CRS and re-encoded as
in-memory GeoTIFF bytes; the mosaicked daily product is stored under a
project-namespaced key so future S3Store swaps are transparent.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

import rasterio
from rasterio.io import MemoryFile
from rasterio.merge import merge as rio_merge
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
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
        native_bbox = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
        window = from_bounds(*native_bbox, transform=src.transform)
        fill = src.nodata if src.nodata is not None else 0
        data = src.read(window=window, boundless=True, fill_value=fill)
        profile = src.profile.copy()
        profile.update(
            height=data.shape[1],
            width=data.shape[2],
            transform=window_transform(window, src.transform),
            compress="deflate",
        )
        with MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(data)
            return mem.read()


def read_window(uri: str, bbox: Bbox, *, opener: Opener | None = None) -> bytes:
    """Read the bbox window of one source raster as GeoTIFF bytes."""
    return (opener or _default_opener)(uri, bbox)


def mosaic(*window_bytes: bytes) -> bytes:
    """Merge overlapping/clipped windows into one GeoTIFF product."""
    if len(window_bytes) == 1:
        return window_bytes[0]
    memfiles = [MemoryFile(chunk) for chunk in window_bytes]
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

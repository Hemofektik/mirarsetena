"""INSAR-PLAN L.4 — the `insar` layer renders through the tile service.

Seam: mirarsetena.tiles.service.TileService.get_tile. The InSAR product is
produced by the background job (no lazy path — sentinel-1-slc returns early
from the processor); this test stores one at the servable key and pins that a
tile request returns a real RGBA PNG drawn with the diverging LOS ramp.
"""
from pathlib import Path

import numpy as np
from rasterio.transform import from_origin

from mirarsetena.pipeline.indices import NODATA, layer_key
from mirarsetena.projects.registry import ProjectRegistry
from mirarsetena.storage import LocalStore
from mirarsetena.tiles.service import TileService

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "projects"


def _insar_config(config):
    return config.model_copy(update={"layers": list(config.layers) + ["insar"]})


def _encode(values, nodata=NODATA):
    from rasterio.io import MemoryFile

    height, width = values.shape
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": height, "width": width, "crs": "EPSG:4326",
        "transform": from_origin(-83.675, 9.397, 0.00375, 0.00475),
        "nodata": nodata, "compress": "deflate",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(values.astype(np.float32), 1)
        return mem.read()


def _fake_search(collection, bbox, start, end):
    import json

    from mirarsetena.pipeline.catalog import parse_scenes

    if collection != "sentinel-1-grd":
        return []
    payload = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "stac_s1_grd.json").read_text()
    )
    return parse_scenes(collection, payload)


def test_insar_tile_renders_diverging_los_overlay(tmp_path):
    config = _insar_config(ProjectRegistry(CONFIG_DIR).get("cdp-rio-general"))
    storage = LocalStore(tmp_path / "cache")

    # The background job stores a LOS-cm product at the servable key for a
    # pair end-date; here we seed one directly (red: key absent → no tile).
    # Constant +5 cm so every tile pixel lands on the red end of the ramp.
    values = np.full((6, 6), 5.0, dtype=np.float32)
    key = layer_key("cdp-rio-general", "sentinel-1-slc", "2026-10-03", "insar")
    storage.put(key, _encode(values))

    service = TileService(storage, config, search_fn=_fake_search)
    png = service.get_tile("insar", "2026-10-03", 16, 17536, 31051)
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    px = img.load()
    r, _g, b, a = px[128, 128]
    assert a == 255
    assert r > b  # red end of the diverging ramp (+5 cm)


def test_insar_masked_tile_pixel_is_transparent(tmp_path):
    config = _insar_config(ProjectRegistry(CONFIG_DIR).get("cdp-rio-general"))
    storage = LocalStore(tmp_path / "cache")

    # A fully-masked (low-coherence) product renders fully transparent:
    # the overlay shows honest gaps, never fabricated motion.
    values = np.full((6, 6), NODATA, dtype=np.float32)
    key = layer_key("cdp-rio-general", "sentinel-1-slc", "2026-08-16", "insar")
    storage.put(key, _encode(values))

    service = TileService(storage, config, search_fn=_fake_search)
    png = service.get_tile("insar", "2026-08-16", 16, 17536, 31051)
    assert png is not None

    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    assert img.getpixel((128, 128))[3] == 0  # transparent where masked


def test_insar_tile_uses_adaptive_range_for_small_displacements(tmp_path):
    # The fixed +-5 cm ramp rendered the real (referenced, +-0.56 cm)
    # product all-white. With the adaptive range the same small values must
    # land on the coloured ends of the ramp, not the white centre.
    config = _insar_config(ProjectRegistry(CONFIG_DIR).get("cdp-rio-general"))
    storage = LocalStore(tmp_path / "cache")

    values = np.full((6, 6), 0.5, dtype=np.float32)  # +0.5 cm everywhere
    key = layer_key("cdp-rio-general", "sentinel-1-slc", "2026-10-03", "insar")
    storage.put(key, _encode(values))

    service = TileService(storage, config, search_fn=_fake_search)
    png = service.get_tile("insar", "2026-10-03", 16, 17536, 31051)
    assert png is not None

    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    r, g, b, a = img.load()[128, 128]
    assert a == 255
    # +0.5 cm at the adaptive limit -> the RED end (178, 24, 43), not the
    # white centre (247, 247, 247) the fixed +-5 cm ramp would give
    assert r > 150 and b < 80, f"expected red end, got rgb({r},{g},{b})"


def test_insar_tile_cache_invalidates_when_the_range_changes(tmp_path):
    # The adaptive range is part of the render, so a cached PNG must not
    # survive a range change: the first product (+0.5 cm, limit 0.5) renders
    # at the red end; after replacing it with a -5 cm product (limit 5.0)
    # the SAME tile request must re-render at the blue end, not serve the
    # stale red tile.
    config = _insar_config(ProjectRegistry(CONFIG_DIR).get("cdp-rio-general"))
    storage = LocalStore(tmp_path / "cache")
    key = layer_key("cdp-rio-general", "sentinel-1-slc", "2026-10-03", "insar")

    import io

    from PIL import Image

    def pixel(png):
        return Image.open(io.BytesIO(png)).convert("RGBA").load()[128, 128]

    # small product: +0.5 cm sits at the adaptive limit -> red end
    storage.put(key, _encode(np.full((6, 6), 0.5, dtype=np.float32)))
    service = TileService(storage, config, search_fn=_fake_search)
    first = pixel(service.get_tile("insar", "2026-10-03", 16, 17536, 31051))
    assert first[0] > 150, f"expected red end, got {first}"

    # wide product at the negative end: limit 5.0 -> blue end (33,102,172)
    storage.put(key, _encode(np.full((6, 6), -5.0, dtype=np.float32)))
    service = TileService(storage, config, search_fn=_fake_search)
    second = pixel(service.get_tile("insar", "2026-10-03", 16, 17536, 31051))
    assert second[2] > 120 and second[0] < 80, (
        f"stale cached tile served: {second}"
    )

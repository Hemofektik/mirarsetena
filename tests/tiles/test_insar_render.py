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

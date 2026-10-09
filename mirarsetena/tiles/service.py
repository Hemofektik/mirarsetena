"""Tile service: product resolution, change derivation, caching, rendering.

The service is the single handler behind both the WMTS GetTile endpoint and
the XYZ route (SCOPE R2-Q4), so parity is structural, not incidental.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future

from mirarsetena.pipeline.catalog import group_by_date, search
from mirarsetena.pipeline.change import (
    PREPROCESS_VERSION,
    default_baseline_date,
    render_change,
)
from mirarsetena.pipeline.dates import list_dates, mission_for_layer
from mirarsetena.pipeline.grd import process_s1_daily
from mirarsetena.pipeline.indices import layer_key, process_s2_daily
from mirarsetena.projects.registry import ProjectConfig, cache_key
from mirarsetena.storage import Storage, enforce_budget
from mirarsetena.tiles.cache import TileCache
from mirarsetena.tiles.render import TileError, render_tile, validate_tile
from mirarsetena.tiles.styles import RADAR_LAYERS, style_for

S2_COLLECTION = "sentinel-2-l2a"
S1_COLLECTION = "sentinel-1-grd"
VALID_MODES = {"change", "raw"}


def make_processor(storage: Storage, config: ProjectConfig, search_fn) -> Callable:
    """Lazy on-demand processing of one (layer, date) — used on cache miss.

    Single-flight: a layer switch fans ~20 tile requests onto the same cold
    product; they share one catalog search + scene download instead of
    racing (otherwise the threadpool saturates and /api/dates stalls).

    Coherence (sentinel-1-slc) has no lazy path: those products are
    produced exclusively by the background coherence jobs (Phase H).
    """
    in_flight: dict[tuple[str, str], Future] = {}
    guard = threading.Lock()
    # Source-level LRU: scene windows, derived layer products and coherence
    # pairs share one budget (scene_budget_bytes in the project config).
    source_prefixes = tuple(
        cache_key(config.slug, name) + "/"
        for name in ("scenes", "layers", "coherence")
    )

    def produce(mission: str, date: str) -> None:
        groups = group_by_date(search_fn(mission, config.bbox, date, date))
        if not groups:
            return
        daily = groups[0]
        if mission == S2_COLLECTION:
            process_s2_daily(storage, config.slug, daily, config.bbox)
        else:
            process_s1_daily(storage, config.slug, daily, config.bbox)

    def process(layer: str, date: str) -> None:
        mission = mission_for_layer(layer)
        if mission == "sentinel-1-slc":
            return
        key = (mission, date)
        with guard:
            future = in_flight.get(key)
            if future is None:
                future = Future()
                in_flight[key] = future
                owner = True
            else:
                owner = False
        if not owner:
            future.result()  # wait for the producer; re-raises its error
            return
        try:
            produce(mission, date)
        except BaseException as exc:  # followers must not hang on failure
            future.set_exception(exc)
            raise
        else:
            future.set_result(None)
        finally:
            with guard:
                in_flight.pop(key, None)
            # Even a failed production may have written partial windows.
            enforce_budget(
                storage, source_prefixes, config.cache.scene_budget_bytes
            )

    return process


class TileService:
    def __init__(
        self,
        storage: Storage,
        config: ProjectConfig,
        *,
        search_fn=search,
        processor: Callable | None = None,
    ):
        self._storage = storage
        self._config = config
        self._search_fn = search_fn
        self._processor = processor
        self._cache = TileCache(
            storage,
            config.slug,
            budget_bytes=config.cache.tile_budget_bytes,
        )

    def _date_entries(self, layer: str) -> list[dict]:
        result = list_dates(
            self._storage,
            self._config.slug,
            self._config,
            layer,
            search_fn=self._search_fn,
        )
        return result["dates"]

    def dates(self, layer: str) -> list[str]:
        return [entry["date"] for entry in self._date_entries(layer)]

    def capabilities_layer_names(self) -> list[str]:
        names = []
        for layer in self._config.layers:
            names.extend(f"{layer}_{date}" for date in self.dates(layer))
        return names

    def _product(self, layer: str, date: str) -> bytes | None:
        mission = mission_for_layer(layer)
        key = layer_key(self._config.slug, mission, date, layer)
        product = self._storage.get(key)
        if product is None and self._processor is not None:
            self._processor(layer, date)
            product = self._storage.get(key)
        return product

    def _derived(
        self, layer: str, date: str, baseline: str | None, mode: str,
        scene: bytes, baseline_product: bytes | None,
    ) -> bytes:
        suffix = f"{layer}_{date}_raw.tif" if mode == "raw" else \
            f"{layer}_{date}_vs_{baseline}_pp{PREPROCESS_VERSION}.tif"
        key = cache_key(self._config.slug, "layers", suffix)
        derived = self._storage.get(key)
        if derived is None:
            derived = render_change(scene, baseline_product, mode=mode, layer=layer)
            self._storage.put(key, derived)
        return derived

    def get_tile(
        self,
        layer: str,
        date: str,
        z: int,
        x: int,
        y: int,
        *,
        baseline: str | None = None,
        mode: str = "change",
    ) -> bytes | None:
        """PNG bytes for one tile, or None when the request maps to nothing.

        Raises TileError for out-of-range requests (caller maps to 400).
        """
        validate_tile(z, x, y, max_zoom=self._config.cache.max_zoom)
        if mode not in VALID_MODES:
            raise TileError(f"unknown mode {mode!r}")
        if layer not in self._config.layers:
            return None
        entries = self._date_entries(layer)
        dates = [entry["date"] for entry in entries]
        if date not in dates:
            return None

        scene = self._product(layer, date)
        if scene is None:
            return None

        if layer in RADAR_LAYERS:
            if mode == "raw":
                # Raw renders ABSOLUTE values via the layer's value ramp
                # (comparable across dates); no derived product needed.
                product = scene
                baseline = None
                style = style_for(layer, mode="raw")
            else:
                orbit_index = {
                    entry["date"]: entry.get("orbits") for entry in entries
                }
                baseline = baseline or default_baseline_date(
                    [entry["date"] for entry in entries],
                    self._config.timeline.works_start,
                    end_date=date,
                    orbit_index=orbit_index,
                )
                if baseline not in dates:
                    return None
                baseline_product = self._product(layer, baseline)
                if baseline_product is None:
                    return None
                product = self._derived(
                    layer, date, baseline, mode, scene, baseline_product
                )
                style = style_for(layer, mode="change")
        else:
            product = scene
            style = style_for(layer)

        # change tiles carry the preprocessing version so old renders are
        # never served after the classification changes
        pp_tag = f"-pp{PREPROCESS_VERSION}" if mode == "change" else ""
        cache_parts = (
            layer, date, str(z), str(x), str(y),
            f"{mode}-{baseline or ''}{pp_tag}.png",
        )
        return self._cache.get_or_render(
            cache_parts,
            lambda: render_tile(product, z, x, y, style),
        )

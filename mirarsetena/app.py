"""FastAPI application factory.

Routes live under the per-project prefix (/p/{slug}/...) produced by the
project registry (SCOPE §3 R5-Q1); storage is the two-level cache backend.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response

from mirarsetena.jobs.poller import poller_loop
from mirarsetena.jobs.runner import JobQueue
from mirarsetena.pipeline import catalog as catalog_module
from mirarsetena.pipeline.dates import UnknownLayer, list_dates
from mirarsetena.projects.registry import ProjectNotFound, ProjectRegistry
from mirarsetena.status import status_payload
from mirarsetena.storage import LocalStore
from mirarsetena.tiles.render import TileError
from mirarsetena.tiles.service import TileService, make_processor
from mirarsetena.tiles.wmts import build_capabilities


def create_app(
    config_dir: str | Path | None = None,
    storage_root: str | Path | None = None,
    search_fn=None,
    processor=None,
    icesat_fetch=None,
) -> FastAPI:
    config_dir = Path(
        config_dir or os.environ.get("MIRAR_CONFIG_DIR", "config/projects")
    )
    storage_root = Path(
        storage_root or os.environ.get("MIRAR_STORAGE_ROOT", "data/cache")
    )

    registry = ProjectRegistry(config_dir)
    storage = LocalStore(storage_root)
    queue = JobQueue(storage_root / "jobs.db")

    @asynccontextmanager
    async def lifespan(app_):
        import asyncio

        import anyio.to_thread

        # Cold tile requests occupy a worker thread for their whole
        # production; abandoned waves (user scrubs away) keep holding
        # theirs until done. A deep pool keeps cache hits instant instead
        # of queueing behind them (MIRAR_THREADPOOL_TOKENS, default 128).
        capacity = int(os.environ.get("MIRAR_THREADPOOL_TOKENS", "128"))
        anyio.to_thread.current_default_thread_limiter().total_tokens = capacity

        interval = int(os.environ.get("MIRAR_POLL_SECONDS", "3600"))
        task = asyncio.create_task(poller_loop(app_, interval))
        app_.state.poller = task
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Mirar Setena", version="0.1.0", lifespan=lifespan)
    app.state.registry = registry
    app.state.storage = storage
    app.state.queue = queue
    app.state.search_fn = search_fn or catalog_module.search
    app.state.icesat_fetch = icesat_fetch
    app.state.processor_override = processor
    # ONE processor closure per project: make_processor owns the
    # single-flight (in_flight + guard). Building it per request gave every
    # concurrent tile its own flight — N duplicate productions and N
    # concurrent budget walks (measured: 22 s + 8.4 s per call under a wave).
    app.state.processors: dict = {}

    def tile_service(slug: str) -> TileService:
        config = registry.get(slug)  # ProjectNotFound handled by callers
        processor_ = app.state.processor_override
        if processor_ is None:
            processor_ = app.state.processors.setdefault(
                slug, make_processor(storage, config, app.state.search_fn)
            )
        return TileService(
            storage, config, search_fn=app.state.search_fn, processor=processor_
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        return {"status": "ok", "threadpool_tokens": int(limiter.total_tokens)}

    @app.get("/projects")
    def projects() -> dict:
        """Project list for the bare-domain boot (no /p/{slug}/ in the path).

        The SPA served at `/` asks which project to open; `default` is the
        first slug in config order (deterministic, config-file sorted).
        """
        known = registry.all()
        slugs = sorted(known)
        return {
            "default": slugs[0] if slugs else None,
            "projects": [
                {"slug": slug, "name": known[slug].name} for slug in slugs
            ],
        }

    @app.get("/p/{slug}/config")
    def project_config(slug: str) -> dict:
        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return config.model_dump(mode="json")

    @app.get("/p/{slug}/api/dates")
    def project_dates(
        slug: str,
        layer: str = Query(min_length=1),
        max_cloud: float | None = Query(default=None, ge=0, le=100),
    ) -> dict:
        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            result = list_dates(
                storage,
                slug,
                config,
                layer,
                max_cloud=max_cloud,
                search_fn=app.state.search_fn,
            )
        except UnknownLayer as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # SCOPE R2-Q1: the first plain-data view starts the eager coherence job.
        from mirarsetena.coherence.trigger import (
            trigger_insar_on_first_view,
            trigger_on_first_view,
        )

        trigger_on_first_view(storage, config, queue, app.state.search_fn)
        # INSAR-PLAN L.5: the first view also starts the InSAR eager job.
        trigger_insar_on_first_view(storage, config, queue, app.state.search_fn)
        return result

    @app.get("/p/{slug}/api/plan")
    def project_plan(slug: str) -> dict:
        """Plan vs. Realidad (INSAR-PLAN L.9): timeline anchors + plan
        milestones + the measured per-POI InSAR series, each from its own
        source — the chart must not invent any of them."""
        import json as _json

        from mirarsetena.projects.registry import cache_key

        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        milestones: list[dict] = []
        if config.plan_path:
            path = config_dir.resolve().parent.parent / config.plan_path
            if not path.is_file():
                raise HTTPException(
                    status_code=404, detail=f"plan file not found: {path.name}"
                )
            payload = _json.loads(path.read_text(encoding="utf-8"))
            milestones = list(payload.get("milestones", []))

        prefix = cache_key(slug, "layers") + "/"
        series = []
        for key in storage.list(prefix):
            if key.endswith("_insar_series.json"):
                raw = storage.get(key)
                if raw:
                    series.append(_json.loads(raw))
        series.sort(key=lambda entry: entry["date"])

        # SBAS aggregation summary: per-chain (satellite+track — never
        # phase-tied across sensors) mean/sem per POI. Population-free
        # sample std so n=2 has a real spread; n=1 reports sem=None.
        from collections import defaultdict
        from statistics import stdev

        chains: dict[str, list] = defaultdict(list)
        for entry in series:
            if entry.get("chain"):
                chains[entry["chain"]].append(entry)
        stack = []
        for chain, entries in sorted(chains.items()):
            entries.sort(key=lambda e: e["date"])
            by_poi: dict[str, list] = defaultdict(list)
            for entry in entries:
                for poi in entry.get("pois", []):
                    if poi.get("los_cm") is not None:
                        by_poi[poi["id"]].append(poi["los_cm"])
            pois_out = []
            for poi_id, values in by_poi.items():
                mean = sum(values) / len(values)
                sem = (
                    stdev(values) / (len(values) ** 0.5)
                    if len(values) >= 2
                    else None
                )
                pois_out.append({"id": poi_id, "mean": mean, "sem": sem})
            stack.append(
                {
                    "chain": chain,
                    "n": len(entries),
                    "start": entries[0]["date"],
                    "end": entries[-1]["date"],
                    "pois": pois_out,
                }
            )

        # SBAS cumulative series (sbas_run, L.6a): the chart draws the
        # inverted trajectory as a line per chain
        sbas = []
        for key in storage.list(cache_key(slug, "sbas") + "/"):
            if key.endswith(".json") and "series" in key.rsplit("/", 1)[-1]:
                raw = storage.get(key)
                if raw:
                    sbas.append(_json.loads(raw))
        sbas.sort(key=lambda entry: entry.get("chain", ""))

        return {
            "timeline": config.timeline.model_dump(mode="json"),
            "milestones": milestones,
            "series": series,
            "stack": stack,
            "sbas": sbas,
        }

    @app.get("/p/{slug}/status")
    def project_status(slug: str) -> dict:
        from datetime import datetime, timezone

        from mirarsetena.jobs.poller import RUNNABLE_TYPES

        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return status_payload(
            storage, config, queue, now=datetime.now(timezone.utc),
            search_fn=app.state.search_fn, runnable_types=RUNNABLE_TYPES,
        )

    @app.get("/p/{slug}/status/page")
    def project_status_page(slug: str) -> Response:
        import json as _json
        from datetime import datetime, timezone

        from mirarsetena.jobs.poller import RUNNABLE_TYPES

        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = status_payload(
            storage, config, queue, now=datetime.now(timezone.utc),
            search_fn=app.state.search_fn, runnable_types=RUNNABLE_TYPES,
        )
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>Mirar Setena — {config.name}</title></head><body>"
            f"<h1>Mirar Setena — {config.name}</h1>"
            f"<pre>{_json.dumps(payload, indent=2, ensure_ascii=False)}</pre>"
            "</body></html>"
        )
        return Response(content=html, media_type="text/html")

    @app.get("/p/{slug}/aoi.geojson")
    def project_aoi(slug: str) -> Response:
        return _geo_response(slug, "aoi_path")

    @app.get("/p/{slug}/pois.geojson")
    def project_pois(slug: str) -> Response:
        return _geo_response(slug, "pois_path")

    @app.get("/p/{slug}/rio.geojson")
    def project_rio(slug: str) -> Response:
        return _geo_response(slug, "rio_path")

    @app.get("/p/{slug}/icesat.geojson")
    def project_icesat(slug: str) -> Response:
        """ICESat-2 ATL06 ground tracks over the AOI (open CMR metadata).

        Cached in storage after the first fetch; elevation VALUES need
        Earthdata login and are not fetched (tracks show where/when the
        laser measured). The CMR fetch is injectable for tests.
        """
        import json as _json

        from mirarsetena.projects.registry import cache_key

        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        cache_key_icesat = cache_key(slug, "catalog", "icesat-tracks.json")
        raw = storage.get(cache_key_icesat)
        if raw is None:
            from mirarsetena.icesat import fetch_tracks

            fetch = app.state.icesat_fetch or fetch_tracks
            collection = fetch(tuple(config.bbox))
            storage.put(
                cache_key_icesat,
                _json.dumps(collection, ensure_ascii=False).encode("utf-8"),
            )
            raw = storage.get(cache_key_icesat)
        return Response(
            content=raw, media_type="application/geo+json",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/p/{slug}/icesat-elevations.geojson")
    def project_icesat_elevations(slug: str) -> Response:
        """ICESat-2 elevation points over the AOI (ATL06, Earthdata-gated).

        Served from whatever tools/icesat_elevations stored; an empty
        store yields an empty collection — the layer lights up the
        moment the tool runs with credentials. No network here.
        """
        import json as _json

        from mirarsetena.projects.registry import cache_key

        try:
            registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raw = storage.get(cache_key(slug, "catalog", "icesat-elevations.json"))
        collection = (
            _json.loads(raw)
            if raw
            else {"type": "FeatureCollection", "features": []}
        )
        return Response(
            content=_json.dumps(collection, ensure_ascii=False).encode("utf-8"),
            media_type="application/geo+json",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/p/{slug}/official.geojson")
    def project_official(slug: str) -> Response:
        return _geo_response(slug, "official_path")

    def _geo_response(slug: str, attr: str) -> Response:
        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        repo_root = config_dir.resolve().parent.parent
        rel = getattr(config, attr)
        if not rel:
            raise HTTPException(status_code=404, detail=f"{attr} not configured")
        path = repo_root / rel
        if not path.is_file():
            raise HTTPException(
                status_code=404, detail=f"{attr} not found: {path.name}"
            )
        return Response(
            content=path.read_text(encoding="utf-8"),
            media_type="application/geo+json",
            headers={"Cache-Control": "no-store"},
        )

    # Built SPA (frontend/dist) — same origin as the API when present.
    dist_dir = Path(os.environ.get("MIRAR_FRONTEND_DIST", "frontend/dist"))
    index_file = dist_dir / "index.html"
    if index_file.is_file():
        if (dist_dir / "assets").is_dir():
            from fastapi.staticfiles import StaticFiles

            app.mount(
                "/assets", StaticFiles(directory=dist_dir / "assets"), name="assets"
            )

        def _index() -> Response:
            return Response(
                content=index_file.read_text(encoding="utf-8"),
                media_type="text/html",
            )

        @app.get("/")
        def root_index() -> Response:
            return _index()

        @app.get("/p/{slug}/")
        def project_index(slug: str) -> Response:
            try:
                registry.get(slug)
            except ProjectNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return _index()

    @app.get("/p/{slug}/wmts")
    def wmts(
        request: Request,
        slug: str,
        SERVICE: str | None = None,
        REQUEST: str | None = None,
        LAYER: str | None = None,
        TILEMATRIXSET: str | None = None,
        TILEMATRIX: str | None = None,
        TILEROW: str | None = None,
        TILECOL: str | None = None,
        STYLE: str | None = None,
    ) -> Response:
        try:
            service = tile_service(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if REQUEST == "GetCapabilities":
            endpoint = str(request.base_url).rstrip("/") + f"/p/{slug}/wmts"
            config = registry.get(slug)
            xml = build_capabilities(
                service.capabilities_layer_names(),
                config.cache.max_zoom,
                endpoint,
                bbox=config.bbox,
            )
            return Response(content=xml, media_type="application/xml")

        if REQUEST == "GetTile":
            missing = [
                name
                for name, value in (
                    ("LAYER", LAYER), ("TILEMATRIX", TILEMATRIX),
                    ("TILEROW", TILEROW), ("TILECOL", TILECOL),
                )
                if value is None
            ]
            if missing:
                raise HTTPException(
                    status_code=400, detail=f"missing parameters: {missing}"
                )
            if "_" not in LAYER:
                raise HTTPException(status_code=404, detail=f"unknown layer {LAYER!r}")
            layer, date = LAYER.split("_", 1)
            try:
                z = int(TILEMATRIX)
                row = int(TILEROW)
                col = int(TILECOL)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="TILEMATRIX/TILEROW/TILECOL must be integers"
                ) from exc
            try:
                png = service.get_tile(layer, date, z, col, row)
            except TileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if png is None:
                raise HTTPException(status_code=404, detail="no tile for {LAYER!r}")
            # same immutability as the XYZ route: LAYER/date/matrix/row/col
            # fully identify the pixels
            return Response(
                content=png,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )

        raise HTTPException(
            status_code=400, detail=f"unsupported REQUEST {REQUEST!r}"
        )

    @app.get("/p/{slug}/tiles/{layer}/{date}/{z}/{x}/{y}.png")
    def xyz_tile(
        slug: str,
        layer: str,
        date: str,
        z: int,
        x: int,
        y: int,
        baseline: str | None = Query(default=None),
        mode: str = Query(default="change"),
    ) -> Response:
        try:
            service = tile_service(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            png = service.get_tile(
                layer, date, z, x, y, baseline=baseline, mode=mode
            )
        except TileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if png is None:
            raise HTTPException(
                status_code=404, detail=f"no tile for {layer}/{date}/{z}/{x}/{y}"
            )
        # Immutable by URL: layer/date/z/x/y fully identify the pixels, so
        # the browser never needs to revalidate — scrubbing back to a date
        # is served from the local cache (user 2026-10-20).
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    return app


app = create_app()

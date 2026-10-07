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
    app.state.processor_override = processor

    def tile_service(slug: str) -> TileService:
        config = registry.get(slug)  # ProjectNotFound handled by callers
        processor = app.state.processor_override
        if processor is None:
            processor = make_processor(storage, config, app.state.search_fn)
        return TileService(
            storage, config, search_fn=app.state.search_fn, processor=processor
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
        from mirarsetena.coherence.trigger import trigger_on_first_view

        trigger_on_first_view(storage, config, queue, app.state.search_fn)
        return result

    @app.get("/p/{slug}/status")
    def project_status(slug: str) -> dict:
        from datetime import datetime, timezone

        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return status_payload(
            storage, config, queue, now=datetime.now(timezone.utc)
        )

    @app.get("/p/{slug}/status/page")
    def project_status_page(slug: str) -> Response:
        import json as _json
        from datetime import datetime, timezone

        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = status_payload(
            storage, config, queue, now=datetime.now(timezone.utc)
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

    def _geo_response(slug: str, attr: str) -> Response:
        try:
            config = registry.get(slug)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        repo_root = config_dir.resolve().parent.parent
        path = repo_root / getattr(config, attr)
        if not path.is_file():
            raise HTTPException(
                status_code=404, detail=f"{attr} not found: {path.name}"
            )
        return Response(
            content=path.read_text(encoding="utf-8"),
            media_type="application/geo+json",
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
            return Response(content=png, media_type="image/png")

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
        return Response(content=png, media_type="image/png")

    return app


app = create_app()

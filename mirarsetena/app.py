"""FastAPI application factory.

Routes live under the per-project prefix (/p/{slug}/...) produced by the
project registry (SCOPE §3 R5-Q1); storage is the two-level cache backend.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from mirarsetena.pipeline import catalog as catalog_module
from mirarsetena.pipeline.dates import UnknownLayer, list_dates
from mirarsetena.projects.registry import ProjectNotFound, ProjectRegistry
from mirarsetena.storage import LocalStore


def create_app(
    config_dir: str | Path | None = None,
    storage_root: str | Path | None = None,
    search_fn=None,
) -> FastAPI:
    config_dir = Path(
        config_dir or os.environ.get("MIRAR_CONFIG_DIR", "config/projects")
    )
    storage_root = Path(
        storage_root or os.environ.get("MIRAR_STORAGE_ROOT", "data/cache")
    )

    registry = ProjectRegistry(config_dir)
    storage = LocalStore(storage_root)

    app = FastAPI(title="Mirar Setena", version="0.1.0")
    app.state.registry = registry
    app.state.storage = storage
    app.state.search_fn = search_fn or catalog_module.search

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
            return list_dates(
                storage,
                slug,
                config,
                layer,
                max_cloud=max_cloud,
                search_fn=app.state.search_fn,
            )
        except UnknownLayer as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


app = create_app()

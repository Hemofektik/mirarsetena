"""Config-driven project registry (docs/SCOPE.md §3 R5-Q1).

A project is one YAML file in the config directory. Everything the service
serves is namespaced under the project slug so other SETENA projects can be
added later as config files, with no management UI.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProjectNotFound(LookupError):
    """No project is registered under the requested slug."""


def route_prefix(slug: str) -> str:
    """URL prefix isolating one project's routes: /p/{slug}."""
    return f"/p/{slug}"


def cache_key(slug: str, *parts: str) -> str:
    """Storage key namespaced by project: p/{slug}/... (SCOPE §3 R5-Q1)."""
    return "/".join(("p", slug, *parts))


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    province: str
    canton: str
    district: str
    sheet: str | None = None


class Timeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    works_start: date

    @model_validator(mode="after")
    def _order(self) -> Timeline:
        if self.works_start < self.start:
            raise ValueError(
                f"works_start {self.works_start} is before timeline start {self.start}"
            )
        return self


class CacheSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_zoom: int = Field(ge=0, le=18)
    tile_budget_bytes: int = Field(gt=0)
    # LRU cap over the source prefixes (scenes/ + layers/ + coherence/).
    scene_budget_bytes: int = Field(gt=0)
    # LRU cap for the SLC scene download cache: chained interferogram pairs
    # share scenes, so one download serves every pair that needs it; this
    # bounds how many of those ~7 GB zips stay on disk (INSAR-PLAN cadence).
    slc_budget_bytes: int = Field(gt=0, default=30 * 1024**3)


class SatelliteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s2_repeat_days: int = Field(gt=0)
    s1_repeat_days: int = Field(gt=0)


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1)
    aoi_path: str
    pois_path: str
    rio_path: str | None = None  # optional work-area line overlay (OSM)
    official_path: str | None = None  # optional official cadastral perimeter (SIRI)
    # Optional plan milestones for the Plan vs. Realidad chart (L.9):
    # data/projects/<slug>/plan.json; series values come from storage.
    plan_path: str | None = None
    bbox: tuple[float, float, float, float]
    layers: list[str] = Field(min_length=1)
    timeline: Timeline
    cache: CacheSettings
    location: Location | None = None
    satellite: SatelliteSettings

    @field_validator("bbox")
    @classmethod
    def _bbox_order(cls, value: tuple[float, float, float, float]) -> tuple:
        west, south, east, north = value
        if not (west < east and south < north):
            raise ValueError(
                "bbox must be [west, south, east, north] with min < max, "
                f"got {value}"
            )
        return value


class ProjectRegistry:
    """Loads and indexes every project config from a directory at construction."""

    def __init__(self, config_dir: str | Path):
        self._config_dir = Path(config_dir)
        self._projects: dict[str, ProjectConfig] = {}
        for path in sorted(self._config_dir.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            config = ProjectConfig.model_validate(raw)
            if config.slug in self._projects:
                raise ValueError(f"duplicate project slug {config.slug!r}")
            self._projects[config.slug] = config

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    def load_pois(self, slug: str) -> list[dict]:
        """Point sample features of a project as [{"id", "coordinates"}].

        The contract shared by the InSAR series writer (sample_pois) and
        anything else that samples a product at the project's POIs; ids come
        from pois.geojson so series entries and map pins stay joinable.
        """
        config = self.get(slug)
        root = self._config_dir.resolve().parent.parent
        gj = json.loads((root / config.pois_path).read_text(encoding="utf-8"))
        return [
            {
                "id": feature["properties"]["id"],
                "coordinates": feature["geometry"]["coordinates"],
            }
            for feature in gj["features"]
            if (feature.get("geometry") or {}).get("type") == "Point"
        ]

    def get(self, slug: str) -> ProjectConfig:
        try:
            return self._projects[slug]
        except KeyError:
            raise ProjectNotFound(f"no project {slug!r}") from None

    def all(self) -> dict[str, ProjectConfig]:
        return dict(self._projects)

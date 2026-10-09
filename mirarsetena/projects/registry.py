"""Config-driven project registry (docs/SCOPE.md §3 R5-Q1).

A project is one YAML file in the config directory. Everything the service
serves is namespaced under the project slug so other SETENA projects can be
added later as config files, with no management UI.
"""
from __future__ import annotations

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

    def get(self, slug: str) -> ProjectConfig:
        try:
            return self._projects[slug]
        except KeyError:
            raise ProjectNotFound(f"no project {slug!r}") from None

    def all(self) -> dict[str, ProjectConfig]:
        return dict(self._projects)

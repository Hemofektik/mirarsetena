"""Closed polar-traverse geometry for cadastral plan reconstruction.

Data source: docs/SCOPE.md Appendix A — derrotero (bearing/distance) tables
transcribed from the original 1991 plan scans of SJ-980860-1991 and
SJ-980861-1991 (Pérez Zeledón, San José), corrected against the scans and
validated on 2026-10-06 (closures 0.02 m / 0.05 m, areas within 0.01 % of the
officially stated areas).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml


class TraverseError(ValueError):
    """A traverse input or result fails its geometric contract."""


def azimuth_to_decimal(degrees: float, minutes: float) -> float:
    """Convert an azimuth written as degrees + minutes to decimal degrees."""
    if not 0 <= degrees < 360:
        raise TraverseError(f"azimuth degrees {degrees!r} outside [0, 360)")
    if not 0 <= minutes < 60:
        raise TraverseError(f"azimuth minutes {minutes!r} outside [0, 60)")
    return degrees + minutes / 60.0


def distance_m(metres: float, centimetres: float) -> float:
    """Combine the metres + centimetres columns of a derrotero row."""
    if metres < 0:
        raise TraverseError(f"negative distance {metres!r} m")
    if not 0 <= centimetres < 100:
        raise TraverseError(f"centimetres {centimetres!r} outside [0, 100)")
    return metres + centimetres / 100.0


@dataclass(frozen=True)
class Leg:
    start: str
    end: str
    azimuth_deg: float  # decimal degrees, clockwise from north
    distance_m: float


@dataclass(frozen=True)
class Plan:
    id: str
    stated_area_m2: float
    legs: tuple[Leg, ...]


@dataclass(frozen=True)
class TraverseResult:
    """Geometry of a computed traverse in local metres (origin = first vertex).

    vertices[0] is the origin; each subsequent vertex is the endpoint of the
    leg with the same index. The final vertex lands near the origin (closure).
    """

    vertices: tuple[tuple[float, float], ...]  # (east, north)
    closure_m: float
    perimeter_m: float
    area_m2: float

    def check(
        self,
        stated_area_m2: float,
        *,
        max_closure_m: float = 2.0,
        max_area_error_pct: float = 1.0,
    ) -> None:
        """Enforce the hard acceptance gates from docs/SCOPE.md §7.

        Raises TraverseError naming the failing quantity.
        """
        if self.closure_m > max_closure_m:
            raise TraverseError(
                f"closure {self.closure_m:.3f} m exceeds gate {max_closure_m} m"
            )
        error_pct = (self.area_m2 - stated_area_m2) / stated_area_m2 * 100
        if abs(error_pct) > max_area_error_pct:
            raise TraverseError(
                f"area {self.area_m2:.2f} m2 vs stated {stated_area_m2:.2f} m2 "
                f"off by {error_pct:+.3f}% (gate ±{max_area_error_pct}%)"
            )


def compute(legs: Sequence[Leg]) -> TraverseResult:
    """Forward-compute a closed traverse: vertices, closure, perimeter, area."""
    if not legs:
        raise TraverseError("traverse has no legs")
    if legs[-1].end != legs[0].start or any(
        a.end != b.start for a, b in zip(legs, legs[1:])
    ):
        raise TraverseError(
            f"open traverse: starts at {legs[0].start!r}, ends at {legs[-1].end!r}"
        )
    east = north = 0.0
    vertices = [(0.0, 0.0)]
    perimeter = 0.0
    for leg in legs:
        bearing = math.radians(leg.azimuth_deg)
        east += leg.distance_m * math.sin(bearing)
        north += leg.distance_m * math.cos(bearing)
        vertices.append((east, north))
        perimeter += leg.distance_m
    closure = math.hypot(east, north)
    area = (
        abs(
            sum(
                vertices[i][0] * vertices[i + 1][1]
                - vertices[i + 1][0] * vertices[i][1]
                for i in range(len(vertices) - 1)
            )
        )
        / 2.0
    )
    return TraverseResult(tuple(vertices), closure, perimeter, area)


def load_plans(path: str | Path) -> tuple[Plan, ...]:
    """Load derrotero plans from a YAML file (see data/projects/*/derrotero.yaml)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    plans = []
    for item in raw["plans"]:
        legs = tuple(
            Leg(
                str(row["start"]),
                str(row["end"]),
                azimuth_to_decimal(*row["azimuth"]),
                distance_m(*row["distance"]),
            )
            for row in item["legs"]
        )
        plans.append(Plan(str(item["id"]), float(item["stated_area_m2"]), legs))
    return tuple(plans)

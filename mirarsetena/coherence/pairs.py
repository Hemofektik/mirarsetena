"""SLC interferometric pair selection.

Pairs must share relative orbit AND orbit state (the AOI sees both a
descending ~23:48 pass and an ascending pass; crossing them yields no
interferogram). Nominal baseline: 12 days, accepted within [11, 13].
"""
from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from mirarsetena.pipeline.catalog import Scene

MIN_BASELINE_DAYS = 11.0
MAX_BASELINE_DAYS = 13.0


@dataclass(frozen=True)
class InterferometricPair:
    first: Scene
    second: Scene

    @property
    def id(self) -> str:
        return f"{self.first.id}|{self.second.id}"


def _parse(datetime_str: str) -> datetime:
    return datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))


def plan_pairs(scenes: Sequence[Scene]) -> list[InterferometricPair]:
    """Consecutive same-track scenes with a 12-day-ish temporal baseline."""
    usable = [
        scene
        for scene in scenes
        if scene.orbit_state is not None and scene.relative_orbit is not None
    ]
    tracks: dict[tuple, list[Scene]] = {}
    for scene in usable:
        tracks.setdefault(
            (scene.relative_orbit, scene.orbit_state), []
        ).append(scene)

    pairs: list[InterferometricPair] = []
    for track_scenes in tracks.values():
        ordered = sorted(track_scenes, key=lambda scene: scene.datetime)
        for first, second in itertools.pairwise(ordered):
            delta_days = (_parse(second.datetime) - _parse(first.datetime)).total_seconds() / 86400
            if MIN_BASELINE_DAYS <= delta_days <= MAX_BASELINE_DAYS:
                pairs.append(InterferometricPair(first=first, second=second))

    pairs.sort(key=lambda pair: (pair.first.datetime, pair.second.datetime))
    return pairs


def pair_key(pair: InterferometricPair) -> str:
    """Job dedupe key for one pair."""
    return f"coherence:{pair.id}"

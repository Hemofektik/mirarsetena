"""SLC interferometric pair selection.

Pairs must share relative orbit AND orbit state (the AOI sees both a
descending ~23:48 pass and an ascending pass; crossing them yields no
interferogram). Nominal baseline: 12 days, accepted within [11, 13].
"""
from __future__ import annotations

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


def plan_pairs(
    scenes: Sequence[Scene],
    *,
    min_days: float = MIN_BASELINE_DAYS,
    max_days: float = MAX_BASELINE_DAYS,
) -> list[InterferometricPair]:
    """Consecutive same-track scenes within a temporal-baseline window.

    Defaults plan the 12-day chain. Wider windows (min_days=23,
    max_days=25) close SBAS triangles over that chain: on a consecutive
    chain alone the per-date atmosphere is indistinguishable from motion
    (rank-deficient), and (t0,t1),(t1,t2),(t0,t2) is the minimal
    redundancy MintPy's inversion needs.
    """
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
        # for each scene, pair it with its NEAREST preceding scene inside
        # the baseline window (adjacency alone would only catch 24-day
        # pairs where a 12-day acquisition is missing)
        for i, second in enumerate(ordered):
            for first in reversed(ordered[:i]):
                delta_days = (
                    _parse(second.datetime) - _parse(first.datetime)
                ).total_seconds() / 86400
                if delta_days > max_days:
                    break
                if delta_days >= min_days:
                    pairs.append(InterferometricPair(first=first, second=second))
                    break

    pairs.sort(key=lambda pair: (pair.first.datetime, pair.second.datetime))
    return pairs


def pair_key(pair: InterferometricPair) -> str:
    """Job dedupe key for one pair."""
    return f"coherence:{pair.id}"

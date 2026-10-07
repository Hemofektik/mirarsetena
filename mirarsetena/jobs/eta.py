"""Orbit-repeat next-acquisition prediction from the last scene seen.

Cadences come from the project config (observed: S2 ~5 days across the
constellation, S1D 12 days). Missions silent for more than 2.5 cycles are
considered quiet and yield no prediction rather than a stale one.
"""
from __future__ import annotations

import math
from datetime import date, timedelta


def next_acquisition(
    last_seen: date | None, repeat_days: int, today: date
) -> date | None:
    """First acquisition strictly after the last scene, or None when stale."""
    if last_seen is None or repeat_days <= 0:
        return None
    silence = (today - last_seen).days
    if silence < 0:
        return last_seen  # clock skew: treat the scene as just acquired
    if silence > 2.5 * repeat_days:
        return None
    cycles = max(1, math.ceil(silence / repeat_days))
    return last_seen + timedelta(days=cycles * repeat_days)

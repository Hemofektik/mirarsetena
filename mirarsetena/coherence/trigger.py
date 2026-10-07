"""Trigger policy: first plain-data view starts the eager coherence job.

After the first view, newly discovered SLC scenes append exactly one pair
job each; pair handlers skip results that already exist, so eager and
append paths can never double-compute (SCOPE R2-Q1).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence

from mirarsetena.coherence.pairs import InterferometricPair, pair_key
from mirarsetena.coherence.pipeline import pair_to_payload
from mirarsetena.projects.registry import cache_key
from mirarsetena.storage import Storage


def viewed_flag_key(slug: str) -> str:
    return cache_key(slug, "jobs", "viewed.json")


def on_plain_data_view(
    storage: Storage,
    slug: str,
    pairs: Sequence[InterferometricPair],
    queue,
) -> bool:
    """Enqueue the single eager job on the project's first data view.

    Returns True only when the first-view transition happened now.
    """
    if storage.get(viewed_flag_key(slug)) is not None:
        return False
    storage.put(
        viewed_flag_key(slug),
        json.dumps(
            {"viewed_at": datetime.now(timezone.utc).isoformat()}
        ).encode("utf-8"),
    )
    queue.enqueue(
        "coherence_eager",
        {"slug": slug, "pairs": [pair_to_payload(pair) for pair in pairs]},
        dedupe=f"coherence_eager:{slug}",
    )
    return True


def enqueue_new_pairs(slug: str, pairs: Sequence[InterferometricPair], queue) -> int:
    """Append one pair job per not-yet-queued pair; returns jobs added."""
    enqueued = 0
    for pair in pairs:
        if queue.enqueue(
            "coherence_pair",
            {"slug": slug, "pair": pair_to_payload(pair)},
            dedupe=pair_key(pair),
        ):
            enqueued += 1
    return enqueued

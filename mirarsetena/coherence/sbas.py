"""SBAS: per-chain weighted least-squares inversion over per-edge LOS.

The per-pair products are individually noise-dominated (L.7 validation:
atmosphere ±0.5–1 cm per pair). SBAS separates atmosphere from motion by
REDUNDANCY: the 24-day closers over the 12-day chain mean each date pair
is observed on multiple paths, and the least-squares inversion averages
the inconsistencies out. On a chain WITHOUT closers the solution
degenerates to plain integration (rank T−1, zero redundancy) — honest,
but it cannot beat the atmosphere.

This is the in-repo decision of INSAR-PLAN L.6a: the standard MintPy
ISCE2 loader needs the classic topsApp stack layout this pipeline
deliberately never produces (crop-unwrap), so the inversion runs on the
geocoded, referenced, coherence-masked products as built.

Model per pixel: observation d_ij (edge LOS, first→second) satisfies
d_ij = x_j − x_i + noise, with the first date pinned at x_0 = 0. Each
pixel is solved from exactly the edges where it has valid data; a date
whose value is not observable from those edges stays NaN (a leaf pixel
missing its only edge to that date cannot know it).
"""
from __future__ import annotations

import numpy as np

_EPS = 1e-9


def solve_sbas(
    dates: list[str],
    edges: list[tuple[str, str, np.ndarray]],
) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Invert one chain. Returns (dates, fields, valid).

    dates: ascending acquisition dates of ONE chain (satellite+track —
    cross-sensor pairs must never be mixed here).
    edges: (first_date, second_date, delta) per interferometric pair;
    deltas broadcast to a common shape (scalars allowed) with NaN for
    pixels that must not be used.
    fields: per-date cumulative LOS relative to dates[0] (pinned at 0),
    same trailing shape as the broadcast deltas; NaN where unobservable.
    valid: pixel mask — True where at least one edge was usable.
    """
    if not dates:
        raise ValueError("no dates")
    index = {d: i for i, d in enumerate(dates)}
    for first, second, _delta in edges:
        if first not in index or second not in index:
            raise ValueError(f"edge {first}->{second} outside dates")

    deltas = np.broadcast_arrays(
        *[np.atleast_1d(np.asarray(delta, dtype=np.float64)) for _, _, delta in edges]
    )
    shape = deltas[0].shape
    n_obs = int(np.prod(shape)) if shape else 1
    flat = [d.reshape(n_obs) for d in deltas]
    usable = [np.isfinite(f) for f in flat]
    valid = np.zeros(n_obs, dtype=bool)
    for mask in usable:
        valid |= mask

    t = len(dates)
    # group pixels by their edge-validity pattern (<= 2^E groups; E is
    # small — 5..9 edges per chain — so each group is one dense solve)
    patterns = np.zeros(n_obs, dtype=np.int64)
    for e, mask in enumerate(usable):
        patterns |= mask.astype(np.int64) << e

    fields_flat = np.full((t, n_obs), np.nan, dtype=np.float64)
    fields_flat[0, :] = 0.0  # reference date pinned

    for pattern in np.unique(patterns[valid]):
        use = np.flatnonzero((patterns == pattern) & valid)
        if use.size == 0:
            continue
        rows = [
            (index[first], index[second])
            for e, (first, second, _) in enumerate(edges)
            if pattern >> e & 1
        ]
        # design for unknowns x_1..x_{T-1}: +1 at j (if j>0), -1 at i (if i>0)
        a = np.zeros((len(rows), t - 1), dtype=np.float64)
        for r, (i, j) in enumerate(rows):
            if j > 0:
                a[r, j - 1] += 1.0
            if i > 0:
                a[r, i - 1] -= 1.0
        d = np.stack([flat[e][use] for e in range(len(edges)) if pattern >> e & 1])
        rhs = a.T @ d  # (T-1, P)
        gram = a.T @ a
        # observability: e_t is determined iff it lies in range(gram)
        projector = gram @ np.linalg.pinv(gram)
        sol = np.linalg.pinv(gram) @ rhs if gram.size else np.zeros((t - 1, use.size))
        for ti in range(1, t):
            e_t = np.zeros(t - 1)
            e_t[ti - 1] = 1.0
            if np.allclose(projector @ e_t, e_t, atol=1e-8):
                fields_flat[ti, use] = sol[ti - 1]

    fields = fields_flat.reshape((t, *shape))
    valid = valid.reshape(shape)
    return dates, fields, valid


def closure_stats(
    edges: list[tuple[str, str, np.ndarray]],
) -> list[dict]:
    """Per-triangle closure statistics — the network's inconsistency metric.

    For every triangle (i,j),(j,k),(i,k) the closure
    d(i,k) − d(i,j) − d(j,k) is zero only when the three observations
    agree; per-edge atmosphere and reference drift show up here as a
    constant offset on the common-valid pixels. Returned sorted by
    median |closure| descending.
    """
    by_pair = {(i, j): np.asarray(d, dtype=np.float64) for i, j, d in edges}
    nodes = sorted({n for i, j in by_pair for n in (i, j)})
    out = []
    for a_i, a in enumerate(nodes):
        for b_i in range(a_i + 1, len(nodes)):
            b = nodes[b_i]
            if (a, b) not in by_pair:
                continue
            for c in nodes[b_i + 1 :]:
                paths = [(a, b, b, c, a, c), (a, c, c, b, a, b)]
                for i, j, k, l, m, n in paths:
                    if (k, l) in by_pair and (m, n) in by_pair:
                        closure = by_pair[(m, n)] - by_pair[(i, j)] - by_pair[(k, l)]
                        valid = closure[np.isfinite(closure)]
                        if valid.size == 0:
                            continue
                        out.append(
                            {
                                "triangle": (a, b, c),
                                "median_abs": float(np.median(np.abs(valid))),
                                "p90_abs": float(np.percentile(np.abs(valid), 90)),
                                "n": int(valid.size),
                            }
                        )
                        break
    out.sort(key=lambda s: -s["median_abs"])
    return out


def rereference_edges(
    edges: list[np.ndarray], reference: np.ndarray
) -> tuple[list[np.ndarray], list[float | None]]:
    """Re-zero every edge on ONE fixed reference pixel set.

    Per-pair referencing subtracts each edge's own coherent median over
    its own valid pixels — mutually inconsistent zero levels the LSQ then
    has to absorb. With a shared `reference` mask the offsets become
    comparable; an edge with no valid reference pixel is left untouched
    (offset None — honestly unreferenced rather than shifted by a guess).
    """
    out = []
    offsets: list[float | None] = []
    for edge in edges:
        arr = np.asarray(edge, dtype=np.float64).copy()
        in_ref = np.asarray(reference, dtype=bool) & np.isfinite(arr)
        if not in_ref.any():
            out.append(arr)
            offsets.append(None)
            continue
        offset = float(np.median(arr[in_ref]))
        arr[np.isfinite(arr)] -= offset
        out.append(arr)
        offsets.append(offset)
    return out, offsets

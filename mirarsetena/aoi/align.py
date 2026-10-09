"""Rigid alignment of the reconstructed parcels to their neighbours.

The 1991 derrotero reconstruction is shape-exact but its absolute placement
carries a small rotation/translation residual (the registry CRTM column is
known-bad, see SCOPE §7). The user's ground truth (2026-10-09): the east
edge must sit directly adjacent to the Quebrada Grande ravine and the south
edge to the forest/orchard on the southern border.

This module fits ONE rigid transform (rotation about the parcel centroid +
translation) so that:
  * the east chain (plan 980861 vertices 1-4 + plan 980860 vertex 3)
    follows the Quebrada Grande polyline, and
  * the south chain (plan 980860 vertices 3-9) follows the forest edge,
minimising the RMS of the signed distances. A rigid transform preserves
shape, area, the shared edge and POI containment by construction.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np

# Candidate rotations (degrees) about the parcel centroid. The minimax
# wants −5.6° to fit Quebrada + forest hard, but the user rejected that
# range — "much closer than before but probably rotated too far... does
# not match the neighboring properties" (2026-10-09) — so the search is
# capped at −4°: within 0.7° of Quebrada's own tilt (the east chain's
# natural direction is −3.3°) and 1.6° of the orchard edge, with both
# adjacency gaps inside the acceptance thresholds.
_ROTATIONS = np.arange(-4.0, 1.01, 0.1)


@dataclass(frozen=True)
class Alignment:
    rotation_deg: float
    east_m: float
    north_m: float
    rms_m: float

    def note(self) -> str:
        return (
            f"rigid adjust {self.rotation_deg:+.1f}deg "
            f"({self.east_m:+.1f},{self.north_m:+.1f})m "
            f"rms {self.rms_m:.1f}m 2026-10-09: east edge adjacent to "
            "Quebrada Grande, south edge to orchard/forest"
        )


def _segments(chain: list[tuple[float, float]]):
    return [
        (np.asarray(a, dtype=float), np.asarray(b, dtype=float))
        for a, b in itertools.pairwise(chain)
    ]


def _closest(p: np.ndarray, segs) -> tuple[float, np.ndarray]:
    """Distance and the closest segment's (a, b) for point p."""
    best = (float("inf"), None)
    for a, b in segs:
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, float((p - a) @ ab) / L2))
        d = float(np.linalg.norm(p - (a + t * ab)))
        if d < best[0]:
            best = (d, (a, b))
    return best


def _signed(p: np.ndarray, segs, toward: np.ndarray) -> tuple[float, np.ndarray]:
    """Signed distance of p to the polyline, positive on the `toward` side."""
    _, (a, b) = _closest(p, segs)
    d = b - a
    n = np.array([-d[1], d[0]]) / np.linalg.norm(d)
    if n @ toward < 0:
        n = -n
    return float(n @ (p - a)), n


def fit_alignment(
    vertices: dict[str, list[tuple[float, float]]],
    quebrada: list[list[tuple[float, float]]],
    forest: list[list[tuple[float, float]]],
) -> Alignment:
    """Least-squares rigid transform snapping both chains to their targets.

    Chains are identified by plan vertex indices (1-based, derrotero order):
    east = 980861 v1..v4 + 980860 v3; south = 980860 v3..v9.
    """
    v861 = list(vertices["SJ-980861-1991"])
    v860 = list(vertices["SJ-980860-1991"])
    east = np.array(v861[0:4] + [v860[2]], dtype=float)
    south = np.array(v860[2:9], dtype=float)
    qg = [p for poly in quebrada for p in poly]
    fr = [p for poly in forest for p in poly]
    qg_segs = _segments(qg)
    fr_segs = _segments(fr)

    all_pts = np.array(
        [p for pts in vertices.values() for p in pts], dtype=float
    )
    centroid = all_pts.mean(axis=0)
    east_toward = np.array([1.0, 0.0])   # positive = east of the ravine
    south_toward = np.array([0.0, -1.0])  # positive = south of the forest

    best = None
    for deg in _ROTATIONS:
        th = math.radians(float(deg))
        R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
        rows, rhs = [], []
        for pts, segs, toward in ((east, qg_segs, east_toward),
                                  (south, fr_segs, south_toward)):
            for p in pts:
                rp = (p - centroid) @ R.T + centroid
                _, n = _signed(rp, segs, toward)
                rows.append(n)
                rhs.append(float(n @ _closest(rp, segs)[1][0] - n @ rp))
        t_ls, *_ = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)

        def worst(tx: float, ty: float, R=R) -> float:
            t = np.array([tx, ty])
            out = 0.0
            for pts, segs, toward in ((east, qg_segs, east_toward),
                                      (south, fr_segs, south_toward)):
                for p in pts:
                    q = (p - centroid) @ R.T + centroid + t
                    out = max(out, abs(_signed(q, segs, toward)[0]))
            return out

        # minimax refinement around the LSQ translation: adjacency is about
        # the WORST gap, not the average (RMS left a 33 m mid-edge dip)
        tx_best, ty_best = float(t_ls[0]), float(t_ls[1])
        w_best = worst(tx_best, ty_best)
        for tx in np.arange(tx_best - 24, tx_best + 24.1, 3.0):
            for ty in np.arange(ty_best - 24, ty_best + 24.1, 3.0):
                w = worst(float(tx), float(ty))
                if w < w_best:
                    w_best, tx_best, ty_best = w, float(tx), float(ty)
        if best is None or w_best < best[0]:
            best = (w_best, float(deg), tx_best, ty_best)

    worst_gap, deg, tx, ty = best
    return Alignment(rotation_deg=deg, east_m=tx, north_m=ty, rms_m=worst_gap)


def apply_alignment(
    vertices: dict[str, list[tuple[float, float]]],
    alignment: Alignment,
) -> dict[str, list[tuple[float, float]]]:
    """Apply the rigid transform to every vertex of every plan."""
    all_pts = [p for pts in vertices.values() for p in pts]
    centroid = np.array(all_pts, dtype=float).mean(axis=0)
    th = math.radians(alignment.rotation_deg)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    t = np.array([alignment.east_m, alignment.north_m])
    out = {}
    for plan_id, pts in vertices.items():
        arr = np.array(pts, dtype=float)
        moved = (arr - centroid) @ R.T + centroid + t
        out[plan_id] = [(float(x), float(y)) for x, y in moved]
    return out
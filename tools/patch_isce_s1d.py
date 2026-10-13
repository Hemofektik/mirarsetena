"""Patch ISCE2 to recognise Sentinel-1C/1D (INSAR-PLAN L.1 spike).

ISCE 2.6.3 (2023-04) predates Sentinel-1C/S1D (launched Dec 2024) and its
TOPS sensor raises "Encountered unknown mission id S1D" — so every post-works
acquisition over this AOI (all S1D since 2025) fails to extract. The check
only computes `burst.trackNumber` from a per-satellite ascending-node offset;
S1A/S1C/S1D share the S1A offset for a self-consistent value (pairs are
same-satellite and we use embedded state vectors, not orbit files), so the
fix maps the unknown-mission branch onto S1A instead of raising.

Idempotent: safe to run repeatedly; exits early if already patched. Applies
to the active isce2 environment's Sentinel1.py (site-packages). Not a repo
source file — it patches the conda package — so this tool is the tracked,
reproducible record of the change.
"""
from __future__ import annotations

import sys
from pathlib import Path

OLD = """            if mission == 'S1A':
                burst.trackNumber = (orbitnumber-73)%175 + 1
            elif mission == 'S1B':
                burst.trackNumber = (orbitnumber-27)%175 + 1
            else:
                raise ValueError('Encountered unknown mission id {0}'.format(mission))"""

NEW = """            if mission == 'S1B':
                burst.trackNumber = (orbitnumber-27)%175 + 1
            else:
                # S1A/S1C/S1D share the S1A ascending-node offset (self-
                # consistent within a satellite; pairs are same-satellite and
                # we use embedded state vectors). ISCE 2.6.3 predates S1C/S1D.
                burst.trackNumber = (orbitnumber-73)%175 + 1"""


def sentinel1_py() -> Path | None:
    """Path to the active isce2 Sentinel1.py, or None."""
    try:
        import isce  # noqa: F401
        import isceobj.Sensor.TOPS.Sentinel1 as mod

        return Path(mod.__file__)
    except ImportError:
        return None


def patch(path: Path) -> bool:
    """Apply the S1C/S1D fix. Returns True if the file changed."""
    text = path.read_text(encoding="utf-8")
    if "S1A/S1C/S1D share the S1A ascending-node offset" in text:
        return False  # already patched
    if OLD not in text:
        raise SystemExit(
            f"unexpected Sentinel1.py layout in {path} — ISCE version drift?"
        )
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")  # keep one pristine copy
    path.write_text(text.replace(OLD, NEW), encoding="utf-8")
    return True


def main() -> int:
    path = sentinel1_py()
    if path is None:
        print("isce2 not importable in this interpreter — nothing to patch")
        return 1
    if patch(path):
        print(f"patched {path} (S1C/S1D now recognised)")
    else:
        print(f"already patched: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Unwrap an AOI crop of a filtered interferogram (INSAR-PLAN L.1 fast path).

Full-swath snaphu is pathological over a partly-decorrelated tropical scene
(measured: >55 min, no output, on the 1286x1748 IW2 merged product) because
most of the swath is zero-magnitude noise that still costs the MST solver.
Unwrapping only the AOI window runs in **seconds** and is the architecture
the production pipeline should use.

Reads the ISCE merged `filt_topophase.flat` (complex64) + `topophase.cor`
(coherence band), crops to the AOI radar window, runs snaphu on the crop,
and reports the unwrapped LOS displacement statistics. Radar coordinates
(no geocoding here — the production chain geocodes after unwrap).

Usage (inside the isce2 env, from a processed pair directory):
    python -m tools.unwrap_crop --bbox -83.675,9.378,-83.660,9.397
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

C_BAND_WAVELENGTH_M = 0.055465763
# standard Sentinel-1 orbit params for snaphu's cost model (approximate;
# they scale the statistical-cost height/phase guide, not the unwrap itself)
EARTH_RADIUS_M = 6371000.0
ALTITUDE_M = 693000.0
RANGE_LOOKS, AZIMUTH_LOOKS = 19, 7  # full->looks ratio for IW2 at this AOI


def _radar_window(merged: Path, bbox: tuple[float, float, float, float],
                  width: int, length: int) -> tuple[int, int, int, int]:
    """(row0, row1, col0, col1) crop covering the WGS84 bbox, with margin."""
    import rasterio

    west, south, east, north = bbox
    with rasterio.open(merged / "lat.rdr.full.vrt") as src:
        lat = src.read(1)
    with rasterio.open(merged / "lon.rdr.full.vrt") as src:
        lon = src.read(1)
    ri = (np.arange(length) * lat.shape[0] / length).astype(int).clip(0, lat.shape[0] - 1)
    ci = (np.arange(width) * lat.shape[1] / width).astype(int).clip(0, lat.shape[1] - 1)
    LAT = lat[np.ix_(ri, ci)]
    LON = lon[np.ix_(ri, ci)]
    target = (LAT >= south) & (LAT <= north) & (LON >= west) & (LON <= east)
    if not target.any():
        raise SystemExit("AOI bbox does not intersect the swath radar grid")
    rows, cols = np.where(target)
    m = 150  # margin for phase continuity + context
    return (
        max(0, rows.min() - m), min(length, rows.max() + m),
        max(0, cols.min() - m), min(width, cols.max() + m),
    )


def unwrap_crop(merged: Path, bbox, workdir: Path) -> dict:
    """Crop + unwrap; returns displacement stats for the AOI window."""
    import isce  # noqa: F401  # registers isce's component paths (contrib.*)
    from contrib.Snaphu.Snaphu import Snaphu  # isce2 env only

    from mirarsetena.coherence.isce_products import read_isce_image

    flat_img = read_isce_image(merged / "filt_topophase.flat")  # (L, W) cfloat
    length, width = flat_img.shape
    r0, r1, c0, c1 = _radar_window(merged, bbox, width, length)
    ch, cw = r1 - r0, c1 - c0

    flat = flat_img[r0:r1, c0:c1].copy()
    # zero any residual NaN: snaphu aborts on ANY NaN
    bad = ~(np.isfinite(flat.real) & np.isfinite(flat.imag))
    flat[bad] = 0

    # phsig.cor is ISCE's coherence file (TopsProc/runFilter writes it and
    # runUnwrapSnaphu passes it as snaphu's corrfile) — NOT topophase.cor,
    # whose band 0 is magnitude and band 1 is pre-filter coherence.
    cor = read_isce_image(merged / "phsig.cor")
    cor = cor[r0:r1, c0:c1].astype(np.float32).copy()
    cor[bad] = 0

    workdir.mkdir(parents=True, exist_ok=True)
    flat_path = workdir / "crop.flat"
    cor_path = workdir / "crop.cor"
    unw_path = workdir / "crop.unw"
    flat.tofile(flat_path)
    cor.tofile(cor_path)

    snp = Snaphu()
    snp.setInitOnly(False)
    snp.setInput(str(flat_path))
    snp.setOutput(str(unw_path))
    snp.setWidth(cw)
    snp.setCostMode("DEFO")
    snp.setEarthRadius(EARTH_RADIUS_M)
    snp.setWavelength(C_BAND_WAVELENGTH_M)
    snp.setAltitude(ALTITUDE_M)
    snp.setCorrfile(str(cor_path))
    snp.setCorrLooks(RANGE_LOOKS * AZIMUTH_LOOKS / (0.8 * 0.8))
    snp.setMaxComponents(20)
    snp.setDefoMaxCycles(4.0)
    snp.setRangeLooks(RANGE_LOOKS)
    snp.setAzimuthLooks(AZIMUTH_LOOKS)
    snp.setCorFileFormat("FLOAT_DATA")
    snp.setInitMethod("MST")
    snp.prepare()
    snp.unwrap()

    from mirarsetena.coherence.isce_products import read_snaphu_unw

    phase, amp = read_snaphu_unw(unw_path, (ch, cw))
    los_cm = C_BAND_WAVELENGTH_M * phase / (4 * np.pi) * 100.0
    valid = amp > 0
    v = los_cm[valid]
    return {
        "crop_shape": [ch, cw],
        "crop_window": [r0, r1, c0, c1],
        "nan_masked": int(bad.sum()),
        "valid_px": int(valid.sum()),
        "los_cm_median": float(np.median(v)) if v.size else None,
        "los_cm_p2p": float(v.max() - v.min()) if v.size else None,
        "los_cm_std": float(v.std()) if v.size else None,
        "unw_path": str(unw_path),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--merged", default="merged")
    p.add_argument("--bbox", required=True, help="W,S,E,N in WGS84")
    p.add_argument("--workdir", default="/tmp/unwrap_crop")
    args = p.parse_args()
    bbox = tuple(float(x) for x in args.bbox.split(","))
    stats = unwrap_crop(Path(args.merged), bbox, Path(args.workdir))
    for k, v in stats.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

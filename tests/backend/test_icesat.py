"""ICESat-2 ground tracks over the AOI (user 2026-10-20).

Seam: mirarsetena.icesat — CMR granule metadata (open, no auth) parsed
into per-pass track segments clipped to the project bbox. Elevation
VALUES live in the ATL06 HDF5 files behind Earthdata login — out of
scope until credentials exist; the tracks show WHERE and WHEN the laser
measured.
"""
import json
from pathlib import Path

from mirarsetena.icesat import clip_polygon_to_bbox, granules_to_features

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cmr_atl06.json"

BBOX = (-83.675, 9.378, -83.660, 9.397)  # W, S, E, N


def test_fixture_parses_into_clipped_track_features():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    features = granules_to_features(payload, BBOX)
    assert features, "fixture granules must yield track features"
    for feature in features:
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Polygon"
        date = feature["properties"]["date"]
        assert len(date) == 10 and date[:4].isdigit()
        # every vertex inside (or on) the AOI bbox
        for lon, lat in feature["geometry"]["coordinates"][0]:
            assert BBOX[0] - 1e-9 <= lon <= BBOX[2] + 1e-9
            assert BBOX[1] - 1e-9 <= lat <= BBOX[3] + 1e-9
    dates = [f["properties"]["date"] for f in features]
    assert dates == sorted(dates)


def test_granule_missing_the_aoi_yields_no_feature():
    # a footprint entirely west of the bbox clips to nothing
    payload = {"feed": {"entry": [{
        "producer_granule_id": "ATL06_x.h5",
        "time_start": "2026-05-09T21:03:40Z",
        "polygons": [["10 -85 10 -84.9 9 -84.9 9 -85 10 -85"]],
    }]}}
    assert granules_to_features(payload, BBOX) == []


def test_clip_polygon_keeps_only_the_bbox_intersection():
    # a strip crossing the bbox: the result is the part inside it
    strip = [(-84.0, 9.5), (-83.5, 9.5), (-83.5, 9.3), (-84.0, 9.3)]
    clipped = clip_polygon_to_bbox(strip, BBOX)
    assert clipped
    for lon, lat in clipped:
        assert BBOX[0] <= lon <= BBOX[2]
        assert BBOX[1] <= lat <= BBOX[3]
    # a polygon fully outside clips to nothing
    assert clip_polygon_to_bbox([(-85, 10), (-84.9, 10), (-84.9, 9.9)], BBOX) == []


def _write_atl06(path, lats, lons, elevs, quality=None):
    """Minimal ATL06-shaped HDF5: per-beam land_segments arrays."""
    import h5py
    import numpy as np

    with h5py.File(path, "w") as f:
        for beam in ("gt1l", "gt2l"):
            g = f.create_group(beam)
            ls = g.create_group("land_segments")
            ls.create_dataset("latitude", data=np.asarray(lats, dtype="f8"))
            ls.create_dataset("longitude", data=np.asarray(lons, dtype="f8"))
            canon = ls.create_group("canonical")
            canon.create_dataset("elevation", data=np.asarray(elevs, dtype="f8"))
            q = (
                np.zeros(len(lats), dtype="i4") if quality is None
                else np.asarray(quality, dtype="i4")
            )
            canon.create_dataset("elevation_quality_flag", data=q)


def test_read_atl06_extracts_elevation_points_inside_the_bbox(tmp_path):
    # The elevation layer's core: ATL06 land_segments -> (lon, lat, elev)
    # points inside the AOI, bad-quality flags dropped.
    import numpy as np

    from mirarsetena.icesat import read_atl06

    lats = np.linspace(9.37, 9.41, 40)          # crosses the AOI lat
    lons = np.full(40, -83.6675)                # inside the AOI lon
    elevs = np.linspace(1100.0, 1150.0, 40)
    quality = np.zeros(40, dtype="i4")
    quality[0] = 1                              # flagged -> dropped
    h5 = tmp_path / "ATL06_test.h5"
    _write_atl06(h5, lats, lons, elevs, quality)

    points = read_atl06(h5, BBOX)
    assert points, "points inside the bbox must be extracted"
    for lon, lat, elev in points:
        assert BBOX[0] <= lon <= BBOX[2]
        assert BBOX[1] <= lat <= BBOX[3]
        assert 1000.0 <= elev <= 1200.0
    # both beams contribute (duplicate positions are fine — they are
    # different ground tracks a few hundred m apart in reality)
    assert len(points) >= 30
    # the flagged point (lat 9.37, outside anyway) never appears
    assert all(abs(e - elevs[0]) > 1e-6 for _, _, e in points)


def test_read_atl06_missing_file_raises_clearly(tmp_path):
    from mirarsetena.icesat import read_atl06

    try:
        read_atl06(tmp_path / "nope.h5", BBOX)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass

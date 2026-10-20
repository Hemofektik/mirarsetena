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

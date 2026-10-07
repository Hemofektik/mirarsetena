"""G.3 — orbit-repeat next-acquisition prediction (SCOPE R3-Q7).

Seam: mirarsetena.jobs.eta.next_acquisition.
"""
from datetime import date

from mirarsetena.jobs.eta import next_acquisition


def test_prediction_goldens():
    # Sentinel-1D: 12-day repeat, last pass Oct 3, today Oct 7 -> Oct 15
    assert next_acquisition(date(2026, 10, 3), 12, date(2026, 10, 7)) == date(2026, 10, 15)
    # Sentinel-2 constellation: 5-day observed cadence, last Oct 6 -> Oct 11
    assert next_acquisition(date(2026, 10, 6), 5, date(2026, 10, 7)) == date(2026, 10, 11)
    # acquisition "today" still means the next pass is one cycle out
    assert next_acquisition(date(2026, 10, 7), 5, date(2026, 10, 7)) == date(2026, 10, 12)


def test_stale_mission_withholds_prediction():
    # 88 days without a scene > 2.5 x 12-day cycle: mission considered quiet
    assert next_acquisition(date(2026, 7, 11), 12, date(2026, 10, 7)) is None


def test_missing_last_seen_withholds_prediction():
    assert next_acquisition(None, 12, date(2026, 10, 7)) is None

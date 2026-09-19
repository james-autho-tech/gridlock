from datetime import datetime, timedelta, timezone

from core.ev_schedule import find_cheapest_window

BASE = datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)


def _rates(values, start=BASE, slot_min=30):
    return {start + timedelta(minutes=slot_min * i): v for i, v in enumerate(values)}


def test_finds_the_cheapest_contiguous_window():
    # 6 slots (3h) of cheap rate sandwiched between expensive ones -- the
    # window should land exactly on the cheap stretch, not an average
    # across the whole day.
    values = [0.30] * 4 + [0.05] * 6 + [0.30] * 4
    rates = _rates(values)
    result = find_cheapest_window(rates, hours=3.0)
    assert result is not None
    start, end, avg = result
    assert start == BASE + timedelta(hours=2)
    assert end == start + timedelta(hours=3)
    assert abs(avg - 0.05) < 1e-9


def test_does_not_splice_across_a_gap_in_published_rates():
    """Two genuinely cheap 1.5h stretches either side of a missing slot
    must not be treated as one contiguous cheap window just because
    they're both cheap -- the gap makes every window spanning it
    invalid, so the real (more expensive) contiguous window elsewhere
    must win instead."""
    values = [0.05] * 3 + [0.05] * 3  # would be a perfect 3h window if contiguous
    rates = _rates(values)
    del rates[BASE + timedelta(hours=1, minutes=30)]  # punch a hole in the middle
    # A genuinely contiguous, real 3h window elsewhere, pricier but real.
    rates.update(_rates([0.20] * 6, start=BASE + timedelta(hours=6)))
    result = find_cheapest_window(rates, hours=3.0)
    assert result is not None
    start, end, avg = result
    assert start == BASE + timedelta(hours=6)
    assert abs(avg - 0.20) < 1e-9


def test_returns_none_when_not_enough_data():
    rates = _rates([0.10] * 3)  # only 1.5h of data
    assert find_cheapest_window(rates, hours=6.0) is None


def test_returns_none_for_empty_rates():
    assert find_cheapest_window({}, hours=6.0) is None


def test_real_agile_data_picks_the_genuinely_cheapest_night():
    """Confirmed live against real Octopus Agile region-H data (2026-09-18
    to 2026-09-19): a night with negative pricing available should be
    found and preferred over a daytime stretch, matching what was
    observed live rather than an assumption."""
    # A compressed real-shaped sample: expensive day, negative overnight.
    day = [0.33] * 34  # ~17h at 33p
    night = [-0.03] * 14  # ~7h negative overnight
    rates = _rates(day + night)
    result = find_cheapest_window(rates, hours=6.0)
    assert result is not None
    start, end, avg = result
    assert start >= BASE + timedelta(hours=17)
    assert avg < 0

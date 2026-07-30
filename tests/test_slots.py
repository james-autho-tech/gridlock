from datetime import datetime, timedelta, timezone

from core.slots import build_slots

NOW = datetime(2026, 7, 30, 15, 30, tzinfo=timezone.utc)


def _build(pv_curve, horizon_slots=96):
    return build_slots(
        NOW,
        import_windows=[(NOW, NOW + timedelta(hours=72), 0.20)],
        export_windows=[(NOW, NOW + timedelta(hours=72), 0.10)],
        dispatch_windows=[],
        pv_curve=pv_curve,
        load_kwh_fn=lambda s: 0.4,
        cheap_rate=0.10,
        live_import_rate=0.20, live_export_rate=0.10,
        default_import_rate=0.20, default_export_rate=0.10,
        horizon_slots=horizon_slots, slot_min=30)


def test_pv_falls_back_to_prior_day_past_solcast_coverage():
    """Solcast only ever publishes a "today" + "tomorrow" sensor — a 48h
    horizon from an afternoon/evening "now" reaches into the day after
    tomorrow, which neither sensor covers. Real production symptom: a
    whole day's pv_kwh silently reading 0.0 including midday, planning
    as if there's no solar coming and draining the battery for no real
    reason. Only today+tomorrow are populated here; slots reaching into
    the third day must repeat the prior day's same time-of-day instead
    of defaulting to zero."""
    base = NOW.replace(minute=0, second=0, microsecond=0)
    pv_curve = {}
    # A simple midday-peak curve for "today" and "tomorrow" only —
    # nothing for the day after (mirrors real Solcast coverage).
    for day in range(2):
        for half_hour in range(48):
            t = base + timedelta(days=day, minutes=30 * half_hour)
            hour = t.hour
            pv_curve[t] = max(0.0, 3.0 - abs(hour - 13) * 0.4)

    slots = _build(pv_curve)
    third_day_midday = [s for s in slots
                        if s["start"].date() == (NOW.date() + timedelta(days=2))
                        and s["start"].hour == 13]
    assert third_day_midday, "fixture should include a third-day midday slot"
    for s in third_day_midday:
        assert s["pv"] > 1.0, (
            f"slot {s['start']} has no Solcast coverage (3rd day) and should have "
            f"repeated the prior day's midday PV instead of defaulting to 0; got {s['pv']}")


def test_pv_defaults_to_zero_when_no_data_exists_at_all():
    """No fallback data available anywhere -> 0.0, same as before (not
    worse) — this isn't about inventing solar from nothing, only about
    preferring real prior-day data over a bare default when it exists."""
    slots = _build(pv_curve={}, horizon_slots=4)
    assert all(s["pv"] == 0.0 for s in slots)

"""backtest_day_cost — apply a tariff's rate structure (the same shape
as apps.yaml's compare_tariffs entries: a default rate, optional HH:MM
off-peak windows, a flat export rate, an optional daily standing
charge) to a day's REAL recorded per-half-hour import/export kWh,
rather than re-optimizing a forward-looking plan.

Exists because a manual, one-off version of this exact calculation
(done in chat, cross-referencing several separately-fetched real data
sources) produced two different answers for the same real day before
the mistake was caught — a fragile, error-prone way to answer "which
tariff would actually have been cheapest," and not something anyone
should have to redo by hand. Pure and unit-tested so gridlock.py's
daily backtest can't silently disagree with itself.
"""

from datetime import time as dtime


def _rate_for_slot(slot_idx, slot_min, import_default, import_windows):
    minute_of_day = (slot_idx * slot_min) % (24 * 60)
    t = dtime(hour=minute_of_day // 60, minute=minute_of_day % 60)
    for w in import_windows:
        st = dtime.fromisoformat(w["start"])
        en = dtime.fromisoformat(w["end"])
        inside = (st <= t < en) if st < en else (t >= st or t < en)
        if inside:
            return float(w["rate"])
    return float(import_default)


def backtest_day_cost(import_kwh_by_slot, export_kwh_by_slot, *,
                       import_default, import_windows=(), export_rate=0.0,
                       standing_gbp=0.0, slot_min=30):
    """Real £ this day's actual recorded usage would have cost under this
    tariff's rate structure — this tariff's rates applied to what
    genuinely happened that day, not a re-optimized what-if plan.
    import_kwh_by_slot/export_kwh_by_slot: lists indexed by slot-of-day
    (slot 0 = 00:00, matching gridlock.py's own slot_idx convention).
    Negative result means a net credit (exported more value than
    imported), same sign convention as the rest of the app."""
    import_cost = sum(
        kwh * _rate_for_slot(i, slot_min, import_default, import_windows)
        for i, kwh in enumerate(import_kwh_by_slot))
    export_value = sum(export_kwh_by_slot) * export_rate
    return round(import_cost - export_value + standing_gbp, 4)


def edf_windows_to_daily_pattern(windows):
    """Real EDF windows fetched as absolute (start_dt, end_dt, rate)
    tuples (see gridlock.py's poll_edf_goelec_rates) span a few real
    calendar days, but EDF's own rate structure is a genuinely daily
    recurring pattern (a fixed off-peak window, same rate every night
    for the length of the contract) — extracted here into the same
    {default, windows: [{start, end, rate}]} shape compare_tariffs
    entries already use, so backtest_day_cost() doesn't need a second
    code path for a live-fetched tariff versus a hand-typed one.
    Returns None if given no windows to work from."""
    if not windows:
        return None
    default_rate = max(rate for _, _, rate in windows)
    daily_windows = []
    seen = set()
    for start, end, rate in windows:
        if rate >= default_rate - 1e-9:
            continue
        key = (start.strftime("%H:%M"), end.strftime("%H:%M"))
        if key in seen:
            continue
        seen.add(key)
        daily_windows.append({"start": key[0], "end": key[1], "rate": rate})
    return {"default": default_rate, "windows": daily_windows}

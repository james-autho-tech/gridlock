"""find_cheapest_window — pure scheduling logic for EV smart-charging on
Octopus Agile, extracted so it's unit-testable without any HA/AppDaemon
machinery. See gridlock.py's _schedule_ev_agile_charge for how it's used
and why this exists (Agile has no equivalent to Intelligent Octopus Go's
own dispatch scheduling).
"""

from datetime import timedelta


def find_cheapest_window(rates_by_start, hours, slot_min=30):
    """rates_by_start: {slot_start_datetime: £/kWh}, one entry per real
    published half-hour (or slot_min-minute) slot. Returns
    (start, end, avg_rate) for the cheapest genuinely contiguous window
    of the given length, or None if no such window exists — either too
    little data overall, or a gap in published rates breaks every
    candidate window of that length (checked explicitly rather than
    just picking the lowest-index/lowest-average slots found, which
    would silently splice together two unrelated stretches either side
    of a gap as if they were one window)."""
    slots_needed = round(hours * 60 / slot_min)
    if slots_needed < 1 or len(rates_by_start) < slots_needed:
        return None
    ordered = sorted(rates_by_start.items())
    best_start, best_avg = None, None
    for i in range(len(ordered) - slots_needed + 1):
        window = ordered[i:i + slots_needed]
        start = window[0][0]
        if not all(window[j][0] == start + timedelta(minutes=slot_min * j)
                   for j in range(slots_needed)):
            continue
        avg = sum(v for _, v in window) / slots_needed
        if best_avg is None or avg < best_avg:
            best_avg, best_start = avg, start
    if best_start is None:
        return None
    return best_start, best_start + timedelta(hours=hours), best_avg

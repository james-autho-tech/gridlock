"""Pure slot-model builder — ported from the old GridLock.build_slots,
minus the HA reads. gridlock.py fetches rate windows / dispatch windows /
PV curve / a load-forecast callable via the tariff.py / forecast.py
providers and passes them straight in here.
"""

from datetime import timedelta


def rate_at(windows, t, default):
    for s, e, v in windows:
        if s <= t < e:
            return v
    return default


def _pv_for_slot(pv_curve, s):
    """Solcast only publishes a "today" and "tomorrow" forecast sensor —
    a 48h horizon reaches into the day *after* tomorrow for a large part
    of the day (e.g. from any afternoon/evening "now"), which neither
    sensor covers at all. Falling back to a bare 0.0 there means "assume
    zero solar", the single most pessimistic possible assumption — it
    reads as correct planning but drains the battery for no real reason
    once the horizon runs past Solcast's own visibility (observed in
    production: a whole day showing 0 PV including midday, ending in
    Bypass mid-afternoon on a day forecast at 38-56kWh solar). Repeating
    the same time-of-day from 24h earlier — real data, when available —
    is a far better default than that."""
    if s in pv_curve:
        return pv_curve[s]
    return pv_curve.get(s - timedelta(hours=24), 0.0)


def _octoplus_baseline_for_slot(windows, s):
    """windows: list of (start, end, baseline_kwh, points_per_kwh) —
    from gridlock.py's _octoplus_session_windows(), a real per-half-hour
    predicted curve from Octopus's own baseline sensor, not a guess.
    Returns (baseline_kwh, points_per_kwh) for whichever window contains
    this slot's start, or (None, None) if it falls outside every known
    session window (the common case, most slots aren't in a session)."""
    for ws, we, baseline_kwh, points_per_kwh in windows:
        if ws <= s < we:
            return baseline_kwh, points_per_kwh
    return None, None


def build_slots(now, *, import_windows, export_windows, dispatch_windows,
                 pv_curve, load_kwh_fn, cheap_rate,
                 live_import_rate, live_export_rate,
                 default_import_rate, default_export_rate,
                 horizon_slots, slot_min,
                 power_down_windows=(), power_up_windows=(),
                 power_down_export_windows=(), power_up_export_windows=()):
    """Returns a list of slot dicts:
    {start, end, imp, exp, pv, load, dispatch, ev_kwh, charge, export,
     power_down_baseline_kwh, power_down_points_per_kwh,
     power_down_export_baseline_kwh, power_up_baseline_kwh,
     power_up_export_baseline_kwh, next_cheap_idx, remaining_deficit}

    charge/export start at 0.0 — optimizer.solve() fills them in.
    """
    cheap_floor = min([v for _, _, v in import_windows], default=live_import_rate)

    base = now.replace(minute=(0 if now.minute < 30 else 30),
                        second=0, microsecond=0)
    slots = []
    for i in range(horizon_slots):
        s = base + timedelta(minutes=slot_min * i)
        e = s + timedelta(minutes=slot_min)
        imp = rate_at(import_windows, s, live_import_rate if i == 0 else default_import_rate)
        ev_win = next(((ds, de, kwh) for ds, de, kwh in dispatch_windows if ds <= s < de), None)
        in_disp = ev_win is not None
        ev_slot_kwh = 0.0
        if in_disp:
            imp = min(imp, cheap_floor)  # IOG dispatch = off-peak price
            ds, de, win_kwh = ev_win
            num_slots = max(1, round((de - ds).total_seconds() / (slot_min * 60)))
            ev_slot_kwh = win_kwh / num_slots
        slots.append({
            "start": s, "end": e,
            "imp": imp,
            "exp": rate_at(export_windows, s,
                           live_export_rate if i == 0 else default_export_rate),
            "pv": _pv_for_slot(pv_curve, s),
            "load": load_kwh_fn(s),
            "dispatch": in_disp,
            "ev_kwh": ev_slot_kwh,
            "charge": 0.0,
            "export": 0.0,
        })
        pd_baseline, pd_points = _octoplus_baseline_for_slot(power_down_windows, s)
        pu_baseline, _ = _octoplus_baseline_for_slot(power_up_windows, s)
        # Export-side baselines reuse the same session's points rate as
        # their import-side counterpart (one joined/announced session,
        # same points currency) — the export sensor carries no separate
        # points figure of its own, so optimizer.py reads pd_points from
        # the import-side field above rather than duplicating it here.
        pd_export_baseline, _ = _octoplus_baseline_for_slot(power_down_export_windows, s)
        pu_export_baseline, _ = _octoplus_baseline_for_slot(power_up_export_windows, s)
        slots[-1]["power_down_baseline_kwh"] = pd_baseline
        slots[-1]["power_down_points_per_kwh"] = pd_points
        slots[-1]["power_down_export_baseline_kwh"] = pd_export_baseline
        slots[-1]["power_up_baseline_kwh"] = pu_baseline
        slots[-1]["power_up_export_baseline_kwh"] = pu_export_baseline

    annotate_reserve(slots, cheap_rate)
    return slots


def annotate_reserve(slots, cheap_rate):
    """For each slot, the index of the next slot (at or after it) whose
    import rate has dropped to "cheap" — the next off-peak window, as
    far as this horizon can see — plus that slot's share of the whole
    peak stretch's total unmet load (load minus PV), accumulated
    backwards from the next cheap slot. Used by the optimizer's on-peak
    reserve constraint to guarantee enough SoC survives to cover the
    rest of an on-peak stretch, not just this instant. Mutates `slots`
    in place (adds next_cheap_idx/remaining_deficit) and returns them,
    so fixtures built without build_slots() (e.g. in tests) can still
    get correct reserve annotations rather than duplicating this loop."""
    next_cheap_idx = None
    deficit_acc = 0.0
    for i in range(len(slots) - 1, -1, -1):
        if slots[i]["imp"] <= cheap_rate:
            next_cheap_idx = i
            deficit_acc = 0.0
        else:
            deficit_acc += max(0.0, slots[i]["load"] - slots[i]["pv"])
        slots[i]["next_cheap_idx"] = next_cheap_idx
        slots[i]["remaining_deficit"] = deficit_acc
    return slots

"""Pure helpers for turning raw HA history into human-meaningful
activity sessions ("when did this turn on, when did it turn off") --
kept separate from gridlock.py so the interval-building logic is
unit-testable without a live AppDaemon runtime, same shape as
core/thermal.py and core/optimizer.py.
"""


def compute_state_sessions(changes):
    """changes: chronological list of {"ts": iso_str, "state": str}
    (as returned by AppDaemon's get_history(), one entity at a time).
    Returns a list of {"state", "start", "end"} runs, one per
    contiguous run of the same state -- "end": None for whichever run
    is still ongoing at the end of the input. A single unbroken state
    across the whole window still returns exactly one session (with
    "end": None), so a caller can tell "watched but never changed"
    apart from "no history at all" by checking for an empty list
    instead of relying on session count."""
    sessions = []
    cur_state, cur_start = None, None
    for c in changes:
        state, ts = c.get("state"), c.get("ts")
        if state is None or ts is None:
            continue
        if state != cur_state:
            if cur_state is not None:
                sessions.append({"state": cur_state, "start": cur_start, "end": ts})
            cur_state, cur_start = state, ts
    if cur_state is not None:
        sessions.append({"state": cur_state, "start": cur_start, "end": None})
    return sessions


TEMPERATURE_UNITS = ("°C", "°F")


def classify_entity_history(changes, unit):
    """Given one entity's chronological history and its
    unit_of_measurement, decide how it's worth showing: as a real-valued
    curve (a temperature over time, to plot alongside what the heat pump
    was doing) or as on/off-style activity sessions (compute_state_sessions
    above) -- never both, and a numeric sensor with no temperature unit
    (voltage, frequency, wifi signal, an ever-changing counter) is neither:
    a session log of every distinct reading is just noise, and it isn't a
    temperature worth charting either, so it's dropped entirely (its
    current value is still visible wherever the caller shows a live
    snapshot).

    Returns (sessions, series) -- exactly one is non-empty."""
    numeric_vals = []
    for c in changes:
        try:
            numeric_vals.append(float(c.get("state")))
        except (TypeError, ValueError):
            numeric_vals = None
            break
    if numeric_vals is not None:
        if unit in TEMPERATURE_UNITS and len(numeric_vals) > 1:
            series = [{"ts": c.get("ts"), "value": v}
                      for c, v in zip(changes, numeric_vals) if c.get("ts") is not None]
            return [], series
        return [], []
    return compute_state_sessions(changes), []

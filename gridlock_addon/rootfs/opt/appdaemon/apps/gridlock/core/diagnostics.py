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

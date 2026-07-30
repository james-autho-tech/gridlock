"""Shared fixture builder for the optimizer tests — builds slot dicts
directly (bypassing HA rate/PV/dispatch parsing, which core/slots.py's
own build_slots() already covers) but reuses the real
core.slots.annotate_reserve() so the on-peak reserve math under test is
exactly what optimizer.py actually runs against, not a re-derived copy
of it that could silently drift."""

from datetime import datetime, timedelta, timezone

from core.slots import annotate_reserve

BASE = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def make_slots(rows, cheap_rate):
    """rows: list of dicts with at least imp/exp/pv/load. Missing keys
    (dispatch, ev_kwh) get sensible defaults."""
    slots = []
    for i, row in enumerate(rows):
        s = BASE + timedelta(minutes=30 * i)
        slots.append({
            "start": s, "end": s + timedelta(minutes=30),
            "imp": row["imp"], "exp": row["exp"],
            "pv": row.get("pv", 0.0), "load": row.get("load", 0.0),
            "dispatch": row.get("dispatch", False),
            "ev_kwh": row.get("ev_kwh", 0.0),
            "charge": 0.0, "export": 0.0,
        })
    annotate_reserve(slots, cheap_rate)
    return slots

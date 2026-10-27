"""What the car on the charger actually needs, from its own charge level.

A supplier's smart-charge dispatch (EDF/Octopus) is planned generously —
22 kWh over three hours for a car that needed 4 kWh — and the cheap rate
only lasts while the car really charges: once it reaches its limit the
supplier drops the rest at the next half-hour. Planning on the whole
dispatch had the battery counting on cheap slots that then vanished. This
module turns the car's % and charge limit into kWh and a finish time,
learning each car's battery size and the charger's rate as it charges.
"""
import math
from datetime import timedelta

# Wall-to-battery: what the car's own "energy added" misses in the charger
# and battery losses.
CHARGE_EFFICIENCY = 0.9
DEFAULT_CAPACITY_KWH = 60.0
DEFAULT_RATE_KW = 7.0
# A capacity sample needs this much charge between readings to be worth
# anything (the car reports whole percent).
LEARN_MIN_PCT = 5.0
LEARN_MIN_KWH = 2.0


def learn(state, *, charging, pct, energy_added_kwh, power_kw):
    """One reading of a car -> its updated learning state:
    {"capacity_kwh", "rate_kw", "anchor": [pct, energy_added] | None}.
    Capacity is energy added per percent gained across a charge; the rate
    is the charger power seen while it charges."""
    st = dict(state or {})
    if charging and power_kw is not None and power_kw > 1.0:
        old = st.get("rate_kw")
        st["rate_kw"] = round(power_kw if old is None else 0.8 * old + 0.2 * power_kw, 2)
    if not charging or pct is None or energy_added_kwh is None:
        st["anchor"] = None
        return st
    anchor = st.get("anchor")
    # A new session resets the car's energy-added counter.
    if not anchor or energy_added_kwh < anchor[1] - 0.05 or pct < anchor[0]:
        st["anchor"] = [pct, energy_added_kwh]
        return st
    d_pct, d_kwh = pct - anchor[0], energy_added_kwh - anchor[1]
    if d_pct >= LEARN_MIN_PCT and d_kwh >= LEARN_MIN_KWH:
        est = d_kwh / d_pct * 100.0
        if 20.0 <= est <= 200.0:
            old = st.get("capacity_kwh")
            st["capacity_kwh"] = round(est if old is None else 0.7 * old + 0.3 * est, 1)
        st["anchor"] = [pct, energy_added_kwh]
    return st


def need_kwh(pct, limit_pct, capacity_kwh):
    """Grid kWh to take the car from pct to its limit, or None if unknown."""
    if pct is None or limit_pct is None:
        return None
    return max(0.0, limit_pct - pct) / 100.0 * (capacity_kwh or DEFAULT_CAPACITY_KWH) / CHARGE_EFFICIENCY


def _slots(windows, now, slot_min):
    step = timedelta(minutes=slot_min)
    out = set()
    for start, end, *_ in windows:
        t = start.replace(minute=(start.minute // slot_min) * slot_min, second=0, microsecond=0)
        while t < end:
            if t + step > now:
                out.add(t)
            t += step
    return sorted(out)


def trim_dispatches(windows, need, rate_kw, now, slot_min=30):
    """Keep only the dispatch half-hours the car needs at rate_kw, in time
    order from now. -> (kept [(start, end, kwh)], dropped [(start, end)]).
    The half-hour it finishes in is kept — the supplier bills that one
    cheap — and everything after it is dropped."""
    step = timedelta(minutes=slot_min)
    rate = rate_kw or DEFAULT_RATE_KW
    remaining = max(0.0, need)
    kept, dropped = [], []
    for t in _slots(windows, now, slot_min):
        if remaining > 1e-6:
            hours = (t + step - max(t, now)).total_seconds() / 3600.0
            take = min(remaining, rate * hours)
            remaining -= take
            kept.append((t, t + step, take))
        else:
            dropped.append((t, t + step))
    return kept, dropped


def expected_charge(need, rate_kw, cheap_slot_starts, slot_min=30):
    """With the car plugged in but nothing scheduled yet, where it will
    charge: the first cheap half-hours from the given starts, at rate_kw.
    -> [(start, end, kwh)]."""
    step = timedelta(minutes=slot_min)
    per_slot = (rate_kw or DEFAULT_RATE_KW) * slot_min / 60.0
    remaining = max(0.0, need or 0.0)
    out = []
    for t in sorted(cheap_slot_starts):
        if remaining <= 1e-6:
            break
        take = min(remaining, per_slot)
        out.append((t, t + step, take))
        remaining -= take
    return out


def finish_time(need, rate_kw, now):
    """When the car reaches its limit charging flat out from now."""
    if need is None:
        return None
    hours = need / (rate_kw or DEFAULT_RATE_KW)
    return now + timedelta(minutes=math.ceil(hours * 60))

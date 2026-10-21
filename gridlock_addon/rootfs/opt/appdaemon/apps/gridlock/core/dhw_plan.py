"""Hot water (DHW) scheduling — heat the tank to its standard setpoint in
the cheapest window, not on a fixed timer.

Deliberately simple. The heat pump's own DHW logic is left intact (it
starts a run when the tank has dropped its hysteresis below the setpoint
and stops at the setpoint); GridWarm only decides WHEN the DHW circuit is
allowed power, so the unit does its heating in the cheapest half-hours
rather than at 01:00 because a timer said so. Cheapest means electricity
cost per kWh of heat: price divided by the COP the unit will get at the
DHW flow temperature in that slot's forecast outdoor temperature -- on a
flat tariff that alone moves the run to the warmest part of the day.

Two guard rails live in the caller: a tank floor that forces heating on
regardless of plan, and a maximum interval between runs so a window can't
be postponed for ever chasing a cheaper tomorrow.
"""

import math
from datetime import timedelta

from core import flow_control as core_flow


def slot_scores(rate_curve, outdoor_curve, flow_temp, eta):
    """Cost of a kWh of heat in each slot: p/kWh electricity divided by
    the predicted COP at the DHW flow temperature."""
    n = min(len(rate_curve), len(outdoor_curve))
    return [rate_curve[i] / core_flow.cop_estimate(flow_temp, outdoor_curve[i], eta)
            for i in range(n)]


def cheapest_window(scores, run_slots, latest_end_slot):
    """The contiguous run of run_slots with the lowest total score that
    ends no later than latest_end_slot. Returns (start, end, mean_score)
    with end exclusive, or None if the scores are empty. If the deadline
    doesn't leave room for a full run, the window is the earliest
    possible one (start now) -- a late run beats no run."""
    n = len(scores)
    if n == 0:
        return None
    run_slots = max(1, min(run_slots, n))
    latest_end_slot = max(run_slots, min(latest_end_slot, n))
    best = None
    for start in range(0, latest_end_slot - run_slots + 1):
        total = sum(scores[start:start + run_slots])
        if best is None or total < best[2] - 1e-9:
            best = (start, start + run_slots, total)
    start, end, total = best
    return start, end, total / run_slots


def run_slots_needed(run_minutes, slot_minutes):
    return max(1, math.ceil(run_minutes / slot_minutes))


def deadline_slots(hours_since_last_run, max_interval_hours, slot_hours):
    """How many slots from now the next run must have finished by. None
    for hours_since_last_run (never run / unknown) counts as "the full
    interval is available", not "overdue" -- a restart mid-day mustn't
    force a run at peak price."""
    if max_interval_hours <= 0:
        return 10 ** 6
    since = hours_since_last_run if hours_since_last_run is not None else 0.0
    remaining = max(0.0, max_interval_hours - since)
    return max(1, math.ceil(remaining / slot_hours))


def slots_until_ready_by(local_now, ready_by_times, slot_minutes):
    """Slots from now until the next of the configured HH:MM local
    times (wrapping to tomorrow), so a needed run can be made to finish
    before it. None when no times are configured."""
    if not ready_by_times:
        return None
    best = None
    for t in ready_by_times:
        try:
            h, m = (int(x) for x in str(t).split(":")[:2])
        except ValueError:
            continue
        target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= local_now:
            target = target + timedelta(days=1)
        slots = int((target - local_now).total_seconds() // (slot_minutes * 60))
        best = slots if best is None else min(best, slots)
    return max(1, best) if best is not None else None


def unit_will_heat(tank_temp, setpoint, start_hysteresis):
    """Whether powering the DHW circuit now actually produces a run: the
    unit only starts once the tank is start_hysteresis below its
    setpoint (Midea's dT5_ON). Powering it above that is harmless, just
    nothing happens -- worth telling the user rather than looking stuck."""
    if tank_temp is None:
        return True
    return tank_temp <= setpoint - start_hysteresis


# ---- Usage learning ---------------------------------------------------
# A tank that's cooling faster than its standing loss is being drawn
# from: hot water out, cold mains in. Each tick's unexplained drop,
# converted through the tank's thermal mass, is a measurement of hot
# water energy used in that half-hour of the day. Accumulated per
# half-hour bucket and blended day over day (same EMA idea as the
# learned house-load profile), it becomes a picture of when this house
# actually uses hot water -- with no diary and no shower counting.

PROFILE_SLOTS = 48           # half-hour buckets across the day
PROFILE_ALPHA = 0.2          # a new day's figure moves the bucket 20% of the way
DRAW_MIN_WH = 30.0           # below this a drop is just sensor noise / loss error


def draw_wh(prev_temp, temp, elapsed_hours, standing_loss_w, mass_wh_per_c, heating_on):
    """Hot water energy drawn over one interval, from the temperature
    drop beyond what standing loss explains. None while the tank is
    being heated (the drop is masked) or the interval is odd."""
    if heating_on or prev_temp is None or temp is None:
        return None
    if not 0.02 <= elapsed_hours <= 0.5:
        return None
    expected_drop = standing_loss_w * elapsed_hours / max(mass_wh_per_c, 1.0)
    unexplained = (prev_temp - temp) - expected_drop
    wh = unexplained * mass_wh_per_c
    return wh if wh >= DRAW_MIN_WH else 0.0


def profile_bucket(local_dt):
    return (local_dt.hour * 60 + local_dt.minute) // 30


def blend_day_into_profile(profile, day_buckets):
    """profile: 48 learned Wh figures. day_buckets: the 48 totals observed
    today (None where nothing was observed, e.g. the tank was heating
    the whole half-hour). Returns the new profile."""
    out = list(profile) if profile else [0.0] * PROFILE_SLOTS
    out = (out + [0.0] * PROFILE_SLOTS)[:PROFILE_SLOTS]
    for i, v in enumerate(day_buckets[:PROFILE_SLOTS]):
        if v is None:
            continue
        out[i] = out[i] * (1 - PROFILE_ALPHA) + v * PROFILE_ALPHA
    return out


def expected_draw(profile, start_bucket, n_slots):
    """Learned Wh per slot for the next n_slots, starting at start_bucket
    and wrapping around midnight."""
    if not profile:
        return [0.0] * n_slots
    return [profile[(start_bucket + i) % PROFILE_SLOTS] for i in range(n_slots)]


def ready_by_slot(expected, threshold_wh, lead_slots=1):
    """The slot a run should have finished by: just before the first
    slot where learned usage reaches threshold_wh (a real draw-off --
    morning showers, say), with lead_slots of margin. None when nothing
    significant is expected in the horizon, so the caller falls back to
    its max-interval deadline."""
    for i, wh in enumerate(expected):
        if wh >= threshold_wh:
            return max(1, i - lead_slots + 1)
    return None


def tank_can_cover(tank_temp, setpoint, start_hysteresis, mass_wh_per_c, expected_wh_until_next_window,
                   standing_loss_w, hours_until_next_window):
    """Can the tank skip this window? Only if, after the learned draw and
    standing loss between now and the next window, it would still be
    above the point the unit would start a run anyway. Conservative:
    a wrong "yes" means a cool tank, a wrong "no" costs one cheap run."""
    if tank_temp is None:
        return False
    usable_wh = (tank_temp - (setpoint - start_hysteresis)) * mass_wh_per_c
    needed_wh = expected_wh_until_next_window + standing_loss_w * hours_until_next_window
    return usable_wh > needed_wh * 1.25



def daily_target_temp(keep_above, base_setpoint, max_setpoint, mass_wh_per_c,
                      loss_watts, loss_degrees_per_hour_per_c, ambient_temp,
                      draw_wh_next_day, hours=24.0):
    """How hot to make the tank tonight so it's still above keep_above
    after `hours` of standing loss and the expected draw. Loss is the
    learned fixed term plus the term proportional to tank-minus-ambient,
    evaluated at the average of the start and end temperatures (a few
    fixed-point passes). Never below the unit's normal setpoint, never
    above max_setpoint."""
    mass = max(mass_wh_per_c, 1.0)
    target = base_setpoint
    for _ in range(5):
        avg = (target + keep_above) / 2.0
        loss_c = (loss_watts * hours) / mass + loss_degrees_per_hour_per_c * max(0.0, avg - ambient_temp) * hours
        target = keep_above + loss_c + draw_wh_next_day / mass
    return round(min(max_setpoint, max(base_setpoint, target)), 0), round(loss_c, 1)



def parse_ready_by(entry):
    """"HH:MM" or "HH:MM@T" -> (hour, minute, temp or None)."""
    t, _, temp = str(entry).partition("@")
    h, m = (int(x) for x in t.strip().split(":")[:2])
    tv = float(temp) if temp.strip() else None
    return h, m, tv


def upcoming_ready_bys(local_now, entries, slot_minutes):
    """[(slots_until, temp or None)] for each configured ready-by within the
    next 24 h, soonest first."""
    out = []
    for e in entries or []:
        try:
            h, m, tv = parse_ready_by(e)
        except (ValueError, TypeError):
            continue
        target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        out.append((max(1, int((target - local_now).total_seconds() // (slot_minutes * 60))), tv))
    return sorted(out)


def tank_drop_c(tank_temp, hours, mass_wh_per_c, loss_watts, loss_degrees, ambient, draw_wh):
    """How much the tank cools over `hours` with no heating: the fixed
    loss, the loss against its surroundings, and the expected draw."""
    mass = max(mass_wh_per_c, 1.0)
    return ((loss_watts * hours + draw_wh) / mass
            + loss_degrees * max(0.0, tank_temp - ambient) * hours)


# Learned draws in this many slots just before a ready-by are the use the
# ready-by is for (the evening showers), not something to heat ahead of:
# "45 °C by 19:30" means 45 °C when the showers start.
READY_BY_OWN_DRAW_SLOTS = 2

# A tank below top_up_below this close to the night waits for it.
NIGHT_WAIT_SLOTS = 12


def night_mask(slot0_local, n, window, slot_minutes):
    """[bool] per slot from slot0_local: does the slot start inside the
    "HH:MM-HH:MM" window (wrapping midnight)?"""
    try:
        a, b = str(window).split("-")
        start = int(a.split(":")[0]) * 60 + int(a.split(":")[1])
        end = int(b.split(":")[0]) * 60 + int(b.split(":")[1])
    except (ValueError, IndexError, AttributeError):
        return [True] * n
    out = []
    for i in range(n):
        t = slot0_local + timedelta(minutes=slot_minutes * i)
        m = t.hour * 60 + t.minute
        out.append(start <= m < end if start < end else (m >= start or m < end))
    return out


def next_night(mask):
    """(first slot, end slot exclusive) of the next night block, or None."""
    try:
        s = mask.index(True)
    except ValueError:
        return None
    e = s
    while e < len(mask) and mask[e]:
        e += 1
    return s, e


def night_key(local_dt):
    """Which night a time belongs to -- the date it started on, for any
    window that doesn't span midday."""
    return (local_dt - timedelta(hours=12)).date().isoformat()

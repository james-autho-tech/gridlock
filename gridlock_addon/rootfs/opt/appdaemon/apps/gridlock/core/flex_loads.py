"""Flexible loads: appliances on a smart plug (a dehumidifier, a heated
airer) that need so many hours of running a day but don't mind when.

GridLock runs each one in the half-hours where an extra kWh costs least
(core/effective_price.py: the tariff where it's importing anyway, the
battery's own charge cost where the battery would cover it, the export
rate given up where spare solar is going to the grid), in runs of at
least min_block so a compressor isn't cycled, inside any allowed hours.

Pure functions, shared by the engine (gridlock.py) and the dashboard
(webui.py validates settings through clean_settings).
"""
import math
import re
from datetime import datetime, timedelta

SLOT_MIN = 30
MAX_LOADS = 8
IDLE_W = 5.0                 # below this a plug that's on isn't really running
IDLE_ALERT_MIN = 30          # on but idle this long: tank full / room already dry
# A load's "day" runs noon to noon, so a whole cheap night (and the evening
# peak before it) is one day's choice. With a midnight boundary the hours
# still owed at 20:00 had to be run before 00:00, on battery or peak power,
# while the cheap night belonged to tomorrow.
DAY_START_HOUR = 12
BIG = 1e9

DEFAULT_LOAD = {
    "id": "",
    "name": "Dehumidifier",
    "enabled": True,
    "switch": "",
    "power": "",
    "hours_per_day": 6.0,
    "min_block_min": 60,
    "window": "",            # "" = any time, else "HH:MM-HH:MM" (may cross midnight)
    "rated_w": 300,
}

_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
_ID_RE = re.compile(r"^[a-z0-9]{4,16}$")


def _num(value, default, lo, hi, step=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    v = min(hi, max(lo, v))
    if step:
        v = round(v / step) * step
    return v


def clean_load(raw, fallback_id=""):
    """One load's settings, whitelisted and clamped."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_LOAD)
    lid = str(raw.get("id") or fallback_id).strip().lower()
    out["id"] = lid if _ID_RE.match(lid) else fallback_id
    name = str(raw.get("name") or "").strip()[:40]
    out["name"] = name or DEFAULT_LOAD["name"]
    out["enabled"] = raw.get("enabled", True) not in (False, "false", "off", 0, "0")
    sw = str(raw.get("switch") or "").strip()
    out["switch"] = sw if re.match(r"^(switch|input_boolean)\.[a-z0-9_]+$", sw) else ""
    pw = str(raw.get("power") or "").strip()
    out["power"] = pw if re.match(r"^sensor\.[a-z0-9_]+$", pw) else ""
    out["hours_per_day"] = _num(raw.get("hours_per_day"), DEFAULT_LOAD["hours_per_day"], 0.5, 24.0, 0.5)
    out["min_block_min"] = int(_num(raw.get("min_block_min"), DEFAULT_LOAD["min_block_min"], 30, 240, 30))
    window = str(raw.get("window") or "").replace(" ", "")
    out["window"] = window if _WINDOW_RE.match(window) and window[:5] != window[6:] else ""
    out["rated_w"] = int(_num(raw.get("rated_w"), DEFAULT_LOAD["rated_w"], 10, 3500, 10))
    return out


def clean_settings(raw):
    """{"loads": [...], "resume": {...}} from the dashboard's flex_loads_settings.json,
    whatever state that file is in. Loads without a usable id get a
    stable one from their position."""
    raw = raw if isinstance(raw, dict) else {}
    loads, seen = [], set()
    for i, item in enumerate((raw.get("loads") or [])[:MAX_LOADS] if isinstance(raw.get("loads"), list) else []):
        load = clean_load(item, fallback_id=f"load{i + 1}")
        while load["id"] in seen:
            load["id"] = f"{load['id'][:12]}x{len(seen)}"
        seen.add(load["id"])
        loads.append(load)
    # Resume presses from the dashboard: {load_id: ISO time}. A manual pause
    # older than the press is lifted.
    resume = {}
    if isinstance(raw.get("resume"), dict):
        for lid, at in raw["resume"].items():
            if lid in seen and isinstance(at, str) and len(at) <= 40:
                try:
                    datetime.fromisoformat(at)
                except ValueError:
                    continue
                resume[lid] = at
    return {"loads": loads, "resume": resume}


def in_window(window, local_dt):
    """Is this local time inside "HH:MM-HH:MM" ("" = always)?"""
    if not window:
        return True
    start, end = window.split("-")
    t = local_dt.strftime("%H:%M")
    return start <= t < end if start < end else (t >= start or t < end)


def slots_needed(hours_per_day, on_min_today):
    """Half-hours still to run today (a part-run half-hour counts as one)."""
    left = max(0.0, hours_per_day * 60.0 - on_min_today)
    return int(math.ceil(left / SLOT_MIN - 1e-6))


def pick_slots(prices, allowed, need, min_block, run_so_far=0):
    """The cheapest set of `need` slots out of `prices` (slot 0 = now) where
    every run of consecutive chosen slots is at least `min_block` long and
    only `allowed` slots are used. `run_so_far` = slots the load has already
    been running for in its current run (it must keep going until the run
    reaches min_block). If `need` can't be met, as many as possible.
    Returns a sorted list of chosen indexes."""
    n = min(len(prices), len(allowed))
    m = max(1, int(min_block))
    need = max(0, int(need))
    start_c = min(max(0, int(run_so_far)), m)
    if need == 0 or n == 0:
        return []
    # best[i][k][c]: (penalty, cost) from slot i on, k still to place,
    # current run length c (capped at m; 0 = not running).
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def best(i, k, c):
        if i == n:
            # Unmet slots are the big penalty; a run cut short at the end
            # (midnight) is penalised like any other short run, so the
            # plan doesn't park a lone half-hour at 23:30.
            return (k * BIG + (m - c if 0 < c < m else 0), 0.0)
        options = []
        if k > 0 and allowed[i]:
            p, cost = best(i + 1, k - 1, min(c + 1, m))
            options.append((p, cost + prices[i], 1))
        if c == 0 or c >= m:
            p, cost = best(i + 1, k, 0)
            options.append((p, cost, 0))
        if not options:
            # Mid-run but this slot isn't allowed: the run has to end here,
            # short. Better than an impossible plan.
            p, cost = best(i + 1, k, 0)
            options.append((p + m - c, cost, 0))
        p, cost, _ = min(options, key=lambda o: (o[0], o[1]))
        return (p, cost)

    chosen, k, c = [], need, start_c
    for i in range(n):
        target = best(i, k, c)
        took = False
        if k > 0 and allowed[i]:
            p, cost = best(i + 1, k - 1, min(c + 1, m))
            if (p, round(cost + prices[i], 9)) == (target[0], round(target[1], 9)):
                chosen.append(i)
                k, c, took = k - 1, min(c + 1, m), True
        if not took:
            c = 0
    best.cache_clear()
    return chosen


# ---- per-load day state -------------------------------------------------------

def empty_day(date):
    return {"date": date, "on_min": 0.0, "kwh": 0.0, "manual": None, "last_cmd": None,
            "run_started": None, "idle_since": None, "idle_notified": None, "avg_w": None}


def clean_state(raw):
    """{load_id: day state} from the engine's saved file, whatever state it's in."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for lid, s in raw.items():
        if not isinstance(s, dict) or not isinstance(s.get("date"), str):
            continue
        day = empty_day(s["date"])
        for k in ("on_min", "kwh", "avg_w"):
            try:
                if s.get(k) is not None:
                    day[k] = float(s[k])
            except (TypeError, ValueError):
                pass
        for k in ("manual", "last_cmd", "run_started", "idle_since", "idle_notified"):
            if isinstance(s.get(k), str):
                day[k] = s[k]
        out[str(lid)] = day
    return out


def day_key(local_now):
    """The load-day (noon to noon) this local time belongs to."""
    return (local_now - timedelta(hours=DAY_START_HOUR)).date().isoformat()


def next_day_start(local_now):
    """When the current load-day ends: the next local noon."""
    noon = local_now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return noon if local_now < noon else noon + timedelta(days=1)


def manual_expired(manual_iso, local_now):
    """A manual pause lasts until the midnight after it was made."""
    try:
        paused = datetime.fromisoformat(manual_iso).astimezone(local_now.tzinfo)
    except (TypeError, ValueError):
        return True
    return local_now >= next_local_midnight(paused)


def roll_day(state, day):
    """New load-day: reset the run-time and energy counts; keep the command
    we last sent, a run in progress, a manual pause (it ends at midnight on
    its own), the idle timer and the learned power."""
    if state.get("date") == day:
        return state
    fresh = empty_day(day)
    for k in ("last_cmd", "run_started", "avg_w", "manual", "idle_since", "idle_notified"):
        fresh[k] = state.get(k)
    return fresh


def add_sample(state, is_on, power_w, dt_min):
    """Count one tick: minutes switched on, kWh used, and the typical
    running power (learned, used to forecast the planned load)."""
    if not (0 < dt_min <= 15):
        return
    if is_on:
        state["on_min"] = state.get("on_min", 0.0) + dt_min
    if power_w is not None and power_w >= 0:
        state["kwh"] = state.get("kwh", 0.0) + power_w / 1000.0 * dt_min / 60.0
        if is_on and power_w >= IDLE_W:
            prev = state.get("avg_w")
            state["avg_w"] = power_w if prev is None else prev * 0.9 + power_w * 0.1


def run_slots_so_far(state, now):
    """Half-hours the current run (started by GridLock) has lasted."""
    started = state.get("run_started")
    if state.get("last_cmd") != "on" or not started:
        return 0
    try:
        t0 = datetime.fromisoformat(started)
    except ValueError:
        return 0
    return max(1, int(math.ceil((now - t0).total_seconds() / 60.0 / SLOT_MIN - 1e-6)))


def next_local_midnight(local_now):
    return (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

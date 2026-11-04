"""Autho Solar: a second solar forecast, run alongside Solcast over the winter.

Off until an Autho Solar API key is saved on the dashboard (Solar tab).

- compare: GridLock sends each completed half-hour of real PV generation to
  Autho Solar (which learns the roof from it), fetches its forecast, and
  every evening snapshots tomorrow's forecast from each source. Once the
  day has happened, each snapshot is scored against what the panels
  actually made. The battery plan keeps using Solcast.
- plan: the same, and the battery plan uses Autho Solar's forecast, with
  Solcast filling any gap and taking over completely whenever the Autho
  Solar forecast is missing or older than STALE_AFTER.

Pure functions only; gridlock.py does the HTTP and the ticking.
"""
from datetime import datetime, timedelta, timezone

DEFAULT_URL = "https://solar.autho.tech"
MODES = ("off", "compare", "plan")
PLAN_CASES = ("p50", "p10")          # median, or the pessimistic model
STALE_AFTER = timedelta(hours=3)
SNAPSHOT_HOUR_UTC = 21               # matches Autho Solar's own day-ahead scoring
HISTORY_DAYS = 120
DETAIL_DAYS = 14                     # scored days kept half-hour by half-hour for the day chart
DAYLIGHT_KWH = 0.005                 # a half-hour counts as daylight above this (forecast or actual)
RANGE_SLACK_KWH = 0.01               # rounding allowance when checking a half-hour against the range
SLOT = timedelta(minutes=30)

# Forecast sources scored each day, in display order.
SOURCES = ("solcast_raw", "solcast_corrected", "autho_p50", "autho_p10", "autho_p90")


def clean_settings(raw):
    """{"api_key", "url", "mode", "plan_case", "site_id"} from the
    dashboard's autho_solar_settings.json, whatever state that file is in."""
    raw = raw if isinstance(raw, dict) else {}
    key = raw.get("api_key")
    key = key.strip() if isinstance(key, str) else ""
    url = raw.get("url")
    url = url.strip().rstrip("/") if isinstance(url, str) and url.strip().startswith("https://") else DEFAULT_URL
    mode = raw.get("mode") if raw.get("mode") in MODES else "compare"
    if not key:
        mode = "off"
    case = raw.get("plan_case") if raw.get("plan_case") in PLAN_CASES else "p50"
    site = raw.get("site_id")
    site = site.strip() if isinstance(site, str) and site.strip() else None
    return {"api_key": key, "url": url, "mode": mode, "plan_case": case, "site_id": site}


def pick_site(sites, wanted_id):
    """The site to use: the saved one if it still exists, else the first."""
    sites = [s for s in (sites or []) if isinstance(s, dict) and s.get("id")]
    for s in sites:
        if s["id"] == wanted_id:
            return s
    return sites[0] if sites else None


def _iso(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_forecast(body):
    """Autho Solar's /forecast response -> {slot_start_utc: {"p10","p50","p90"} kWh}
    plus its issued time. Power (kW) over a half-hour is halved to kWh."""
    out = {}
    if not isinstance(body, dict):
        return out, None
    for p in body.get("periods") or []:
        t = _iso(p.get("period_start")) if isinstance(p, dict) else None
        if t is None:
            continue
        try:
            out[t] = {k: max(0.0, float(p[f"{k}_kw"])) / 2.0 for k in ("p10", "p50", "p90")}
        except (KeyError, TypeError, ValueError):
            continue
    return out, _iso(body.get("issued_at")) if body.get("issued_at") else None


def is_fresh(issued_at, fetched_at, now):
    """Usable for planning: fetched recently and not itself stale."""
    if fetched_at is None or now - fetched_at > STALE_AFTER:
        return False
    return issued_at is None or now - issued_at <= STALE_AFTER + timedelta(hours=1)


def planning_curve(autho, case, solcast_curve):
    """Autho Solar's chosen case where it has a figure, Solcast everywhere else."""
    curve = dict(solcast_curve)
    for t, v in autho.items():
        curve[t] = v[case]
    return curve


def slot_start(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)


def slot_key(dt):
    return slot_start(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- state: half-hour readings, nightly snapshots, daily scores ----------------

def empty_state():
    return {"accum": None, "queue": [], "actuals": {}, "snapshots": {}, "history": [], "detail": {}}


def clean_state(raw):
    s = empty_state()
    if isinstance(raw, dict):
        for k in ("queue", "history"):
            if isinstance(raw.get(k), list):
                s[k] = raw[k]
        for k in ("actuals", "snapshots", "detail"):
            if isinstance(raw.get(k), dict):
                s[k] = raw[k]
        if isinstance(raw.get("accum"), dict):
            s["accum"] = raw["accum"]
        if isinstance(raw.get("last_sample"), str):
            s["last_sample"] = raw["last_sample"]
        if isinstance(raw.get("meter"), dict):
            s["meter"] = raw["meter"]
        for k in ("meter_backfill", "relearn"):
            if raw.get(k) is not None:
                s[k] = raw[k]
    return s


def _record(state, slot, kwh):
    """A finished half-hour: queued for Autho Solar, kept for the day's scoring."""
    state["queue"].append({"period_start": slot, "kwh": kwh})
    state["queue"] = state["queue"][-2000:]
    state["actuals"].setdefault(slot[:10], {})[slot] = kwh


def add_sample(state, now, pv_kw, dt_h):
    """Integrate one tick of live PV into the current half-hour. When the
    half-hour changes, the finished one is queued for Autho Solar and kept
    for the day's scoring. Returns True if a half-hour was completed."""
    key = slot_key(now)
    acc = state.get("accum")
    finished = False
    if acc and acc.get("slot") != key:
        # Only report a half-hour we watched for most of its length.
        if acc.get("seen_h", 0.0) >= 0.4:
            kwh = round(acc["kwh"] * (0.5 / acc["seen_h"]) if acc["seen_h"] < 0.5 else acc["kwh"], 4)
            _record(state, acc["slot"], kwh)
            finished = True
        acc = None
    if acc is None:
        acc = {"slot": key, "kwh": 0.0, "seen_h": 0.0}
    if pv_kw is not None and 0 < dt_h <= 10 / 60:
        acc["kwh"] += max(0.0, pv_kw) * dt_h
        acc["seen_h"] += dt_h
    state["accum"] = acc
    return finished


# ---- PV energy counter ---------------------------------------------------------
# The inverter's "PV energy today" counter measures generation properly in
# weak light, where the live PV power reading often shows 0 while the panels
# are still producing (on a dull day that's half the day's energy). Half-hours
# come from the counter read at each :00 / :30, interpolated between ticks.

METER_MAX_GAP = timedelta(minutes=10)    # live: readings further apart start afresh
HISTORY_MAX_GAP = timedelta(minutes=30)  # backfill: a boundary bracketed wider than this, while the count moved, is unknown
RESET_WINDOW_MIN = 45                    # a drop this close to local midnight is the daily reset
ROUNDING_KWH = 0.005


def counter_step(prev, cur, when, tz):
    """kWh made between two readings of a daily counter, and the reading to
    compare the next one with. A drop near local midnight is the daily
    reset (count from zero); a drop at any other time is a glitch, so
    nothing is counted and the old reading stays the baseline."""
    if cur >= prev - ROUNDING_KWH:
        return max(0.0, cur - prev), max(cur, prev)
    local = when.astimezone(tz)
    mins = local.hour * 60 + local.minute
    if min(mins, 1440 - mins) <= RESET_WINDOW_MIN:
        return max(0.0, cur), cur
    return 0.0, prev


def add_meter_reading(state, now, counter_kwh, tz):
    """One tick's reading of the PV energy counter. When a :00 / :30 boundary
    passes, the finished half-hour (if it was watched from its start) is
    queued for Autho Solar and kept for scoring. Returns True if one was."""
    if counter_kwh is None:
        return False
    now = now.astimezone(timezone.utc)
    key = slot_key(now)
    m = state.get("meter") if isinstance(state.get("meter"), dict) else {}
    last_t = _iso(m.get("t")) if m.get("t") else None
    try:
        last_v, acc, slot, whole = float(m["v"]), float(m["acc"]), str(m["slot"]), bool(m["whole"])
    except (KeyError, TypeError, ValueError):
        last_t = None
    if last_t is not None and now <= last_t:
        return False
    if last_t is None or now - last_t > METER_MAX_GAP:
        # First reading, or a gap (restart, counter unavailable): count from
        # here. This half-hour wasn't watched from its start, so it isn't sent.
        state["meter"] = {"t": now.isoformat(), "v": counter_kwh, "acc": 0.0, "slot": key, "whole": False}
        return False
    kwh, base = counter_step(last_v, counter_kwh, now, tz)
    finished = False
    if key != slot:
        boundary = slot_start(now)
        frac = min(1.0, max(0.0, (boundary - last_t) / (now - last_t)))
        if whole:
            _record(state, slot, round(acc + kwh * frac, 4))
            finished = True
        acc, slot, whole = kwh * (1.0 - frac), key, True
    else:
        acc += kwh
    state["meter"] = {"t": now.isoformat(), "v": base, "acc": acc, "slot": slot, "whole": whole}
    return finished


def counter_series(points, tz, until=None):
    """[(datetime, reading)] from a daily counter's history -> [(datetime,
    running kWh total)], with daily resets and glitches handled. `until`:
    when the history was read up to (the last reading holds until then)."""
    out, total, base = [], 0.0, None
    for t, v in sorted(points, key=lambda p: p[0]):
        if base is None:
            base = v
        else:
            kwh, base = counter_step(base, v, t, tz)
            total += kwh
        out.append((t.astimezone(timezone.utc), total))
    if out and until is not None and until.astimezone(timezone.utc) > out[-1][0]:
        out.append((until.astimezone(timezone.utc), total))
    return out


def counter_at(series, t, max_gap=HISTORY_MAX_GAP):
    """Running total at time t, interpolated between the readings either
    side; None outside the history, or across a gap where the count moved
    (Home Assistant was down, so when it moved isn't known). The series
    should end with a point at the time the history was read up to."""
    import bisect
    if not series or t < series[0][0] or t > series[-1][0]:
        return None
    i = bisect.bisect_right([p[0] for p in series], t)
    t1, c1 = series[i - 1]
    if t1 == t or i == len(series):
        return c1
    t2, c2 = series[i]
    if t2 - t1 > max_gap and c2 - c1 > 0.015:
        return None
    return c1 + (c2 - c1) * ((t - t1) / (t2 - t1))


def energy_by_period(series, start, end, step):
    """{period_start_utc: kWh} for every whole period of `step` between start
    and end that the history covers."""
    out = {}
    t = start
    while t + step <= end:
        a, b = counter_at(series, t), counter_at(series, t + step)
        if a is not None and b is not None:
            out[t] = round(max(0.0, b - a), 4)
        t += step
    return out


def backfill_from_counter(state, series, start, end):
    """Rebuild every half-hour from start to end from the counter's history:
    queued for Autho Solar (a re-sent half-hour replaces the old figure) and
    today's (unscored) half-hours replaced for scoring. Returns how many."""
    halves = energy_by_period(series, slot_start(start) + SLOT, slot_start(end), SLOT)
    for t, kwh in sorted(halves.items()):
        key = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        state["queue"].append({"period_start": key, "kwh": kwh})
        if key[:10] in state["actuals"]:
            state["actuals"][key[:10]][key] = kwh
    state["queue"] = state["queue"][-2000:]
    return len(halves)


def take_snapshot(state, now, curves):
    """Once an evening (after SNAPSHOT_HOUR_UTC), store tomorrow's forecast
    from every source: {source: {slot_start_utc: kWh}}. Returns True if taken."""
    now = now.astimezone(timezone.utc)
    if now.hour < SNAPSHOT_HOUR_UTC:
        return False
    day = (now + timedelta(days=1)).date().isoformat()
    if day in state["snapshots"]:
        return False
    snap = {}
    for source, curve in curves.items():
        if not curve:
            continue
        vals = {slot_key(t): round(v, 4) for t, v in curve.items()
                if t.astimezone(timezone.utc).date().isoformat() == day}
        if vals:
            snap[source] = vals
    if not snap:
        return False
    state["snapshots"][day] = snap
    return True


def score_day(snapshot, actuals):
    """One day's result: actual kWh, and per source its forecast kWh, the
    day error %, and two half-hour by half-hour figures over daylight slots:
    the mean miss in kWh, and all the misses added up as a % of the day's
    generation (catches a forecast with the right total in the wrong hours).
    For Autho Solar, also how many daylight half-hours landed inside its range."""
    act_total = sum(actuals.values())
    row = {"actual_kwh": round(act_total, 2), "halfhours": len(actuals)}
    for source in SOURCES:
        fc = snapshot.get(source)
        if not fc:
            continue
        slots = [k for k in actuals if fc.get(k, 0) > DAYLIGHT_KWH or actuals[k] > DAYLIGHT_KWH]
        fc_total = sum(fc.get(k, 0.0) for k in actuals)
        miss = sum(abs(fc.get(k, 0.0) - actuals[k]) for k in slots)
        row[source] = {
            "kwh": round(fc_total, 2),
            "error_pct": round((fc_total - act_total) / act_total * 100, 1) if act_total > 0.2 else None,
            "slot_mae_kwh": round(miss / len(slots), 3) if slots else None,
            "slot_error_pct": round(miss / act_total * 100, 1) if act_total > 0.2 and slots else None,
        }
    lo, hi = snapshot.get("autho_p10"), snapshot.get("autho_p90")
    if lo and hi:
        slots = [k for k in actuals if hi.get(k, 0) > DAYLIGHT_KWH or actuals[k] > DAYLIGHT_KWH]
        inside = sum(1 for k in slots if lo.get(k, 0.0) - RANGE_SLACK_KWH <= actuals[k] <= hi.get(k, 0.0) + RANGE_SLACK_KWH)
        row["autho_range_slots"] = {"inside": inside, "of": len(slots)}
    return row


def day_detail(snapshot, actuals):
    """A day half-hour by half-hour, daylight only, for the dashboard chart:
    {"actual": {slot: kWh}, source: {slot: kWh}, ...}."""
    snapshot = snapshot or {}
    slots = sorted(set(actuals or {}) | {k for fc in snapshot.values() for k in fc})
    keep = [k for k in slots if (actuals or {}).get(k, 0) > DAYLIGHT_KWH
            or any(fc.get(k, 0) > DAYLIGHT_KWH for fc in snapshot.values())]
    out = {"actual": {k: actuals[k] for k in keep if k in (actuals or {})}}
    for source, fc in snapshot.items():
        out[source] = {k: fc[k] for k in keep if k in fc}
    return out


def roll_days(state, now):
    """Score every finished day that has both a snapshot and readings, move
    it into history, and drop old data."""
    today = now.astimezone(timezone.utc).date().isoformat()
    for day in sorted(set(state["actuals"]) | set(state["snapshots"])):
        if day >= today:
            continue
        snap, acts = state["snapshots"].get(day), state["actuals"].get(day)
        if snap and acts and len(acts) >= 40:
            state["history"] = [h for h in state["history"] if h.get("date") != day]
            state["history"].append({"date": day, **score_day(snap, acts)})
            state.setdefault("detail", {})[day] = day_detail(snap, acts)
        state["snapshots"].pop(day, None)
        state["actuals"].pop(day, None)
    state["history"] = sorted(state["history"], key=lambda h: h["date"])[-HISTORY_DAYS:]
    detail = state.setdefault("detail", {})
    for day in sorted(detail)[:-DETAIL_DAYS]:
        detail.pop(day, None)


def summarise(history):
    """Per source over all scored days: typical day error (median of the
    absolute %), mean half-hourly error, and how many days it was the
    closest of the three main forecasts."""
    out = {}
    main = ("solcast_raw", "solcast_corrected", "autho_p50")
    wins = {s: 0 for s in main}
    for h in history:
        # Closest day total; a tie goes to the better half-hour by half-hour.
        errs = {s: (abs(h[s]["error_pct"]), h[s].get("slot_error_pct") or 0.0)
                for s in main if h.get(s) and h[s].get("error_pct") is not None}
        if len(errs) >= 2:
            wins[min(errs, key=errs.get)] += 1
    for source in SOURCES:
        rows = [h[source] for h in history if h.get(source)]
        pcts = sorted(abs(r["error_pct"]) for r in rows if r.get("error_pct") is not None)
        maes = [r["slot_mae_kwh"] for r in rows if r.get("slot_mae_kwh") is not None]
        slot_pcts = sorted(r["slot_error_pct"] for r in rows if r.get("slot_error_pct") is not None)
        if not rows:
            continue
        out[source] = {
            "days": len(rows),
            "median_abs_error_pct": round(pcts[len(pcts) // 2], 1) if pcts else None,
            "mean_slot_mae_kwh": round(sum(maes) / len(maes), 3) if maes else None,
            "median_slot_error_pct": round(slot_pcts[len(slot_pcts) // 2], 1) if slot_pcts else None,
            "closest_days": wins.get(source),
        }
    inside = [h for h in history if h.get("autho_p10") and h.get("autho_p90")]
    if inside:
        hits = sum(1 for h in inside if h["autho_p10"]["kwh"] <= h["actual_kwh"] <= h["autho_p90"]["kwh"])
        out["autho_range"] = {"days": len(inside), "inside": hits}
    ranged = [h["autho_range_slots"] for h in history if h.get("autho_range_slots")]
    if ranged:
        out["autho_range_slots"] = {"inside": sum(r["inside"] for r in ranged), "of": sum(r["of"] for r in ranged)}
    return out

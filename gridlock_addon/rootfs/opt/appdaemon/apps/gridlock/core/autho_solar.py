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
    return {"accum": None, "queue": [], "actuals": {}, "snapshots": {}, "history": []}


def clean_state(raw):
    s = empty_state()
    if isinstance(raw, dict):
        for k in ("queue", "history"):
            if isinstance(raw.get(k), list):
                s[k] = raw[k]
        for k in ("actuals", "snapshots"):
            if isinstance(raw.get(k), dict):
                s[k] = raw[k]
        if isinstance(raw.get("accum"), dict):
            s["accum"] = raw["accum"]
        if isinstance(raw.get("last_sample"), str):
            s["last_sample"] = raw["last_sample"]
    return s


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
            state["queue"].append({"period_start": acc["slot"], "kwh": kwh})
            state["queue"] = state["queue"][-2000:]
            day = acc["slot"][:10]
            state["actuals"].setdefault(day, {})[acc["slot"]] = kwh
            finished = True
        acc = None
    if acc is None:
        acc = {"slot": key, "kwh": 0.0, "seen_h": 0.0}
    if pv_kw is not None and 0 < dt_h <= 10 / 60:
        acc["kwh"] += max(0.0, pv_kw) * dt_h
        acc["seen_h"] += dt_h
    state["accum"] = acc
    return finished


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
    day error % and the mean half-hourly error (kWh) over daylight slots."""
    act_total = sum(actuals.values())
    row = {"actual_kwh": round(act_total, 2), "halfhours": len(actuals)}
    for source in SOURCES:
        fc = snapshot.get(source)
        if not fc:
            continue
        slots = [k for k in actuals if fc.get(k, 0) > 0.005 or actuals[k] > 0.005]
        fc_total = sum(fc.get(k, 0.0) for k in actuals)
        row[source] = {
            "kwh": round(fc_total, 2),
            "error_pct": round((fc_total - act_total) / act_total * 100, 1) if act_total > 0.2 else None,
            "slot_mae_kwh": round(sum(abs(fc.get(k, 0.0) - actuals[k]) for k in slots) / len(slots), 3) if slots else None,
        }
    return row


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
        state["snapshots"].pop(day, None)
        state["actuals"].pop(day, None)
    state["history"] = sorted(state["history"], key=lambda h: h["date"])[-HISTORY_DAYS:]


def summarise(history):
    """Per source over all scored days: typical day error (median of the
    absolute %), mean half-hourly error, and how many days it was the
    closest of the three main forecasts."""
    out = {}
    main = ("solcast_raw", "solcast_corrected", "autho_p50")
    wins = {s: 0 for s in main}
    for h in history:
        errs = {s: abs(h[s]["error_pct"]) for s in main if h.get(s) and h[s].get("error_pct") is not None}
        if len(errs) >= 2:
            wins[min(errs, key=errs.get)] += 1
    for source in SOURCES:
        rows = [h[source] for h in history if h.get(source)]
        pcts = sorted(abs(r["error_pct"]) for r in rows if r.get("error_pct") is not None)
        maes = [r["slot_mae_kwh"] for r in rows if r.get("slot_mae_kwh") is not None]
        if not rows:
            continue
        out[source] = {
            "days": len(rows),
            "median_abs_error_pct": round(pcts[len(pcts) // 2], 1) if pcts else None,
            "mean_slot_mae_kwh": round(sum(maes) / len(maes), 3) if maes else None,
            "closest_days": wins.get(source),
        }
    inside = [h for h in history if h.get("autho_p10") and h.get("autho_p90")]
    if inside:
        hits = sum(1 for h in inside if h["autho_p10"]["kwh"] <= h["actual_kwh"] <= h["autho_p90"]["kwh"])
        out["autho_range"] = {"days": len(inside), "inside": hits}
    return out

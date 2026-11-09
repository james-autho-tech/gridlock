"""Heat pump COP from the unit's own heat-out and electricity-in counters.

A run's COP can't be measured well tick by tick (the current sensors read
whole amps), but the unit's kWh counters, differenced over a day, can: the
heat counter checks out against hot water tank heat-ups. Every tick the
counters' increase since the last tick is credited to what the heat pump
was doing over that interval -- space heating, hot water, or neither -- so
each day ends up with heat and electricity per mode, plus how long each ran
and the outdoor and flow temperatures it ran at. Weeks, months and the
heating season are sums of days; the planner calibrates its COP model from
whole days (gridlock.py), and the dashboard's COP tab and share card read
summarise().

The counters step in whole kWh and lag a little, so one tick's split is
lumpy; a kWh that lands just after a run stops still belongs to that run.
Day totals are right, and a day with less than MIN_DAY_ELEC_KWH through it
is flagged as too small to read much into.

The unit's electricity meter usually covers the outdoor unit only, not the
indoor water pump or the controller's standby draw, which flatters its COP.
Those are added back as an allowance (pump_w while running, standby_w all
the time; editable) when figures are worked out, so changing them re-states
history rather than needing it recorded again.

Pure functions; gridlock.py feeds it, webui.py reads the published sensor.
"""
from datetime import date, datetime, timedelta

MODES = ("heating", "dhw", "other")
MIN_DAY_ELEC_KWH = 3.0          # below this a day's COP is mostly counter steps
RUN_TAIL = timedelta(minutes=30)  # a counter step this soon after a run ends is that run's
MAX_GAP = timedelta(minutes=20)   # longer than this between readings: credited to "other"
KEEP_DAYS = 400
SEASON_START_MONTH = 9

DEFAULT_SETTINGS = {"gas_p": 6.3, "boiler_eff": 0.9, "season_start_month": SEASON_START_MONTH,
                    "share_name": "", "pump_w": 70.0, "standby_w": 20.0}


def clean_settings(raw):
    """Dashboard settings for the COP tab, clamped."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)

    def num(key, lo, hi):
        try:
            v = float(raw.get(key))
        except (TypeError, ValueError):
            return
        if lo <= v <= hi:
            out[key] = v
    num("gas_p", 1.0, 50.0)
    num("boiler_eff", 0.5, 1.0)
    num("pump_w", 0.0, 500.0)
    num("standby_w", 0.0, 200.0)
    try:
        m = int(raw.get("season_start_month"))
        if 1 <= m <= 12:
            out["season_start_month"] = m
    except (TypeError, ValueError):
        pass
    name = raw.get("share_name")
    if isinstance(name, str):
        out["share_name"] = name.strip()[:40]
    return out


def empty_day():
    return {"heat": {m: 0.0 for m in MODES}, "elec": {m: 0.0 for m in MODES},
            "cost_p": {m: 0.0 for m in MODES}, "run_h": {"heating": 0.0, "dhw": 0.0},
            "out_h": {"heating": 0.0, "dhw": 0.0}, "flow_h": {"heating": 0.0, "dhw": 0.0},
            "out_all": 0.0, "hours": 0.0}


def clean_ledger(raw):
    """The saved ledger, whatever state the file is in."""
    out = {"last": None, "days": {}}
    if not isinstance(raw, dict):
        return out
    last = raw.get("last")
    if isinstance(last, dict) and isinstance(last.get("t"), str):
        try:
            datetime.fromisoformat(last["t"])
            out["last"] = {"t": last["t"], "heat": float(last["heat"]), "elec": float(last["elec"]),
                           "mode": last.get("mode") if last.get("mode") in MODES else "other",
                           "run_mode": last.get("run_mode") if last.get("run_mode") in ("heating", "dhw") else None,
                           "run_end": last.get("run_end") if isinstance(last.get("run_end"), str) else None}
        except (KeyError, TypeError, ValueError):
            out["last"] = None
    days = raw.get("days")
    if isinstance(days, dict):
        for d, v in days.items():
            if not isinstance(v, dict):
                continue
            try:
                date.fromisoformat(d)
            except (TypeError, ValueError):
                continue
            day = empty_day()
            for key, modes in (("heat", MODES), ("elec", MODES), ("cost_p", MODES),
                               ("run_h", ("heating", "dhw")), ("out_h", ("heating", "dhw")),
                               ("flow_h", ("heating", "dhw"))):
                sub = v.get(key) if isinstance(v.get(key), dict) else {}
                for m in modes:
                    try:
                        day[key][m] = float(sub.get(m) or 0.0)
                    except (TypeError, ValueError):
                        pass
            for key in ("out_all", "hours"):
                try:
                    day[key] = float(v.get(key) or 0.0)
                except (TypeError, ValueError):
                    pass
            out["days"][d] = day
    return out


def mode_of(state, compressor_hz=None):
    """"heating" / "dhw" / "other" from the unit's activity sensor text
    (e.g. Midea's "Heating" / "DHW" / "Idle"), falling back to whether
    the compressor is turning."""
    s = str(state or "").lower()
    if "dhw" in s or "hot water" in s or "tank" in s:
        return "dhw"
    if "heat" in s:
        return "heating"
    if s in ("", "unknown", "unavailable") and compressor_hz and compressor_hz > 5:
        return "heating"
    return "other"


def add_reading(ledger, now, heat_kwh, elec_kwh, mode, outdoor=None, flow=None, price_p=None):
    """One tick: the counters' increase since the last reading, credited to
    the mode the heat pump was in over that interval (`mode` is now's, the
    previous reading's mode covers the interval just gone). now: an aware
    local datetime. price_p: what a kWh of electricity costs right now."""
    if heat_kwh is None or elec_kwh is None:
        return False
    mode = mode if mode in MODES else "other"
    last = ledger.get("last")
    ledger["last"] = {"t": now.isoformat(), "heat": heat_kwh, "elec": elec_kwh, "mode": mode,
                      "run_mode": (last or {}).get("run_mode"), "run_end": (last or {}).get("run_end")}
    if mode in ("heating", "dhw"):
        ledger["last"]["run_mode"], ledger["last"]["run_end"] = mode, None
    elif last and last.get("mode") in ("heating", "dhw"):
        ledger["last"]["run_end"] = now.isoformat()
    if not last:
        return False
    try:
        t0 = datetime.fromisoformat(last["t"])
    except (TypeError, ValueError):
        return False
    dt = now - t0
    if dt <= timedelta(0):
        return False
    d_heat, d_elec = heat_kwh - last["heat"], elec_kwh - last["elec"]
    if d_heat < 0 or d_elec < 0 or d_heat > 50 or d_elec > 20:
        return False  # counter reset or glitch: re-based above, nothing credited
    span = last.get("mode") or "other"
    if dt > MAX_GAP:
        span = "other"
    elif span == "other" and last.get("run_mode") and last.get("run_end"):
        try:
            if now - datetime.fromisoformat(last["run_end"]) <= RUN_TAIL:
                span = last["run_mode"]
        except (TypeError, ValueError):
            pass
    day = ledger["days"].setdefault(t0.date().isoformat(), empty_day())
    day["heat"][span] += d_heat
    day["elec"][span] += d_elec
    if price_p is not None:
        day["cost_p"][span] += d_elec * price_p
    hours = min(dt, MAX_GAP).total_seconds() / 3600.0
    day["hours"] += hours
    if outdoor is not None:
        day["out_all"] += outdoor * hours
    if last.get("mode") in ("heating", "dhw") and dt <= MAX_GAP:
        m = last["mode"]
        day["run_h"][m] += hours
        if outdoor is not None:
            day["out_h"][m] += outdoor * hours
        if flow is not None:
            day["flow_h"][m] += flow * hours
    for old in sorted(ledger["days"])[:-KEEP_DAYS]:
        ledger["days"].pop(old, None)
    return True


def _extra_kwh(day, mode, cfg):
    """Electricity the unit's meter doesn't see: the indoor pump while that
    mode ran, and standby over the time it ran (mode) or the rest of the day
    ("other")."""
    if mode in ("heating", "dhw"):
        return day["run_h"][mode] * (cfg["pump_w"] + cfg["standby_w"]) / 1000.0
    idle_h = max(0.0, day["hours"] - day["run_h"]["heating"] - day["run_h"]["dhw"])
    return idle_h * cfg["standby_w"] / 1000.0


def _elec(day, mode, cfg):
    return day["elec"][mode] + _extra_kwh(day, mode, cfg)


MIN_RATIO_ELEC_KWH = 1.0   # less electricity than this: no COP worth showing


def _ratio(a, b):
    return round(a / b, 2) if b and b >= MIN_RATIO_ELEC_KWH and a >= 0 else None


def day_summary(d, day, cfg=None):
    """One day, flattened for the dashboard (electricity includes the allowance)."""
    cfg = clean_settings(cfg)
    heat = sum(day["heat"].values())
    elec = sum(_elec(day, m, cfg) for m in MODES)
    # Heat and electricity after a run (the counters' lag) is in the run's own
    # mode already; "other" left is standby: in the total, not in either mode.
    out = {"date": d, "heat_kwh": round(heat, 1), "elec_kwh": round(elec, 1), "cop": _ratio(heat, elec),
           "ok": elec >= MIN_DAY_ELEC_KWH,
           "outdoor": round(day["out_all"] / day["hours"], 1) if day["hours"] else None,
           "cost_p": round(sum(day["cost_p"].values()), 1) if sum(day["cost_p"].values()) > 0 else None}
    for m in ("heating", "dhw"):
        h, e, run_h = day["heat"][m], _elec(day, m, cfg), day["run_h"][m]
        out[m] = {"heat_kwh": round(h, 1), "elec_kwh": round(e, 1), "cop": _ratio(h, e),
                  "hours": round(run_h, 2),
                  "outdoor": round(day["out_h"][m] / run_h, 1) if run_h else None,
                  "flow": round(day["flow_h"][m] / run_h, 1) if run_h else None}
    return out


def season_start(today, month=SEASON_START_MONTH):
    """The first day of the heating season today falls in."""
    start = date(today.year, month, 1)
    return start if today >= start else date(today.year - 1, month, 1)


def _period(days, keys, cfg):
    tot = {"heat": 0.0, "elec": 0.0, "cost_p": 0.0, "priced_heat": 0.0, "days": 0,
           "heating": [0.0, 0.0], "dhw": [0.0, 0.0]}
    for k in keys:
        day = days.get(k)
        if not day:
            continue
        tot["days"] += 1
        tot["heat"] += sum(day["heat"].values())
        tot["elec"] += sum(_elec(day, m, cfg) for m in MODES)
        cost = sum(day["cost_p"].values())
        if cost > 0:   # days rebuilt from history have no prices
            tot["cost_p"] += cost
            tot["priced_heat"] += sum(day["heat"].values())
        for m in ("heating", "dhw"):
            tot[m][0] += day["heat"][m]
            tot[m][1] += _elec(day, m, cfg)
    return {"days": tot["days"], "heat_kwh": round(tot["heat"], 1), "elec_kwh": round(tot["elec"], 1),
            "cop": _ratio(tot["heat"], tot["elec"]),
            "heating_cop": _ratio(*tot["heating"]), "dhw_cop": _ratio(*tot["dhw"]),
            "heating_heat_kwh": round(tot["heating"][0], 1), "dhw_heat_kwh": round(tot["dhw"][0], 1),
            "cost_p": round(tot["cost_p"], 1),
            "p_per_kwh_heat": round(tot["cost_p"] / tot["priced_heat"], 2) if tot["priced_heat"] > 0 else None}


def summarise(ledger, today, settings=None, show_days=60):
    """Everything the COP tab and share card need. today: local date."""
    cfg = clean_settings(settings)
    days = ledger.get("days") or {}
    keys = sorted(days)

    def since(first):
        return [k for k in keys if first.isoformat() <= k <= today.isoformat()]
    start = season_start(today, cfg["season_start_month"])
    periods = {"week": _period(days, since(today - timedelta(days=6)), cfg),
               "month": _period(days, since(today - timedelta(days=29)), cfg),
               "season": _period(days, since(start), cfg)}
    gas_per_kwh_heat = cfg["gas_p"] / cfg["boiler_eff"]
    for p in periods.values():
        p["gas_p_per_kwh_heat"] = round(gas_per_kwh_heat, 2)
        p["gas_cost_p"] = round(p["heat_kwh"] * gas_per_kwh_heat, 1)
    return {"season_start": start.isoformat(), "periods": periods,
            "days": [day_summary(k, days[k], cfg) for k in (keys[-show_days:] if show_days > 0 else [])],
            "first_day": keys[0] if keys else None, "settings": cfg}


def pooled_mode(ledger, mode, before, settings=None, days=7):
    """`mode`'s heat and electricity (with allowance) over the last `days`
    complete days before `before`, pooled -- one day's split is too lumpy
    (whole-kWh counter steps) to calibrate from. -> (cop, avg flow, avg
    outdoor, kWh electricity) or None when there's too little to go on."""
    cfg = clean_settings(settings)
    keys = [k for k in sorted(ledger.get("days") or {}) if k < before.isoformat()][-days:]
    heat = elec = run_h = flow_h = out_h = 0.0
    for k in keys:
        day = ledger["days"][k]
        heat += day["heat"][mode]
        elec += _elec(day, mode, cfg)
        run_h += day["run_h"][mode]
        flow_h += day["flow_h"][mode]
        out_h += day["out_h"][mode]
    if elec < (2 * MIN_DAY_ELEC_KWH if mode == "heating" else MIN_DAY_ELEC_KWH) or run_h < 1.0 or heat <= 0:
        return None
    return heat / elec, flow_h / run_h, out_h / run_h, elec

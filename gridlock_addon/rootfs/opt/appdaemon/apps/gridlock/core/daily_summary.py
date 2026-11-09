"""The evening summary sent to the phone: how today went and what tonight's
plan is, in a few short lines. Pure; gridlock.py gathers the figures.
"""
import re

DEFAULT_SETTINGS = {"service": "", "alerts": True, "summary": True, "summary_time": "21:00"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SERVICE_RE = re.compile(r"^notify\.[a-z0-9_]+$")


def clean_settings(raw):
    """notify_settings.json from the dashboard, whatever state it's in."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)
    svc = str(raw.get("service") or "").strip()
    out["service"] = svc if _SERVICE_RE.match(svc) else ""
    for key in ("alerts", "summary"):
        if key in raw:
            out[key] = raw[key] not in (False, "false", "off", 0, "0")
    t = str(raw.get("summary_time") or "").strip()
    if re.match(r"^\d:[0-5]\d$", t):
        t = "0" + t
    if _TIME_RE.match(t):
        out["summary_time"] = t
    return out


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def plan_tonight(columns, rows, hours=14):
    """From the published plan table: the grid charge (first/last slot,
    kWh, the battery level it reaches), the car's kWh and any free or paid
    sessions, over the next `hours`."""
    if not columns or not rows:
        return {}
    col = {c: i for i, c in enumerate(columns)}

    def v(r, name):
        i = col.get(name)
        return _f(r[i]) if i is not None and i < len(r) else None
    n = min(len(rows), hours * 2)
    charge = [(r[col["slot"]], v(r, "charge_kwh") or 0.0, v(r, "soc_pct")) for r in rows[:n]]
    charging = [c for c in charge if c[1] > 0.05]
    out = {"soc_end": None}
    if charging:
        out["charge_from"] = str(charging[0][0]).split(" ")[-1]
        out["charge_to"] = str(charging[-1][0]).split(" ")[-1]
        out["charge_kwh"] = round(sum(c[1] for c in charging), 1)
        socs = [c[2] for c in charge if c[2] is not None]
        out["soc_peak"] = round(max(socs)) if socs else None
    ev = sum(v(r, "ev_kwh") or 0.0 for r in rows[:n])
    if ev > 0.5:
        out["ev_kwh"] = round(ev, 1)
    free = [r[col["slot"]] for r in rows[:n] if (v(r, "free_electricity") or 0) > 0.5]
    if free:
        out["free_from"] = str(free[0]).split(" ")[-1]
    session = [r[col["slot"]] for r in rows[:n] if (v(r, "saving_session") or 0) > 0.5]
    if session:
        out["session_at"] = str(session[0]).split(" ")[-1]
    return out


def compose(today, tonight, tomorrow):
    """-> (title, message). today: {cost_gbp, saved_gbp, solar_kwh, cop,
    heat_kwh, hp_elec_kwh}; tonight: plan_tonight() plus {dhw_from, dhw_to,
    dhw_temp, setback_temp, setback_from}; tomorrow: {solar_kwh}. Any
    missing figure is simply left out."""
    lines = []
    bits = []
    if _f(today.get("cost_gbp")) is not None:
        bits.append(f"£{_f(today['cost_gbp']):.2f} spent")
    if _f(today.get("saved_gbp")) is not None and _f(today["saved_gbp"]) > 0.005:
        bits.append(f"£{_f(today['saved_gbp']):.2f} saved")
    if _f(today.get("solar_kwh")) is not None:
        bits.append(f"solar {_f(today['solar_kwh']):.1f} kWh")
    if bits:
        lines.append("Today: " + ", ".join(bits) + ".")
    if _f(today.get("cop")) is not None:
        lines.append(f"Heat pump: COP {_f(today['cop']):.1f} ({_f(today.get('heat_kwh')) or 0:.0f} kWh of heat "
                     f"from {_f(today.get('hp_elec_kwh')) or 0:.1f} kWh).")
    night = []
    if tonight.get("charge_kwh"):
        peak = f" to {tonight['soc_peak']}%" if tonight.get("soc_peak") is not None else ""
        night.append(f"battery charges {tonight['charge_from']}-{tonight['charge_to']}{peak} "
                     f"(+{tonight['charge_kwh']:.1f} kWh)")
    else:
        night.append("no grid charge needed")
    if tonight.get("dhw_from"):
        t = f" to {tonight['dhw_temp']:.0f}°C" if _f(tonight.get("dhw_temp")) else ""
        night.append(f"hot water {tonight['dhw_from']}-{tonight['dhw_to']}{t}")
    if tonight.get("setback_temp") is not None and tonight.get("setback_from"):
        night.append(f"heating down to {_f(tonight['setback_temp']):.0f}°C from {tonight['setback_from']}")
    if tonight.get("ev_kwh"):
        night.append(f"car {tonight['ev_kwh']:.0f} kWh")
    if tonight.get("free_from"):
        night.append(f"free electricity from {tonight['free_from']}")
    if tonight.get("session_at"):
        night.append(f"saving session at {tonight['session_at']}")
    lines.append("Tonight: " + ", ".join(night) + ".")
    if _f(tomorrow.get("solar_kwh")) is not None:
        lines.append(f"Tomorrow: solar about {_f(tomorrow['solar_kwh']):.0f} kWh.")
    return "GridLock: today and tonight", "\n".join(lines)

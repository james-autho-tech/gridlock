#!/usr/bin/env python3
"""Minimal Ingress web UI for GridLock — a live status page served
straight out of HA state, styled to match dashboard.yaml."""

import http.server
import json
import os
import socketserver
import urllib.error
import urllib.request

SUPERVISOR_TOKEN = os.environ["SUPERVISOR_TOKEN"]
PORT = 8099


def ha_get_all_states():
    """One bulk call instead of one HTTP round-trip per entity —
    build_status() looks up 20+ entities per request; fetching them
    individually risked the whole page hanging on "Loading…" if any
    single one was slow, since each call carries its own timeout and
    they ran one after another."""
    req = urllib.request.Request(
        "http://supervisor/core/api/states",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            states = json.load(resp)
        return {s["entity_id"]: s for s in states}
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return {}


def as_float(state_obj, default=0.0):
    try:
        return float(state_obj["state"])
    except (TypeError, ValueError, KeyError):
        return default


def as_kw(state_obj, default=0.0):
    """abs value, normalised to kW if the sensor reports W."""
    v = abs(as_float(state_obj, default))
    unit = (state_obj or {}).get("attributes", {}).get("unit_of_measurement", "kW")
    return v / 1000 if str(unit).lower() == "w" else v


def is_on(state_obj):
    return bool(state_obj) and state_obj.get("state") == "on"


def denudge(value, default=0):
    """GridLock's own set_state() override nudges every literal 0 in an
    attributes dict to 1e-9 before publishing (a separate workaround for
    HA silently dropping true zero attribute values) — round that back
    to a clean 0 for display so a genuinely-zero count doesn't render as
    the literal string "1e-9"."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return 0 if abs(v) < 1e-6 else v


# Standard HA weather-integration condition strings -> emoji, so the
# header widget doesn't depend on any particular weather integration's
# own icon set (Met Office, OpenWeatherMap, etc. all report from this
# same fixed vocabulary).
WEATHER_ICONS = {
    "clear-night": "🌙", "cloudy": "☁️", "fog": "🌫️", "hail": "🌨️",
    "lightning": "⛈️", "lightning-rainy": "⛈️", "partlycloudy": "⛅",
    "pouring": "🌧️", "rainy": "🌦️", "snowy": "❄️", "snowy-rainy": "🌨️",
    "sunny": "☀️", "windy": "💨", "windy-variant": "💨", "exceptional": "⚠️",
}


def find_weather_entity(states):
    """Best-effort discovery of a weather.* entity for the header widget
    — no gridlock.py/HASensorRegistry involvement needed, webui.py
    already pulls the full state dump for its own purposes."""
    candidates = [eid for eid in states if eid.startswith("weather.")]
    live = [e for e in candidates
            if states[e].get("state") not in (None, "unknown", "unavailable")]
    pool = live or candidates
    if not pool:
        return None
    eid = pool[0]
    obj = states[eid]
    attrs = obj.get("attributes", {})
    condition = obj.get("state")
    return {
        "entity_id": eid,
        "name": attrs.get("friendly_name") or eid,
        "temp": attrs.get("temperature"),
        "unit": attrs.get("temperature_unit", "°C"),
        "condition": condition,
        "icon": WEATHER_ICONS.get(condition, "🌡️"),
    }


VALID_MODES = ("auto", "eco", "balanced", "max_profit")


def ha_call_service(domain_service, entity_id, **data):
    """POST to HA's services API — same auth/base URL as
    ha_get_all_states, just a write instead of a read. Used only for the
    mode-override segmented control; nothing else in this file writes
    to HA."""
    payload = json.dumps({"entity_id": entity_id, **data}).encode()
    req = urllib.request.Request(
        f"http://supervisor/core/api/services/{domain_service}",
        data=payload, method="POST",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def build_status():
    states = ha_get_all_states()

    def get(entity_id):
        return states.get(entity_id) if entity_id else None

    status = get("sensor.gridlock_status") or {}
    status_attrs = status.get("attributes", {})
    soc = get(status_attrs.get("soc_entity"))
    imp = get(status_attrs.get("import_rate_entity"))
    exp = get(status_attrs.get("export_rate_entity"))
    forecast = get("sensor.gridlock_soc_forecast") or {}
    target = get("sensor.gridlock_target_soc")
    compare = get("sensor.gridlock_tariff_compare") or {}
    net = get("sensor.gridlock_calculated_net_cost_today") or {}
    savings = get("sensor.gridlock_savings") or {}
    ev_dispatch = get("sensor.gridlock_ev_dispatch_kwh") or {}
    decision_log = get("sensor.gridlock_decision_log") or {}
    solar = get("sensor.gridlock_solar_forecast") or {}
    storm = get("sensor.gridlock_storm_status") or {}
    carbon = get("sensor.gridlock_carbon_intensity") or {}
    ssen = get("sensor.gridlock_ssen_local_outages") or {}
    saving_raw = get(status_attrs.get("saving_events_entity")) or {}
    saving_attrs = saving_raw.get("attributes", {})

    pv_kw = sum(as_kw(get(e))
                for e in (status_attrs.get("pv_power_entities") or []))
    battery_kw = as_kw(get(status_attrs.get("battery_power_entity")))
    grid_kw = as_kw(get(status_attrs.get("grid_power_entity")))
    home_kw = as_kw(get(status_attrs.get("load_power_entity")))
    ev_kw = as_kw(get(status_attrs.get("ev_power_entity")))
    flow = {
        "pv_kw": round(pv_kw, 2),
        "soc_pct": round(as_float(soc), 1),
        "battery_kw": round(battery_kw, 2),
        "grid_kw": round(grid_kw, 2),
        "home_kw": round(home_kw, 2),
        "ev_kw": round(ev_kw, 2),
        "pv_generating": is_on(get(status_attrs.get("pv_generating_entity"))),
        "importing": is_on(get(status_attrs.get("importing_entity"))),
        "exporting": is_on(get(status_attrs.get("exporting_entity"))),
        "battery_charging": is_on(get(status_attrs.get("battery_charging_entity"))),
        "battery_discharging": is_on(get(status_attrs.get("battery_discharging_entity"))),
        "ev_charging": is_on(get(status_attrs.get("ev_entity"))),
        "ev_protected": status.get("state") == "EV Protection",
    }

    return {
        "flow": flow,
        "soc": as_float(soc),
        "target": as_float(target),
        "import_p": as_float(imp) * 100,
        "export_p": as_float(exp) * 100,
        "state": status.get("state", "unknown"),
        "reason": status_attrs.get("reason") or "—",
        "plan_html": status_attrs.get("plan_html") or "",
        "plan_summary": forecast.get("attributes", {}).get("plan_summary") or "",
        "plan_cost_24h": forecast.get("attributes", {}).get("plan_cost_24h", 0),
        "plan_table": forecast.get("attributes", {}).get("plan_table") or {"columns": [], "rows": []},
        "net_today": net.get("state", "0.00"),
        "net_today_calc_import": net.get("attributes", {}).get("import_cost_calculated_today"),
        "net_today_calc_export": net.get("attributes", {}).get("export_value_calculated_today"),
        "savings_today": savings.get("attributes", {}).get("today"),
        "savings_week": savings.get("attributes", {}).get("week"),
        "savings_month": savings.get("attributes", {}).get("month"),
        "daily_cost_history": savings.get("attributes", {}).get("daily_cost_history") or [],
        "daily_savings_history": savings.get("attributes", {}).get("daily_savings_history") or [],
        "plan_accuracy": savings.get("attributes", {}).get("plan_accuracy"),
        "profile_comparison_history": savings.get("attributes", {}).get("profile_comparison_history") or [],
        "profile_comparison_totals": savings.get("attributes", {}).get("profile_comparison_totals") or {},
        "carbon_now": as_float(carbon, None),
        "carbon_index": carbon.get("attributes", {}).get("index"),
        "carbon_forecast_data": carbon.get("attributes", {}).get("forecast_data") or [],
        "best_tariff": compare.get("state", "—"),
        "compare_html": compare.get("attributes", {}).get("compare_html") or "",
        "compare_results": compare.get("attributes", {}).get("results") or [],
        "weather": find_weather_entity(states),
        "mode_active": status_attrs.get("battery_risk_profile") or "balanced",
        "mode_override": (get("input_select.gridlock_mode_override") or {}).get("state", "auto"),
        "cheap_rate_p": (status_attrs.get("cheap_rate_threshold") or 0.10) * 100,
        "ev_planned_kwh": ev_dispatch.get("state", "0.00"),
        "log_entries": list(reversed(decision_log.get("attributes", {}).get("entries") or [])),
        "solar_today_kwh": solar.get("attributes", {}).get("today_kwh", 0),
        "solar_tomorrow_kwh": solar.get("attributes", {}).get("tomorrow_kwh", 0),
        "inverter_temp": as_float(get(status_attrs.get("inverter_temp_entity")), None),
        "battery_temp": as_float(get(status_attrs.get("battery_temp_entity")), None),
        "battery_soh": as_float(get(status_attrs.get("battery_soh_entity")), None),
        "battery_risk_profile": status_attrs.get("battery_risk_profile") or "balanced",
        "battery_degradation_cost": status_attrs.get("battery_degradation_cost"),
        "export_degradation_cost": status_attrs.get("export_degradation_cost"),
        "thermal_derate": status_attrs.get("thermal_derate"),
        "solar_forecast_data": solar.get("attributes", {}).get("forecast_data") or [],
        "soc_forecast_data": forecast.get("attributes", {}).get("forecast_data") or [],
        "learned_load_profile": forecast.get("attributes", {}).get("learned_load_profile") or [],
        "storm_state": storm.get("state", "Clear"),
        "storm_reason": storm.get("attributes", {}).get("reason") or "No active alerts",
        "ssen_count": ssen.get("state", "0"),
        "ssen_planned": denudge(ssen.get("attributes", {}).get("planned", 0)),
        "ssen_severe": bool(ssen.get("attributes", {}).get("network_severe_weather")),
        "ssen_postcode": status_attrs.get("ssen_postcode"),
        "saving_joined": saving_attrs.get("joined_events") or [],
        "saving_available": saving_attrs.get("available_events") or [],
        "entities": {
            "Battery SoC": status_attrs.get("soc_entity"),
            "Import rate": status_attrs.get("import_rate_entity"),
            "Export rate": status_attrs.get("export_rate_entity"),
            "EV charging": status_attrs.get("ev_entity"),
            "IOG dispatch": status_attrs.get("dispatch_entity"),
            "Saving sessions": status_attrs.get("saving_events_entity"),
            "Daily import cost": status_attrs.get("daily_import_cost_entity"),
            "Daily export value": status_attrs.get("daily_export_value_entity"),
            "Daily standing charge": status_attrs.get("daily_standing_charge_entity"),
            "PV power": ", ".join(status_attrs.get("pv_power_entities") or []) or None,
            "Grid power": status_attrs.get("grid_power_entity"),
            "Battery power": status_attrs.get("battery_power_entity"),
            "Load power": status_attrs.get("load_power_entity"),
            "EV power": status_attrs.get("ev_power_entity"),
            "Inverter temp": status_attrs.get("inverter_temp_entity"),
            "Battery temp": status_attrs.get("battery_temp_entity"),
            "Battery SoH": status_attrs.get("battery_soh_entity"),
            "Discharge cutoff (hardware)": status_attrs.get("discharge_cutoff_entity"),
            "Storm Watch": ", ".join(status_attrs.get("storm_watch_entities") or []) or None,
            "SSEN postcode": status_attrs.get("ssen_postcode"),
        },
    }


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GridLock</title>
<style>
  :root { --bg:#0b1220; --panel:#111a2c; --line:#1e293b; --ink:#e2e8f0;
          --dim:#64748b; --cyan:#38bdf8; --green:#34d399; --amber:#fbbf24;
          --violet:#a78bfa; --red:#f87171; --crimson:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:system-ui,sans-serif; }
  .gl-nav { display:flex; gap:4px; padding:12px 20px; background:var(--panel);
            border-bottom:1px solid var(--line); position:sticky; top:0; z-index:10; }
  .gl-nav-btn { background:none; border:none; color:var(--dim); font-size:13px;
                font-weight:600; letter-spacing:.4px; padding:8px 16px; border-radius:8px;
                cursor:pointer; transition:color .15s, background .15s; font-family:inherit; }
  .gl-nav-btn:hover { color:var(--ink); background:rgba(255,255,255,.05); }
  .gl-nav-btn.active { color:var(--cyan); background:rgba(56,189,248,.12); }
  .tab-page { padding:20px; }
  .num { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
         font-variant-numeric:tabular-nums; }
  .gl, .gl-wrap { background:var(--panel); border:1px solid var(--line);
       border-radius:14px; padding:20px 22px; margin-bottom:16px; }
  .gl-eyebrow { display:flex; align-items:center; gap:6px; color:var(--cyan);
                font-size:11px; font-weight:600; letter-spacing:2.5px;
                text-transform:uppercase; margin-bottom:10px; }
  .gl-top { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  .gl-state { font-size:26px; font-weight:700; letter-spacing:.5px; }
  .gl-dot { width:10px; height:10px; border-radius:50%; display:inline-block;
            margin-right:8px; background:var(--green);
            box-shadow:0 0 8px var(--green); animation:glpulse 2s infinite; }
  @keyframes glpulse { 50% { opacity:.45 } }
  .gl-reason { color:var(--dim); font-size:14px; }
  .gl-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
             gap:12px; margin-top:18px; }
  .gl-tile { background:#0b1220; border:1px solid var(--line);
             border-radius:10px; padding:12px 14px; }
  .gl-tile .lbl { font-size:11px; text-transform:uppercase; letter-spacing:1.2px;
                  color:var(--dim); margin-bottom:6px; }
  .gl-tile .val { font-size:22px; font-weight:600; }
  .gl-bar { margin-top:16px; }
  .gl-bar .lbl { font-size:11px; text-transform:uppercase; letter-spacing:1.2px;
                 color:var(--dim); display:flex; justify-content:space-between; }
  .gl-track { height:10px; border-radius:6px; background:#0b1220;
              border:1px solid var(--line); margin-top:6px; position:relative;
              overflow:hidden; }
  .gl-fill { height:100%; border-radius:6px;
             background:linear-gradient(90deg,#0e7490,var(--green)); }
  .gl-target { position:absolute; top:-2px; bottom:-2px; width:2px;
               background:var(--amber); }
  .gl-h { font-size:12px; letter-spacing:1.6px; text-transform:uppercase;
          color:var(--dim); margin-bottom:10px; }
  .gl-scroll { max-height:520px; overflow-y:auto; }
  .gl-scroll-mini { max-height:230px; overflow-y:hidden; }
  .gl-more-btn { display:block; width:100%; margin-top:12px; padding:9px;
                 background:none; border:1px solid var(--line); border-radius:8px;
                 color:var(--cyan); font-size:12px; font-weight:600; letter-spacing:.3px;
                 cursor:pointer; font-family:inherit; transition:background .15s; }
  .gl-more-btn:hover { background:rgba(56,189,248,.08); }
  .gl-bars { display:flex; align-items:flex-end; gap:2px; height:150px;
             margin-top:6px; border-bottom:1px solid var(--line); }
  .gl-bar-col { flex:1; height:100%; display:flex; align-items:flex-end; min-width:2px; }
  .gl-bar-fill { width:100%; min-height:2px; border-radius:2px 2px 0 0;
                 background:linear-gradient(180deg,var(--amber),rgba(251,191,36,.15)); }
  .gl-bar-fill-soc { background:linear-gradient(180deg,var(--cyan),rgba(56,189,248,.15)); }
  .gl-bar-fill-load { background:linear-gradient(180deg,var(--violet),rgba(167,139,250,.15)); }
  /* Same flex/gap shape as .gl-bars/.gl-bar-col above so each labelled
     column lines up exactly under its own bar — empty columns in
     between just render blank, letting a label's text overflow into
     that free space either side rather than every column needing its
     own (unreadable, 4px-wide) label. */
  .gl-bar-axis { display:flex; gap:2px; margin-top:4px; }
  .gl-bar-axis-col { flex:1; min-width:2px; font-size:10px; color:var(--dim);
                      text-align:center; white-space:nowrap; overflow:visible; }
  .gl-sub { color:var(--dim); font-size:12px; margin:-4px 0 10px; }
  .gl-combo-svg { width:100%; height:220px; display:block; margin-top:6px; }
  .gl-combo-legend { display:flex; gap:18px; font-size:12px; color:var(--dim); margin-top:6px; }
  .gl-legend-dot { display:inline-block; width:9px; height:9px; border-radius:50%;
                    margin-right:6px; vertical-align:middle; }
  .gl-status-row { display:flex; align-items:center; gap:10px; }
  .gl-status-dot { width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
  .gl-sess-row { display:flex; justify-content:space-between; gap:12px; font-size:13px;
                 padding:7px 0; border-bottom:1px solid #14203a; }
  .gl-sess-row .code { color:var(--dim); font-size:11px; }
  .flow-wrap { display:flex; justify-content:center; padding:12px 0; }
  .flow-svg { width:100%; max-width:920px; height:auto; }
  .flow-line { fill:none; stroke:#1e293b; stroke-width:2; }
  .flow-line.active { stroke-width:3; filter:drop-shadow(0 0 3px currentColor); }
  .flow-dot { filter:drop-shadow(0 0 6px currentColor); }
  .flow-hub { fill:var(--line); }
  .flow-hub.active { fill:var(--ink); filter:drop-shadow(0 0 5px var(--ink)); }
  .flow-node circle.ring { fill:var(--panel); stroke:var(--line); stroke-width:1.5; }
  .flow-node.active circle.ring { stroke-width:2.5; animation:ringpulse 2.2s ease-in-out infinite; }
  @keyframes ringpulse {
    0%, 100% { filter:drop-shadow(0 0 2px currentColor); }
    50% { filter:drop-shadow(0 0 12px currentColor); }
  }
  .flow-node text.icon { font-size:24px; text-anchor:middle; dominant-baseline:central; }
  .flow-node text.label { font-size:10px; letter-spacing:1.5px; text-transform:uppercase;
                           fill:var(--dim); text-anchor:middle; }
  .flow-node text.val { font-size:14px; font-weight:700; text-anchor:middle;
                         font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .gl-ent-list { display:flex; flex-direction:column; gap:6px; }
  .gl-ent-row { display:flex; align-items:baseline; gap:8px; font-size:13px;
                padding:4px 0; border-bottom:1px solid #14203a; }
  .gl-ent-dot { width:7px; height:7px; border-radius:50%; flex:0 0 auto; }
  .gl-ent-label { color:var(--dim); flex:0 0 150px; }
  .gl-ent-id { color:#cbd5e1; font-size:12px; word-break:break-all; }
  .gl-log-list { display:flex; flex-direction:column; gap:2px; }
  .gl-log-row { display:flex; align-items:baseline; gap:12px; font-size:13px;
                padding:8px 0; border-bottom:1px solid #14203a; }
  .gl-log-dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto;
                margin-top:5px; }
  .gl-log-ts { color:var(--dim); flex:0 0 148px; font-size:12px; }
  .gl-log-state { flex:0 0 200px; font-weight:600; }
  .gl-log-reason { color:#cbd5e1; }
  table.gridlock-plan { width:100%; border-collapse:collapse; font-size:13px;
          font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  table.gridlock-plan th { position:sticky; top:0; background:var(--panel);
          color:var(--dim); font-size:11px; letter-spacing:1.2px;
          text-transform:uppercase; text-align:left; padding:8px 10px;
          border-bottom:1px solid #334155; }
  table.gridlock-plan td { padding:7px 10px; border-bottom:1px solid #14203a;
          color:#cbd5e1; }
  table.gridlock-plan tr:first-child td { color:var(--green); font-weight:700; }

  /* ---- header: mode segmented control + weather widget ---- */
  .gl-nav { flex-wrap:wrap; justify-content:space-between; row-gap:8px; }
  .gl-nav-tabs { display:flex; gap:4px; flex-wrap:wrap; }
  .gl-nav-right { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  .gl-segmented { display:flex; background:#0b1220; border:1px solid var(--line);
                  border-radius:10px; padding:3px; gap:2px; }
  .gl-seg-btn { background:none; border:none; color:var(--dim); font-size:11px;
                font-weight:700; letter-spacing:.6px; padding:7px 12px; border-radius:8px;
                cursor:pointer; font-family:inherit; transition:background .15s,color .15s; }
  .gl-seg-btn:hover { color:var(--ink); }
  .gl-seg-btn.active[data-mode="eco"] { background:rgba(52,211,153,.18); color:var(--green); }
  .gl-seg-btn.active[data-mode="balanced"] { background:rgba(56,189,248,.18); color:var(--cyan); }
  .gl-seg-btn.active[data-mode="max_profit"] { background:rgba(167,139,250,.18); color:var(--violet); }
  .gl-seg-btn:disabled { opacity:.5; cursor:wait; }
  .gl-weather { display:flex; align-items:center; gap:12px; font-size:12px; color:var(--dim); }
  .gl-weather .temp { font-size:15px; font-weight:700; color:var(--ink); }
  .gl-weather-icon { font-size:18px; }
  .gl-solar-pill, .gl-storm-pill { display:inline-flex; align-items:center; gap:5px;
                 padding:4px 10px; border-radius:999px; font-size:11px; font-weight:700;
                 letter-spacing:.3px; background:rgba(255,255,255,.05); }
  .gl-storm-pill.clear { color:var(--green); }
  .gl-storm-pill.active { color:var(--red); background:rgba(248,113,113,.15); animation:glpulse 1.6s infinite; }

  /* ---- action pill badges ---- */
  .gl-pill { display:inline-flex; align-items:center; gap:4px; padding:3px 10px;
             border-radius:999px; font-size:11px; font-weight:700; letter-spacing:.4px;
             white-space:nowrap; }
  .gl-pill-charge { background:rgba(52,211,153,.18); color:var(--green); }
  .gl-pill-export { background:rgba(56,189,248,.18); color:var(--cyan); }
  .gl-pill-eco    { background:rgba(251,191,36,.14); color:#d6a94f; }
  .gl-pill-storm  { background:rgba(220,38,38,.20); color:var(--crimson); }
  .gl-pill-bypass { background:rgba(251,146,60,.22); color:#fb923c;
                     animation:pillglow 1.8s ease-in-out infinite; }
  /* Deliberate, cost-optimal off-peak passthrough (battery held back on
     purpose because importing is cheaper than cycling it) — kept visually
     distinct from .gl-pill-bypass above, which means the battery genuinely
     had nothing left to give. Same word, deliberately different colour and
     no glow animation, so a routine cheap-import night never reads as a
     problem. */
  .gl-pill-bypass-planned { background:rgba(96,165,250,.18); color:#60a5fa; }
  @keyframes pillglow {
    0%, 100% { box-shadow:0 0 2px rgba(251,146,60,.4); }
    50% { box-shadow:0 0 10px rgba(251,146,60,.85); }
  }

  /* ---- plan table (client-rendered from plan_table data) ---- */
  .gl-table-scroll { overflow-x:auto; }
  table.gl-plan { width:100%; border-collapse:collapse; font-size:13px;
          font-family:ui-monospace,SFMono-Regular,Menlo,monospace; white-space:nowrap; }
  table.gl-plan th { position:sticky; top:0; background:var(--panel); z-index:1;
          color:var(--dim); font-size:11px; letter-spacing:1.2px;
          text-transform:uppercase; text-align:left; padding:8px 10px;
          border-bottom:1px solid #334155; }
  table.gl-plan td { padding:7px 10px; border-bottom:1px solid #14203a; color:#cbd5e1; }
  table.gl-plan tr.is-now td { color:var(--green); font-weight:700; }
  table.gl-plan tr.is-bypass { background:rgba(251,146,60,.10); }
  table.gl-plan tr:hover { background:rgba(255,255,255,.03); }
  .gl-soc-mini { display:flex; align-items:center; gap:6px; }
  .gl-soc-mini-track { width:56px; height:7px; border-radius:4px; background:#0b1220;
                        border:1px solid var(--line); overflow:hidden; flex:0 0 auto; }
  .gl-soc-mini-fill { height:100%; border-radius:4px; background:linear-gradient(90deg,#0e7490,var(--green)); }

  /* ---- overview bypass banner + flow glow ---- */
  .gl-bypass-banner { display:flex; align-items:center; gap:8px; padding:10px 14px;
                       border-radius:10px; background:rgba(251,146,60,.14);
                       border:1px solid rgba(251,146,60,.4); color:#fb923c;
                       font-weight:700; font-size:13px; margin-bottom:14px;
                       animation:pillglow 1.8s ease-in-out infinite; }
  .flow-line.bypass { stroke:#fb923c !important; animation:flowwarn 1.3s ease-in-out infinite; }
  @keyframes flowwarn { 0%, 100% { opacity:1 } 50% { opacity:.35 } }

  /* ---- KPI sparklines + price coloring ---- */
  .gl-tile { position:relative; overflow:hidden; }
  .gl-spark { display:block; margin-top:6px; width:100%; height:22px; }
  .gl-tile .val.price-cheap { color:var(--green); }
  .gl-tile .val.price-mid { color:var(--amber); }
  .gl-tile .val.price-peak { color:var(--red); }

  /* ---- forecast: 3 stacked synced charts ---- */
  .gl-triad { position:relative; }
  .gl-triad-chart { display:block; width:100%; height:120px; margin-bottom:2px; cursor:crosshair; }
  .gl-triad-guide { stroke:var(--ink); stroke-width:1; opacity:0; pointer-events:none; }
  .gl-triad-label { position:absolute; left:0; top:0; font-size:10px; letter-spacing:1px;
                     text-transform:uppercase; color:var(--dim); pointer-events:none; }
  .gl-triad-tooltip { position:absolute; top:4px; transform:translateX(-50%);
                       background:#0b1220; border:1px solid var(--line); border-radius:8px;
                       padding:8px 10px; font-size:11px; color:#cbd5e1; white-space:nowrap;
                       pointer-events:none; display:none; z-index:5; box-shadow:0 4px 16px rgba(0,0,0,.4); }
  .gl-triad-tooltip b { color:var(--ink); }

  /* ---- tariff bar visualizer ---- */
  .gl-tariff-row { display:grid; grid-template-columns:120px 1fr 70px; align-items:center;
                    gap:10px; font-size:13px; padding:8px 0; }
  .gl-tariff-track { height:16px; border-radius:5px; background:#0b1220;
                      border:1px solid var(--line); overflow:hidden; }
  .gl-tariff-fill { height:100%; border-radius:5px; }
  .gl-tariff-row.is-best .gl-tariff-fill { background:linear-gradient(90deg,#0e7490,var(--green)); }
  .gl-tariff-row:not(.is-best) .gl-tariff-fill { background:linear-gradient(90deg,#7c2d12,var(--amber)); }
  .gl-tariff-row.is-active .gl-name { color:var(--cyan); font-weight:700; }

  /* ---- entity discovery cards ---- */
  .gl-ent-cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px; }
  .gl-ent-card { background:#0b1220; border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .gl-ent-card-h { display:flex; align-items:center; gap:8px; font-size:12px; font-weight:700;
                   letter-spacing:1px; text-transform:uppercase; color:var(--ink); margin-bottom:10px; }
  .gl-ent-card .gl-ent-row { border-bottom:1px solid #14203a; }
  .gl-ent-card .gl-ent-row:last-child { border-bottom:none; }

  /* ---- log severity ---- */
  .gl-log-row.is-warn { background:rgba(248,113,113,.08); border-radius:8px;
                         padding-left:8px; margin:0 -8px; }

  @media (max-width:640px) {
    .gl-nav-right { width:100%; justify-content:space-between; }
    .gl-log-ts { flex-basis:100px; }
    .gl-log-state { flex-basis:140px; }
    .gl-tariff-row { grid-template-columns:90px 1fr 60px; font-size:12px; }
  }
</style>
</head>
<body>
<nav class="gl-nav">
  <div class="gl-nav-tabs">
    <button class="gl-nav-btn" data-tab="overview">Overview</button>
    <button class="gl-nav-btn" data-tab="plan">Plan</button>
    <button class="gl-nav-btn" data-tab="forecast">Forecast</button>
    <button class="gl-nav-btn" data-tab="tariffs">Tariffs</button>
    <button class="gl-nav-btn" data-tab="entities">Entities</button>
    <button class="gl-nav-btn" data-tab="log">Log</button>
  </div>
  <div class="gl-nav-right" id="gl-nav-right"></div>
</nav>
<div id="app">Loading…</div>
<script>
let currentTab = 'overview';
let latestPlanTable = { columns: [], rows: [] };
function csvCell(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
// Field name -> friendly CSV header, in the exact order gridlock.py's
// plan_table.columns lists them — driven by that list rather than a
// separately hand-maintained one, so the two can't drift apart.
const PLAN_CSV_HEADERS = {
  slot: 'Slot', import_p: 'Import (p)', export_p: 'Export (p)', pv_kwh: 'PV (kWh)',
  load_kwh: 'Load (kWh)', grid_kwh: 'Grid (kWh)', charge_kwh: 'Charge (kWh)',
  battery_kwh: 'Battery (kWh)',
  action: 'Action', ev_kwh: 'EV (kWh)', dispatch: 'EV dispatch slot',
  saving_session: 'Saving session', power_up_session: 'Power Up session',
  session_reward_p: 'Session reward (p)', session_baseline_kwh: 'Session baseline (kWh)',
  session_export_baseline_kwh: 'Session export baseline (kWh)',
  soc_pct: 'SoC (%)',
  cost_delta_p: 'Grid cost delta (p)', total_gbp: 'Grid total (£)',
  import_rank: 'Import rank', export_rank: 'Export rank',
};
function downloadPlanCsv() {
  const cols = latestPlanTable.columns || [];
  const rows = latestPlanTable.rows || [];
  if (!rows.length) return;
  const header = cols.map(c => PLAN_CSV_HEADERS[c] || c);
  const lines = [header.map(csvCell).join(',')];
  rows.forEach(row => lines.push(row.map(csvCell).join(',')));
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `gridlock-plan-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
function selectTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.gl-nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-page').forEach(p => p.style.display = (p.dataset.tab === tab) ? '' : 'none');
}
document.querySelectorAll('.gl-nav-btn').forEach(b => b.addEventListener('click', () => selectTab(b.dataset.tab)));
function dotColor(state) {
  if (state.includes('Storm')) return 'var(--red)';
  if (state.includes('Bypass')) return 'var(--amber)';
  if (state.includes('Charg')) return 'var(--green)';
  if (state.includes('Export') || state.includes('Session')) return 'var(--cyan)';
  if (['Disabled','unavailable','unknown'].includes(state)) return 'var(--red)';
  return 'var(--violet)';
}
function esc(s) {
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
// "Forced Bypass" only — the battery genuinely had nothing left to give
// and grid had to step in. Deliberately narrower than a bare "Bypass"
// substring match: gridlock.py also uses the plain label "Bypass" for the
// opposite, cost-optimal case (battery held back on purpose because
// importing is cheaper than cycling it), which must NOT trigger this
// warning treatment.
function isBypass(state) {
  return String(state || '').includes('Forced Bypass');
}
function isPlannedBypass(state) {
  return String(state || '').trim() === 'Bypass';
}
function isStormHold(state) {
  return String(state || '').includes('Storm');
}
// Action string (from plan_table, or the live row-0 label) -> pill badge.
const PLANNED_BYPASS_TITLE = "Grid imports straight through to load this slot — the battery is deliberately left alone because importing fresh at this cheap/off-peak rate is cheaper than cycling stored charge to avoid it. No extra wear, no loss: this is the plan working as intended, not a fallback.";
const FORCED_BYPASS_TITLE = "Grid had to cover load directly because the battery was already at its floor SoC with no solar to help — a genuine shortfall, not a planning choice. Frequent occurrences may mean the reserve target or an earlier charge slot needs adjusting.";
function actionPill(action) {
  const a = String(action || '');
  if (isBypass(a)) return `<span class="gl-pill gl-pill-bypass" title="${FORCED_BYPASS_TITLE}">⚠️ BYPASS</span>`;
  if (isPlannedBypass(a)) return `<span class="gl-pill gl-pill-bypass-planned" title="${PLANNED_BYPASS_TITLE}">BYPASS</span>`;
  if (isStormHold(a)) return `<span class="gl-pill gl-pill-storm">🔴 STORM_HOLD</span>`;
  if (a.includes('CHARGE') || a.includes('Charg')) return `<span class="gl-pill gl-pill-charge">🟢 CHARGE</span>`;
  if (a.includes('EXPORT') || a.includes('Export') || a.includes('Session')) return `<span class="gl-pill gl-pill-export">🔵 EXPORT</span>`;
  return `<span class="gl-pill gl-pill-eco">🟡 ${esc(a || 'ECO')}</span>`;
}
let modeSwitchBusy = false;
async function setMode(mode) {
  if (modeSwitchBusy) return;
  modeSwitchBusy = true;
  document.querySelectorAll('.gl-seg-btn').forEach(b => b.disabled = true);
  try {
    await fetch('api/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
  } catch (e) { /* surfaced on next refresh() if the engine never picks it up */ }
  setTimeout(() => { modeSwitchBusy = false; refresh(); }, 1500);
}
function renderHeaderRight(d) {
  const modes = [['eco', 'ECO'], ['balanced', 'BALANCED'], ['max_profit', 'MAX PROFIT']];
  const active = d.mode_override && d.mode_override !== 'auto' ? d.mode_override : d.mode_active;
  const seg = modes.map(([val, label]) =>
    `<button class="gl-seg-btn${val === active ? ' active' : ''}" data-mode="${val}" onclick="setMode('${val}')">${label}</button>`
  ).join('');
  const w = d.weather;
  const weatherHtml = w
    ? `<span class="gl-weather-icon">${esc(w.icon)}</span><span class="temp">${w.temp === null || w.temp === undefined ? '—' : Math.round(w.temp) + (w.unit || '°C')}</span><span>${esc(w.name)}</span>`
    : `<span style="color:var(--dim)">No weather entity found</span>`;
  const solarBand = Number(d.solar_today_kwh) >= 20 ? 'High' : Number(d.solar_today_kwh) >= 8 ? 'Medium' : 'Low';
  const stormActive = d.storm_state === 'Active';
  document.getElementById('gl-nav-right').innerHTML = `
    <div class="gl-segmented">${seg}</div>
    <div class="gl-weather">${weatherHtml}</div>
    <span class="gl-solar-pill" style="color:var(--amber)">☀️ Solar: ${Number(d.solar_today_kwh).toFixed(1)} kWh (${solarBand})</span>
    <span class="gl-storm-pill ${stormActive ? 'active' : 'clear'}" title="${esc(d.storm_reason)}">${stormActive ? '🌩️ Storm Watch: Active' : '🟢 Storm Watch: Clear'}</span>
  `;
}
function priceClass(pence, cheapP) {
  if (pence <= cheapP) return 'price-cheap';
  if (pence >= cheapP * 2.5) return 'price-peak';
  return 'price-mid';
}
// plan_table is columns+rows (array-of-arrays, not array-of-objects —
// see gridlock.py's publish_plan for why) — this reads one named
// column out as a plain array, everywhere the JS needs plan data.
function planCol(table, name) {
  if (!table || !table.columns || !table.rows) return [];
  const idx = table.columns.indexOf(name);
  if (idx < 0) return [];
  return table.rows.map(r => r[idx]);
}
function sparkline(values, color) {
  const vals = (values || []).map(Number).filter(v => !isNaN(v));
  if (vals.length < 2) return '';
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  const W = 100, H = 22;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1) * W).toFixed(1)},${(H - ((v - min) / range) * (H - 3) - 1.5).toFixed(1)}`).join(' ');
  return `<svg class="gl-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
  </svg>`;
}
function renderTempTile(label, value, amberAt, redAt) {
  if (value === null || value === undefined) {
    return `<div class="gl-tile"><div class="lbl">${esc(label)}</div><div class="val" style="color:var(--dim);font-size:14px">not found</div></div>`;
  }
  const color = value >= redAt ? 'var(--red)' : value >= amberAt ? 'var(--amber)' : 'var(--green)';
  return `<div class="gl-tile"><div class="lbl">${esc(label)}</div><div class="val num" style="color:${color}">${Number(value).toFixed(1)}°C</div></div>`;
}
function renderSohTile(value) {
  if (value === null || value === undefined) {
    return '<div class="gl-tile"><div class="lbl">State of health</div><div class="val" style="color:var(--dim);font-size:14px">not found</div></div>';
  }
  const color = value >= 90 ? 'var(--green)' : value >= 80 ? 'var(--amber)' : 'var(--red)';
  return `<div class="gl-tile"><div class="lbl">State of health</div><div class="val num" style="color:${color}">${Number(value).toFixed(1)}%</div></div>`;
}
function renderFlow(f, bypassActive) {
  if (!f) return '';
  // Everything routes through a virtual centre hub — keeps 5
  // independent flows (solar/grid/battery/home/ev) simple to animate
  // without modelling every real physical wiring path.
  const CX = 210, CY = 195;
  const socPct = typeof f.soc_pct === 'number' ? Math.round(f.soc_pct) : null;
  const nodes = {
    solar:   { x: 210, y: 55,  icon: '☀️', label: 'Solar',   val: `${f.pv_kw.toFixed(2)} kW`,
               color: 'var(--amber)', active: f.pv_generating, glow: f.pv_kw > 2 },
    grid:    { x: 55,  y: 195, icon: '⚡',  label: 'Grid',    val: `${f.grid_kw.toFixed(2)} kW`,
               color: bypassActive ? '#fb923c' : (f.exporting ? 'var(--cyan)' : 'var(--amber)'),
               active: f.importing || f.exporting || bypassActive },
    ev:      { x: 365, y: 115, icon: '🚗', label: 'EV',      val: `${f.ev_kw.toFixed(2)} kW`,
               color: f.ev_protected ? 'var(--violet)' : 'var(--green)',
               active: f.ev_charging, badge: f.ev_protected ? '🛡️' : null },
    home:    { x: 365, y: 275, icon: '🏠', label: 'Home',    val: `${f.home_kw.toFixed(2)} kW`,
               color: bypassActive ? '#fb923c' : 'var(--violet)',
               active: f.home_kw > 0.05 || bypassActive },
    battery: { x: 210, y: 335, icon: '🔋', label: 'Battery',
               val: socPct === null ? `${f.battery_kw.toFixed(2)} kW` : `${socPct}% · ${f.battery_kw.toFixed(2)}kW`,
               color: f.battery_charging ? 'var(--green)' : 'var(--cyan)',
               active: f.battery_charging || f.battery_discharging,
               glow: f.battery_charging },
  };
  // [outward, dotColor] — outward = node -> hub; false = hub -> node
  const dirs = {
    solar: [true, 'var(--amber)'],
    grid: [f.importing || bypassActive, bypassActive ? '#fb923c' : (f.exporting ? 'var(--cyan)' : 'var(--amber)')],
    ev: [false, f.ev_protected ? 'var(--violet)' : 'var(--green)'],
    home: [bypassActive ? true : false, bypassActive ? '#fb923c' : 'var(--violet)'],
    battery: [f.battery_discharging, f.battery_charging ? 'var(--green)' : 'var(--cyan)'],
  };
  // Sankey-style: line thickness scales with that flow's actual kW,
  // relative to whichever flow is biggest right now — thin lines mean
  // "barely anything moving here", thick means "this is where most of
  // the power's going", at a glance rather than reading five numbers.
  const mags = { solar: f.pv_kw, grid: f.grid_kw, ev: f.ev_kw, home: f.home_kw, battery: f.battery_kw };
  const maxMag = Math.max(...Object.values(mags), 0.3);
  const widthFor = v => Math.max(2, Math.min(16, 2 + (v / maxMag) * 14));
  const lines = Object.entries(nodes).map(([key, n]) => {
    const [outward, dotColor] = dirs[key];
    const path = outward ? `M${n.x},${n.y} L${CX},${CY}` : `M${CX},${CY} L${n.x},${n.y}`;
    const isWarn = bypassActive && (key === 'grid' || key === 'home');
    const dur = n.active ? (isWarn ? '0.9s' : '1.6s') : '0s';
    const w = n.active ? widthFor(mags[key]) : 2;
    return `
      <path class="flow-line ${n.active ? 'active' : ''} ${isWarn ? 'bypass' : ''}" d="M${n.x},${n.y} L${CX},${CY}"
            stroke="${n.active ? n.color : '#1e293b'}"
            style="stroke-width:${w.toFixed(1)}px" stroke-linecap="round" />
      ${n.active ? `<circle r="${Math.max(3.5, w * 0.4).toFixed(1)}" class="flow-dot" fill="${dotColor}" style="color:${dotColor}">
        <animateMotion dur="${dur}" repeatCount="indefinite" path="${path}" />
      </circle>` : ''}`;
  }).join('');
  const nodeEls = Object.values(nodes).map(n => `
    <g class="flow-node ${n.active ? 'active' : ''}" transform="translate(${n.x},${n.y})">
      <circle class="ring" r="30" style="stroke:${n.active ? n.color : 'var(--line)'};color:${n.color};${n.glow ? `filter:drop-shadow(0 0 10px ${n.color})` : ''}" />
      <text class="icon" y="-2">${n.icon}</text>
      <text class="val" style="fill:${n.active ? n.color : 'var(--dim)'}" y="48">${n.val}</text>
      <text class="label" y="-38">${n.label}</text>
      ${n.badge ? `<text x="22" y="-20" style="font-size:16px">${n.badge}</text>` : ''}
    </g>`).join('');
  const anyActive = Object.values(nodes).some(n => n.active);
  const hubR = anyActive
    ? Math.max(5, Math.min(13, 5 + (Object.values(mags).reduce((a, b) => a + b, 0) / (maxMag * 4)) * 8))
    : 4;
  return `<div class="flow-wrap"><svg class="flow-svg" viewBox="0 0 420 400">
    ${lines}
    <circle class="flow-hub ${anyActive ? 'active' : ''}" cx="${CX}" cy="${CY}" r="${hubR.toFixed(1)}" />
    ${nodeEls}
  </svg></div>${f.ev_protected ? '<div style="text-align:center;color:var(--violet);font-size:13px;margin-top:8px">🛡️ EV Protection active — battery discharge clamped to 0</div>' : ''}`;
}
// Column order is gridlock.py's publish_plan()'s plan_table.columns —
// read positionally rather than assuming it, so this can't silently
// drift if that list's order ever changes.
function planRowObjects(table) {
  if (!table || !table.columns || !table.rows) return [];
  const cols = table.columns;
  // Defensive: a row with the wrong length would otherwise silently
  // misassign every value after the gap to the wrong column name (seen
  // in practice from a stray NaN upstream shortening a row) — skip a
  // malformed row rather than render misleading numbers from it.
  const bad = table.rows.filter(r => r.length !== cols.length).length;
  if (bad) {
    console.warn(`plan_table: ${bad} row(s) don't match the ${cols.length}-column shape — skipping them.`);
  }
  return table.rows
    .filter(row => row.length === cols.length)
    .map(row => Object.fromEntries(cols.map((c, i) => [c, row[i]])));
}
function heatColor(pence, cheapP) {
  // Relative to the site's own configured cheap-rate threshold (blue),
  // scaling up to a deep red by ~2.5x that — adaptive to whatever
  // tariff's actually in use rather than a fixed absolute pence figure
  // that could be meaningless on a different tariff.
  const peakP = Math.max(cheapP * 2.5, cheapP + 15);
  if (pence <= cheapP) return 'rgba(56,189,248,.16)';
  const frac = Math.min(1, (pence - cheapP) / (peakP - cheapP));
  const r = Math.round(56 + frac * (239 - 56));
  const g = Math.round(189 - frac * (189 - 68));
  const b = Math.round(248 - frac * (248 - 68));
  return `rgba(${r},${g},${b},.18)`;
}
function socMiniBar(pct) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="gl-soc-mini"><div class="gl-soc-mini-track"><div class="gl-soc-mini-fill" style="width:${p}%"></div></div><span class="num" style="font-size:11px">${Math.round(p)}%</span></div>`;
}
function savingSessionTitle(r) {
  // Power Down's import-side baseline is always present when this cell
  // shows — the export-side baseline (a separate sensor, confirmed real
  // via a user's own HA entity data) only sometimes is, so that part of
  // the tooltip is conditional rather than always claiming an export
  // reward that may not apply this slot.
  const exportBaseline = Number(r.session_export_baseline_kwh) || 0;
  const exportPart = exportBaseline > 0
    ? ` This window also has its own export baseline (~${exportBaseline.toFixed(2)}kWh) — exporting MORE than that earns the same points, on top of any import-side credit above.`
    : ' Exporting is a separate decision here, governed by the export price threshold.';
  return `Joined Octopus Saving Session (Power Down) — rewards importing LESS than a predicted baseline this window (based on YOUR OWN historic usage for this half-hour, not a guess). Baseline ~${Number(r.session_baseline_kwh).toFixed(2)}kWh vs ${Number(r.grid_kwh).toFixed(2)}kWh actually imported — credit is proportional to that gap, so a small baseline means a small reward even at 0 import; it isn't being left on the table.${exportPart}`;
}
function renderPlanTable(table, opts) {
  opts = opts || {};
  const rows = planRowObjects(table);
  if (!rows.length) {
    return '<div style="color:var(--dim)">Waiting for first plan — computes every 5 minutes.</div>';
  }
  const shown = opts.limit ? rows.slice(0, opts.limit) : rows;
  const cheapP = Number(opts.cheapP || 10);
  const trs = shown.map((r, i) => {
    const bypass = isBypass(r.action);
    const cls = [i === 0 ? 'is-now' : '', bypass ? 'is-bypass' : ''].filter(Boolean).join(' ');
    return `<tr class="${cls}">
      <td>${esc(r.slot)}</td>
      <td style="background:${heatColor(r.import_p, cheapP)}">${Number(r.import_p).toFixed(1)}p</td>
      <td style="background:${heatColor(r.export_p, cheapP)}">${Number(r.export_p).toFixed(1)}p</td>
      <td>${Number(r.pv_kwh).toFixed(2)}</td>
      <td>${Number(r.load_kwh).toFixed(2)}</td>
      <td>${Number(r.grid_kwh).toFixed(2)}</td>
      <td>${Number(r.charge_kwh).toFixed(2)}</td>
      <td class="num" title="Battery-side kWh discharged this slot (self-consumption + export combined) — read directly off this row, not a SoC difference against the row above">${Number(r.battery_kwh).toFixed(2)}</td>
      <td>${actionPill(r.action)}</td>
      <td>${Number(r.dispatch) > 0.5 ? `<span style="color:var(--cyan)">⚡ ${Number(r.ev_kwh).toFixed(2)}</span>` : '—'}</td>
      <td>${Number(r.saving_session) > 0.5 ? `<span title="${savingSessionTitle(r)}" style="color:#facc15">💰${Number(r.session_reward_p) > 0 ? `<br><span class="num" style="font-size:11px">+${Number(r.session_reward_p).toFixed(1)}p</span>` : ''}</span>` : '—'}</td>
      <td>${Number(r.power_up_session) > 0.5 ? `<span title="Octopus Power Up (Free Electricity) — credits consuming MORE than a predicted baseline this window (based on YOUR OWN historic usage for this half-hour), at your own unit rate. Baseline ~${Number(r.session_baseline_kwh).toFixed(2)}kWh vs ${Number(r.grid_kwh).toFixed(2)}kWh actually imported — credit is proportional to the excess above that. Separate from export: this rewards using extra power (e.g. charging harder), not selling it." style="color:#4ade80">⚡🆓${Number(r.session_reward_p) > 0 ? `<br><span class="num" style="font-size:11px">+${Number(r.session_reward_p).toFixed(1)}p</span>` : ''}</span>` : '—'}</td>
      <td>${socMiniBar(r.soc_pct)}</td>
      <td style="color:${Number(r.cost_delta_p) <= 0 ? 'var(--green)' : 'var(--amber)'}">${Number(r.cost_delta_p) > 0 ? '+' : ''}${Number(r.cost_delta_p).toFixed(1)}p</td>
      <td>£${Number(r.total_gbp).toFixed(2)}</td>
    </tr>`;
  }).join('');
  return `<div class="gl-table-scroll"><table class="gl-plan">
    <tr><th>Slot</th><th>Import</th><th>Export</th><th>PV kWh</th><th>Load kWh</th>
        <th>Grid kWh</th><th>Charge kWh</th><th title="Battery kWh discharged this slot, self-consumption + export combined">Battery kWh</th>
        <th title="CHARGE: grid charges the battery. EXPORT: battery discharges to sell. ECO: self-consumption from PV/battery. BYPASS: grid covers load directly, battery deliberately left idle because it's cheaper than cycling it. ⚠️ BYPASS: same, but forced — battery was at floor with no solar to help.">Action</th>
        <th>EV kWh</th>
        <th title="Rewards importing LESS than a predicted baseline — not exporting more. These are separate decisions; ECO with 0 grid import is already earning the full credit available.">Saving</th>
        <th title="Credits consuming MORE than a predicted baseline, at your own unit rate — not exporting more. Separate from the export decision.">Power Up</th>
        <th>SoC</th>
        <th>Grid £</th><th>Total £</th></tr>
    ${trs}
  </table></div>`;
}
// Groups the flat entities dict (label -> entity_id, from gridlock.py's
// discovered-entity attributes) into the categories the spec asked for
// — plus one catch-all so a discovered entity never silently
// disappears just because it doesn't fit Battery/Inverter/Grid/EV/Weather.
const ENTITY_CATEGORIES = [
  { name: 'Battery', icon: '🔋', labels: ['Battery SoC', 'Battery power', 'Battery temp', 'Battery SoH', 'Discharge cutoff (hardware)'] },
  { name: 'Inverter', icon: '🧠', labels: ['Inverter temp', 'PV power'] },
  { name: 'Grid', icon: '⚡', labels: ['Grid power', 'Import rate', 'Export rate', 'Daily import cost', 'Daily export value', 'Daily standing charge'] },
  { name: 'EV', icon: '🚗', labels: ['EV charging', 'EV power'] },
  { name: 'Weather & Alerts', icon: '🌩️', labels: ['Storm Watch', 'SSEN postcode'] },
  { name: 'Tariff & Home', icon: '🏠', labels: ['IOG dispatch', 'Saving sessions', 'Load power'] },
];
function renderEntityCards(entities, weather) {
  entities = entities || {};
  const cards = ENTITY_CATEGORIES.map(cat => {
    const rows = cat.labels
      .filter(l => l in entities)
      .map(l => {
        const eid = entities[l];
        return `<div class="gl-ent-row">
          <span class="gl-ent-dot" style="background:${eid ? 'var(--green)' : 'var(--red)'}"></span>
          <span class="gl-ent-label">${esc(l)}</span>
          <span class="gl-ent-id num">${eid ? esc(eid) : 'not found'}</span>
        </div>`;
      }).join('');
    const weatherRow = (cat.name === 'Weather & Alerts')
      ? `<div class="gl-ent-row">
          <span class="gl-ent-dot" style="background:${weather ? 'var(--green)' : 'var(--red)'}"></span>
          <span class="gl-ent-label">Weather</span>
          <span class="gl-ent-id num">${weather ? esc(weather.entity_id) : 'not found — no weather.* entity in HA'}</span>
        </div>` : '';
    return `<div class="gl-ent-card">
      <div class="gl-ent-card-h"><span>${cat.icon}</span>${esc(cat.name)}</div>
      ${rows}${weatherRow}
    </div>`;
  });
  return `<div class="gl-ent-cards">${cards.join('')}</div>`;
}
function renderTariffCompare(results, activeTariffName) {
  if (!results || !results.length) {
    return '<div style="color:var(--dim)">Waiting for first comparison run.</div>';
  }
  const sorted = [...results].sort((a, b) => Number(a.cost) - Number(b.cost));
  const best = Number(sorted[0].cost);
  const maxCost = Math.max(...sorted.map(r => Math.abs(Number(r.cost))), 0.01);
  const rows = sorted.map(r => {
    const cost = Number(r.cost);
    const pct = Math.max(3, (Math.abs(cost) / maxCost) * 100);
    const isBest = Math.abs(cost - best) < 0.005;
    const isActive = r.name === activeTariffName;
    return `<div class="gl-tariff-row${isBest ? ' is-best' : ''}${isActive ? ' is-active' : ''}">
      <span class="gl-name">${esc(r.name)}${isBest ? ' 🏆' : ''}</span>
      <div class="gl-tariff-track"><div class="gl-tariff-fill" style="width:${pct.toFixed(0)}%"></div></div>
      <span class="num" style="text-align:right">£${cost.toFixed(2)}</span>
    </div>`;
  }).join('');
  return rows;
}
function fmtTs(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: 'short', hour: '2-digit', minute: '2-digit' });
  } catch (e) { return iso; }
}
function fmtDate(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  } catch (e) { return iso; }
}
function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, { hour: '2-digit', minute: '2-digit' });
  } catch (e) { return iso; }
}
function fmtShortDate(dateStr) {
  // dateStr: "YYYY-MM-DD" (daily_cost_history's own date key) -> "4 Aug".
  // Parsed as local, not UTC-shifted-then-relocalized like fmtDate does
  // for full ISO timestamps — a bare date has no time/zone component to
  // shift in the first place.
  try {
    const [y, m, d] = dateStr.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  } catch (e) { return dateStr; }
}
function renderLog(entries) {
  if (!entries || !entries.length) {
    return '<div style="color:var(--dim)">No decisions logged yet — entries appear here as the plan changes.</div>';
  }
  // The reason text itself already carries the guardrail's own words
  // (e.g. "(battery at floor — bypass mode)", "Engine error, running
  // self-consumption: …") — the icon/prefix here just makes a bypass or
  // fault entry impossible to scroll past without noticing, it isn't
  // inventing new explanatory text gridlock.py didn't already log.
  const rows = entries.map(e => {
    const warn = isBypass(e.state) || isBypass(e.reason) || /fault|error/i.test(e.state);
    return `
    <div class="gl-log-row${warn ? ' is-warn' : ''}">
      <span class="gl-log-dot" style="background:${dotColor(e.state)}"></span>
      <span class="gl-log-ts num">${fmtTs(e.ts)}</span>
      <span class="gl-log-state" style="color:${dotColor(e.state)}">${warn ? '🔴 ' : ''}${esc(e.state)}</span>
      <span class="gl-log-reason">${esc(e.reason)}</span>
    </div>`;
  }).join('');
  return `<div class="gl-log-list">${rows}</div>`;
}
// The 3 stacked forecast charts (PV vs load, SoC curve, rate step-bars)
// are all built from plan_table's own rows — one source of truth, so
// they're perfectly aligned by construction rather than merged from
// separately-timestamped sensors (which is what this used to do, and
// could drift out of step if the two curves' horizons ever differed).
let triadRows = [];
const VB_W = 900;
function triadX(i, n) { return n > 1 ? (i / (n - 1)) * VB_W : VB_W / 2; }
function renderForecastTriad(table) {
  triadRows = planRowObjects(table);
  const n = triadRows.length;
  if (!n) return '<div style="color:var(--dim)">No forecast data yet.</div>';
  const H = 116;

  // Chart 1: PV vs load, overlaid areas
  const maxE = Math.max(...triadRows.map(r => Math.max(Number(r.pv_kwh), Number(r.load_kwh))), 0.1);
  const yE = v => H - 6 - (Math.min(v, maxE) / maxE) * (H - 16);
  const pvPts = triadRows.map((r, i) => `${triadX(i, n).toFixed(1)},${yE(Number(r.pv_kwh)).toFixed(1)}`).join(' ');
  const loadPts = triadRows.map((r, i) => `${triadX(i, n).toFixed(1)},${yE(Number(r.load_kwh)).toFixed(1)}`).join(' ');
  const pvArea = `M0,${H} L${pvPts} L${VB_W},${H} Z`;
  const chart1 = `
    <svg class="gl-triad-chart" viewBox="0 0 ${VB_W} ${H}" preserveAspectRatio="none"
         onmousemove="triadHover(event,this)" onmouseleave="triadLeave()">
      <path d="${pvArea}" fill="var(--amber)" opacity="0.25" />
      <polyline points="${pvPts}" fill="none" stroke="var(--amber)" stroke-width="2" />
      <polyline points="${loadPts}" fill="none" stroke="var(--violet)" stroke-width="2" stroke-dasharray="4,3" />
      <line class="gl-triad-guide" data-g="1" x1="0" y1="0" x2="0" y2="${H}" />
    </svg>`;

  // Chart 2: SoC curve
  const yS = pct => H - 6 - (Math.min(100, Math.max(0, pct)) / 100) * (H - 16);
  const socPts = triadRows.map((r, i) => `${triadX(i, n).toFixed(1)},${yS(Number(r.soc_pct)).toFixed(1)}`).join(' ');
  const chart2 = `
    <svg class="gl-triad-chart" viewBox="0 0 ${VB_W} ${H}" preserveAspectRatio="none"
         onmousemove="triadHover(event,this)" onmouseleave="triadLeave()">
      <polyline points="${socPts}" fill="none" stroke="var(--cyan)" stroke-width="2.5" />
      <line class="gl-triad-guide" data-g="1" x1="0" y1="0" x2="0" y2="${H}" />
    </svg>`;

  // Chart 3: import vs export rate, step-bars
  const maxP = Math.max(...triadRows.map(r => Math.max(Number(r.import_p), Number(r.export_p))), 1);
  const bw = VB_W / n;
  const rateBars = triadRows.map((r, i) => {
    const impH = (Number(r.import_p) / maxP) * (H / 2 - 4);
    const expH = (Number(r.export_p) / maxP) * (H / 2 - 4);
    const x = i * bw;
    return `<rect x="${x.toFixed(1)}" y="${(H / 2 - impH).toFixed(1)}" width="${Math.max(0.5, bw - 1).toFixed(1)}" height="${impH.toFixed(1)}" fill="var(--amber)" opacity="0.7" />`
      + `<rect x="${x.toFixed(1)}" y="${(H / 2).toFixed(1)}" width="${Math.max(0.5, bw - 1).toFixed(1)}" height="${expH.toFixed(1)}" fill="var(--cyan)" opacity="0.7" />`;
  }).join('');
  const chart3 = `
    <svg class="gl-triad-chart" viewBox="0 0 ${VB_W} ${H}" preserveAspectRatio="none"
         onmousemove="triadHover(event,this)" onmouseleave="triadLeave()">
      <line x1="0" y1="${H / 2}" x2="${VB_W}" y2="${H / 2}" stroke="var(--line)" stroke-width="1" />
      ${rateBars}
      <line class="gl-triad-guide" data-g="1" x1="0" y1="0" x2="0" y2="${H}" />
    </svg>`;

  return `<div class="gl-triad">
    <div class="gl-triad-tooltip" id="gl-triad-tooltip"></div>
    <div class="gl-combo-legend" style="margin-bottom:4px">
      <span><span class="gl-legend-dot" style="background:var(--amber)"></span>Solar PV (kWh)</span>
      <span><span class="gl-legend-dot" style="background:var(--violet)"></span>Home load (kWh)</span>
    </div>
    ${chart1}
    <div class="gl-combo-legend" style="margin:8px 0 4px"><span><span class="gl-legend-dot" style="background:var(--cyan)"></span>Battery SoC (%)</span></div>
    ${chart2}
    <div class="gl-combo-legend" style="margin:8px 0 4px">
      <span><span class="gl-legend-dot" style="background:var(--amber)"></span>Import rate (p)</span>
      <span><span class="gl-legend-dot" style="background:var(--cyan)"></span>Export rate (p)</span>
    </div>
    ${chart3}
  </div>`;
}
function triadHover(evt, svgEl) {
  const n = triadRows.length;
  if (!n) return;
  const rect = svgEl.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (evt.clientX - rect.left) / rect.width));
  const idx = Math.min(n - 1, Math.floor(frac * n));
  const row = triadRows[idx];
  if (!row) return;
  const vbX = triadX(idx, n);
  document.querySelectorAll('.gl-triad-guide').forEach(g => {
    g.setAttribute('x1', vbX.toFixed(1));
    g.setAttribute('x2', vbX.toFixed(1));
    g.style.opacity = 1;
  });
  const tt = document.getElementById('gl-triad-tooltip');
  if (!tt) return;
  tt.style.display = 'block';
  tt.style.left = `${(frac * 100).toFixed(1)}%`;
  tt.innerHTML = `<b>${esc(row.slot)}</b><br>
    PV ${Number(row.pv_kwh).toFixed(2)}kWh · Load ${Number(row.load_kwh).toFixed(2)}kWh<br>
    SoC ${Number(row.soc_pct).toFixed(0)}%<br>
    Import ${Number(row.import_p).toFixed(1)}p · Export ${Number(row.export_p).toFixed(1)}p<br>
    ${actionPill(row.action)}`;
}
function triadLeave() {
  document.querySelectorAll('.gl-triad-guide').forEach(g => { g.style.opacity = 0; });
  const tt = document.getElementById('gl-triad-tooltip');
  if (tt) tt.style.display = 'none';
}
// Shared tick-label row for the flex `.gl-bars` charts — one column per
// bar so labels line up with renderLoadProfileChart/renderCarbonChart's
// own `.gl-bar-col` grid exactly, but only every Nth column gets text so
// 48 half-hour bars don't turn into 48 crowded, unreadable labels.
function barAxisLabels(count, everyN, labelFor) {
  let cols = '';
  for (let i = 0; i < count; i++) {
    cols += `<div class="gl-bar-axis-col">${i % everyN === 0 ? esc(labelFor(i)) : ''}</div>`;
  }
  return `<div class="gl-bar-axis">${cols}</div>`;
}
function renderLoadProfileChart(data) {
  if (!data || !data.length) {
    return '<div style="color:var(--dim)">Still learning your usage pattern — builds up over the first few days and gets more accurate over time.</div>';
  }
  const max = Math.max(...data.map(p => Number(p.y)), 0.01);
  const bars = data.map(p => {
    const h = Math.max(2, Math.round((Number(p.y) / max) * 100));
    return `<div class="gl-bar-col" title="${esc(p.x)}: ${Number(p.y).toFixed(2)} kWh">
      <div class="gl-bar-fill gl-bar-fill-load" style="height:${h}%"></div>
    </div>`;
  }).join('');
  // Every 4th half-hour slot -> a label every 2 hours (00:00, 02:00, ...).
  const axis = barAxisLabels(data.length, 4, i => data[i].x);
  return `<div class="gl-bars">${bars}</div>${axis}`;
}
function renderDailyCostChart(data) {
  if (!data || !data.length) {
    return '<div style="color:var(--dim)">No history yet — builds up a day at a time.</div>';
  }
  const W = 900, H = 160, mid = H / 2;
  const maxAbs = Math.max(...data.map(p => Math.abs(Number(p.cost))), 0.5);
  const scale = (mid - 10) / maxAbs;
  const bw = W / data.length;
  const bars = data.map((p, i) => {
    const v = Number(p.cost);
    const h = Math.max(1, Math.abs(v) * scale);
    const x = i * bw;
    const y = v >= 0 ? mid : mid - h;
    const color = v >= 0 ? 'var(--amber)' : 'var(--green)';
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(0.5, bw - 2).toFixed(1)}" height="${h.toFixed(1)}" fill="${color}" opacity="0.75"><title>${esc(p.date)}: £${v.toFixed(2)}</title></rect>`;
  }).join('');
  // A plain HTML row below the SVG, not <text> inside it — this SVG
  // uses preserveAspectRatio="none" (deliberately non-uniform stretch
  // to fill the container), which would squash or stretch actual text
  // glyphs unevenly; flex columns underneath aren't affected by that at
  // all and reuse the exact same alignment helper as the other two
  // bar charts. ~7 labels regardless of how many days of history exist
  // yet, rather than one per day (unreadable at 28+).
  const everyN = Math.max(1, Math.ceil(data.length / 7));
  const axis = barAxisLabels(data.length, everyN, i => fmtShortDate(data[i].date));
  return `<svg viewBox="0 0 ${W} ${H}" class="gl-combo-svg" preserveAspectRatio="none" style="height:170px">
      <line x1="0" y1="${mid}" x2="${W}" y2="${mid}" stroke="var(--line)" stroke-width="1" />
      ${bars}
    </svg>
    ${axis}
    <div class="gl-combo-legend">
      <span><span class="gl-legend-dot" style="background:var(--green)"></span>Credit day</span>
      <span><span class="gl-legend-dot" style="background:var(--amber)"></span>Cost day</span>
    </div>`;
}
function carbonColor(index) {
  if (index === 'very low' || index === 'low') return 'var(--green)';
  if (index === 'moderate') return 'var(--amber)';
  return 'var(--red)'; // high, very high
}
function renderCarbonChart(data) {
  if (!data || !data.length) {
    return '<div style="color:var(--dim)">No forecast yet.</div>';
  }
  const max = Math.max(...data.map(p => Number(p.y)), 1);
  const bars = data.map(p => {
    const h = Math.max(2, Math.round((Number(p.y) / max) * 100));
    return `<div class="gl-bar-col" title="${esc(fmtDate(p.x))}: ${p.y} gCO2/kWh (${esc(p.index || '')})">
      <div class="gl-bar-fill" style="height:${h}%;background:linear-gradient(180deg,${carbonColor(p.index)},transparent)"></div>
    </div>`;
  }).join('');
  // Every 4th half-hour slot -> a label every 2 hours, same spacing as
  // the load-profile chart's own axis.
  const axis = barAxisLabels(data.length, 4, i => fmtTime(data[i].x));
  return `<div class="gl-bars">${bars}</div>${axis}`;
}
function renderProfileComparison(totals) {
  totals = totals || {};
  const names = ['eco', 'balanced', 'max_profit'];
  const labels = { eco: 'Eco', balanced: 'Balanced', max_profit: 'Max profit' };
  const vals = names.map(n => totals[n]).filter(v => v !== undefined && v !== null);
  if (!vals.length) {
    return '<div style="color:var(--dim)">Building up history — one comparison point gets added each day, check back after a few days.</div>';
  }
  const best = Math.min(...vals);
  const tiles = names.map(n => {
    const v = totals[n];
    if (v === undefined || v === null) {
      return `<div class="gl-tile"><div class="lbl">${labels[n]}</div><div class="val" style="color:var(--dim);font-size:14px">—</div></div>`;
    }
    const isBest = v === best;
    return `<div class="gl-tile"${isBest ? ' style="outline:1px solid var(--green)"' : ''}>
      <div class="lbl">${labels[n]}${isBest ? ' 🏆' : ''}</div>
      <div class="val num" style="color:${v <= 0 ? 'var(--green)' : 'var(--amber)'}">£${v.toFixed(2)}</div>
    </div>`;
  }).join('');
  return `<div class="gl-grid">${tiles}</div>`;
}
function renderSavingSessions(joined, available) {
  const parts = [];
  if (available && available.length) {
    parts.push(`<div style="color:var(--green);font-size:12px;margin-bottom:8px">⚡ ${available.length} session(s) available to join — GridLock auto-joins these.</div>`);
  }
  if (!joined || !joined.length) {
    parts.push('<div style="color:var(--dim)">No upcoming saving sessions.</div>');
  } else {
    // rewarded_octopoints is null until Octopus has settled the session
    // (usually the day or two after it ends) — sum only what's actually
    // been awarded so far, across every joined session, not just the
    // ones shown below.
    const settled = joined.filter(s => s.rewarded_octopoints !== null && s.rewarded_octopoints !== undefined);
    const totalPoints = settled.reduce((sum, s) => sum + Number(s.rewarded_octopoints), 0);
    parts.push(`<div style="color:var(--amber);font-weight:700;font-size:14px;margin-bottom:10px">🏆 ${totalPoints.toLocaleString()} Octopoints earned across ${settled.length} session(s)</div>`);
    const rows = joined.slice(-8).reverse().map(s => {
      const pts = (s.rewarded_octopoints === null || s.rewarded_octopoints === undefined)
        ? '<span style="color:var(--dim)">pending</span>'
        : `<span style="color:var(--amber);font-weight:700">${Number(s.rewarded_octopoints).toLocaleString()} pts</span>`;
      return `
      <div class="gl-sess-row">
        <span>${esc(fmtDate(s.start))} – ${esc(fmtTime(s.end))}</span>
        <span class="code">${pts} <span style="color:var(--dim)">(${s.octopoints_per_kwh || 0} pts/kWh)</span></span>
      </div>`;
    }).join('');
    parts.push(`<div>${rows}</div>`);
  }
  return parts.join('');
}
async function refresh() {
  let d;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 15000);
    const resp = await fetch('api/status', { signal: ctrl.signal });
    clearTimeout(t);
    d = await resp.json();
    latestPlanTable = d.plan_table || { columns: [], rows: [] };
  } catch (e) {
    document.getElementById('app').innerHTML =
      `<div class="gl">Could not reach GridLock (${esc(e.message || e)}) — check the add-on log, or it may still be starting up.</div>`;
    return;
  }
  renderHeaderRight(d);
  document.getElementById('app').innerHTML = `
    <div class="tab-page" data-tab="overview">
      <div class="gl">
        <div class="gl-eyebrow">⚡ GridLock Engine</div>
        <div class="gl-top">
          <span class="gl-state"><span class="gl-dot" style="background:${dotColor(d.state)}"></span>${d.state}</span>
          <span class="gl-reason">${d.reason}</span>
        </div>
        <div class="gl-grid">
          <div class="gl-tile"><div class="lbl">Import</div><div class="val num ${priceClass(d.import_p, d.cheap_rate_p)}">${d.import_p.toFixed(1)}p</div>${sparkline(planCol(d.plan_table, 'import_p').slice(0, 24), 'var(--amber)')}</div>
          <div class="gl-tile"><div class="lbl">Export</div><div class="val num" style="color:var(--cyan)">${d.export_p.toFixed(1)}p</div>${sparkline(planCol(d.plan_table, 'export_p').slice(0, 24), 'var(--cyan)')}</div>
          <div class="gl-tile" title="${d.net_today_calc_import === null || d.net_today_calc_import === undefined ? '' : `GridLock's own estimate (calculated, not billing data): import £${Number(d.net_today_calc_import).toFixed(2)} · export £${Number(d.net_today_calc_export).toFixed(2)}`}"><div class="lbl">Today net</div><div class="val num" style="color:${Number(d.net_today) <= 0 ? 'var(--green)' : 'var(--amber)'}">£${d.net_today}</div>${sparkline((d.daily_cost_history || []).slice(-14).map(p => p.cost), 'var(--amber)')}</div>
          <div class="gl-tile" title="${d.savings_today === null || d.savings_today === undefined ? '' : `Today: £${Number(d.savings_today).toFixed(2)} · Month: £${Number(d.savings_month).toFixed(2)}`}"><div class="lbl">Saved (7d)</div>${d.savings_week === null || d.savings_week === undefined
            ? '<div class="val" style="color:var(--dim);font-size:14px">learning…</div>'
            : `<div class="val num" style="color:${Number(d.savings_week) >= 0 ? 'var(--green)' : 'var(--amber)'}">£${Number(d.savings_week).toFixed(2)}</div>`}${sparkline((d.daily_savings_history || []).slice(-14).map(p => p.saved), 'var(--green)')}</div>
          <div class="gl-tile"><div class="lbl">Plan cost 24h</div><div class="val num" style="color:${Number(d.plan_cost_24h) <= 0 ? 'var(--green)' : 'var(--amber)'}">£${Number(d.plan_cost_24h).toFixed(2)}</div>${sparkline(planCol(d.plan_table, 'total_gbp'), 'var(--cyan)')}</div>
          <div class="gl-tile"><div class="lbl">Best tariff</div><div class="val" style="font-size:16px">${d.best_tariff}</div></div>
          <div class="gl-tile"><div class="lbl">EV planned</div><div class="val num" style="color:var(--cyan)">${d.ev_planned_kwh} kWh</div></div>
        </div>
        <div class="gl-bar">
          <div class="lbl"><span>Battery ${Math.round(d.soc)}%</span><span>target ${Math.round(d.target)}%</span></div>
          <div class="gl-track">
            <div class="gl-fill" style="width:${d.soc}%"></div>
            <div class="gl-target" style="left:${d.target}%"></div>
          </div>
        </div>
      </div>
      ${isBypass(d.state) ? `<div class="gl-bypass-banner">⚠️ BYPASS ACTIVE — ${esc(d.reason)}</div>` : ''}
      <div class="gl-wrap">
        <div class="gl-h">Live power flow</div>
        ${renderFlow(d.flow, isBypass(d.state))}
      </div>
      <div class="gl-wrap">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div class="gl-h" style="margin:0">Next up</div>
          ${d.state ? actionPill(d.state) : ''}
        </div>
        ${d.plan_summary ? `<div class="gl-sub">${esc(d.plan_summary)}</div>` : ''}
        <div class="gl-scroll gl-scroll-mini">${renderPlanTable(d.plan_table, { limit: 8, cheapP: d.cheap_rate_p })}</div>
        <button class="gl-more-btn" onclick="selectTab('plan')">Full 48h plan →</button>
      </div>
    </div>
    <div class="tab-page" data-tab="plan">
      <div class="gl-wrap">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <div class="gl-h" style="margin:0">30-minute action tape — full 48h plan</div>
          <button class="gl-more-btn" style="width:auto;padding:8px 16px;margin:0" onclick="downloadPlanCsv()" ${(d.plan_table && d.plan_table.rows || []).length ? '' : 'disabled'}>⬇ Download CSV</button>
        </div>
        ${d.plan_summary ? `<div class="gl-sub">${esc(d.plan_summary)}</div>` : ''}
        <div class="gl-scroll">${renderPlanTable(d.plan_table, { cheapP: d.cheap_rate_p })}</div>
      </div>
    </div>
    <div class="tab-page" data-tab="forecast">
      <div class="gl-wrap">
        <div class="gl-h">Energy forecast</div>
        <div class="gl-sub">The full plan horizon, half-hour by half-hour, all three views built from the exact same per-slot data (so they're always in step) — hover any of them to see a slot's full detail.</div>
        <div class="gl-grid" style="margin-bottom:4px">
          <div class="gl-tile"><div class="lbl">Solar today</div><div class="val num" style="color:var(--amber)">${Number(d.solar_today_kwh).toFixed(1)} kWh</div></div>
          <div class="gl-tile"><div class="lbl">Solar tomorrow</div><div class="val num" style="color:var(--amber)">${Number(d.solar_tomorrow_kwh).toFixed(1)} kWh</div></div>
        </div>
        ${renderForecastTriad(d.plan_table)}
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Battery health</div>
        <div class="gl-sub">Temperature: solar/battery efficiency drop off in high heat (not modelled in the forecast above — no reliable curve to calculate that from), but above 60°C GridLock does start reducing the commanded charge/discharge rate itself, tapering to 25% by 75°C.</div>
        <div class="gl-grid">
          ${renderTempTile('Inverter', d.inverter_temp, 60, 75)}
          ${renderTempTile('Battery cells', d.battery_temp, 40, 55)}
          ${renderSohTile(d.battery_soh)}
        </div>
        ${d.thermal_derate !== null && d.thermal_derate !== undefined && Number(d.thermal_derate) < 1
          ? `<div class="gl-sub" style="margin-top:12px;color:var(--amber)">🌡️ Thermal derate active — charge/discharge commands reduced to <b>${(Number(d.thermal_derate) * 100).toFixed(0)}%</b> of configured rate while the inverter's this warm.</div>`
          : ''}
        <div class="gl-sub" style="margin-top:12px">Cycling protection: <b style="color:var(--ink)">${esc(d.battery_risk_profile)}</b>${d.battery_degradation_cost === null || d.battery_degradation_cost === undefined ? '' : ` — needs at least ${(Number(d.battery_degradation_cost) * 100).toFixed(1)}p/kWh spread to self-consume from the battery`}${d.export_degradation_cost === null || d.export_degradation_cost === undefined ? '' : `, ${(Number(d.export_degradation_cost) * 100).toFixed(1)}p/kWh to export it`}. Set <code>battery_risk_profile</code> (eco / balanced / max_profit) in apps.yaml.</div>
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Daily cost history</div>
        <div class="gl-sub">Real grid spend per day (last 28) — green bars are days you netted a credit, amber are days you paid, companion to the Saved (7d) tile on Overview.</div>
        ${renderDailyCostChart(d.daily_cost_history)}
        ${d.plan_accuracy ? `<div class="gl-sub" style="margin-top:10px">Plan accuracy (${esc(d.plan_accuracy.date)}): predicted <b style="color:var(--ink)">£${Number(d.plan_accuracy.forecast).toFixed(2)}</b>, actual <b style="color:var(--ink)">£${Number(d.plan_accuracy.actual).toFixed(2)}</b> — the plan's own morning forecast against what actually happened, no invented score.</div>` : ''}
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Risk profile comparison</div>
        <div class="gl-sub">What each risk profile's own morning plan predicted, summed across every day recorded — not a real-outcome backtest (that would need running all three profiles continuously), but a genuine forecast-vs-forecast comparison building up over time.</div>
        ${renderProfileComparison(d.profile_comparison_totals)}
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Carbon intensity</div>
        <div class="gl-sub">GB grid carbon intensity, next 24h (National Grid ESO) — informational only, not factored into the cost plan (no solid basis to convert gCO2/kWh into a £ trade-off).</div>
        <div class="gl-grid" style="margin-bottom:4px">
          <div class="gl-tile"><div class="lbl">Right now</div><div class="val num" style="color:${carbonColor(d.carbon_index)}">${d.carbon_now === null || d.carbon_now === undefined ? '—' : Math.round(d.carbon_now)}${d.carbon_now === null || d.carbon_now === undefined ? '' : ' gCO2/kWh'}</div></div>
          <div class="gl-tile"><div class="lbl">Band</div><div class="val" style="font-size:16px;color:${carbonColor(d.carbon_index)}">${esc(d.carbon_index || '—')}</div></div>
        </div>
        ${renderCarbonChart(d.carbon_forecast_data)}
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Learned house usage</div>
        <div class="gl-sub">Your typical household draw by time of day, learned from live readings — used to plan ahead instead of assuming a flat average.</div>
        ${renderLoadProfileChart(d.learned_load_profile)}
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Storm Watch</div>
        <div class="gl-grid">
          <div class="gl-tile" style="grid-column:1/-1">
            <div class="lbl">Status</div>
            <div class="val" style="font-size:18px;color:${d.storm_state === 'Active' ? 'var(--red)' : 'var(--green)'}">${esc(d.storm_state)}</div>
            <div style="color:var(--dim);font-size:12px;margin-top:4px">${esc(d.storm_reason)}</div>
          </div>
        </div>
      </div>
      <div class="gl-wrap">
        <div class="gl-h">SSEN Power Track</div>
        ${d.ssen_postcode ? `<div class="gl-grid">
          <div class="gl-tile"><div class="lbl">Local faults</div><div class="val num" style="color:${Number(d.ssen_count) > 0 ? 'var(--red)' : 'var(--green)'}">${d.ssen_count}</div></div>
          <div class="gl-tile"><div class="lbl">Planned outages</div><div class="val num" style="color:${Number(d.ssen_planned) > 0 ? 'var(--amber)' : 'var(--green)'}">${d.ssen_planned}</div></div>
          <div class="gl-tile"><div class="lbl">Severe weather</div><div class="val" style="font-size:16px;color:${d.ssen_severe ? 'var(--red)' : 'var(--green)'}">${d.ssen_severe ? 'Flagged' : 'Clear'}</div></div>
        </div>` : `<div style="color:var(--dim)">No postcode set — SSEN polling is off until you add one. Set <code>ssen_postcode</code> in apps.yaml, or "SSEN Postcode Override" in the add-on's Configuration tab (e.g. "SW1A 1").</div>`}
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Saving sessions</div>
        ${renderSavingSessions(d.saving_joined, d.saving_available)}
      </div>
    </div>
    <div class="tab-page" data-tab="tariffs">
      <div class="gl-wrap">
        <div class="gl-h">Tariff comparison</div>
        <div class="gl-sub">Estimated cost over the same plan horizon, each tariff re-optimised under your active strategy (<b style="color:var(--ink)">${esc(d.mode_active)}</b>) — not just today's rates re-applied to today's plan.</div>
        ${renderTariffCompare(d.compare_results, d.best_tariff)}
      </div>
    </div>
    <div class="tab-page" data-tab="entities">
      <div class="gl-wrap">
        <div class="gl-h">Discovered entities</div>
        ${renderEntityCards(d.entities, d.weather)}
      </div>
    </div>
    <div class="tab-page" data-tab="log">
      <div class="gl-wrap">
        <div class="gl-h">Decision log — what changed, and why</div>
        <div class="gl-scroll">${renderLog(d.log_entries)}</div>
      </div>
    </div>
  `;
  selectTab(currentTab);
}
refresh();
setInterval(refresh, 30000);
</script>
</body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path.endswith("/api/status"):
            self._send(200, json.dumps(build_status()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.endswith("/api/mode"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            mode = str(body.get("mode", "")).lower()
            if mode not in VALID_MODES:
                self._send(400, json.dumps(
                    {"error": f"mode must be one of {VALID_MODES}"}).encode(),
                    "application/json")
                return
            ha_call_service("input_select/select_option",
                            "input_select.gridlock_mode_override", option=mode)
            self._send(200, json.dumps({"ok": True, "mode": mode}).encode(),
                       "application/json")
        except Exception as exc:  # noqa: BLE001 — surface it to the caller, don't crash the server
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page's JS is embedded in this HTML (no separate .js file),
        # so any browser caching of it silently serves stale code after
        # an update — no visible sign anything's wrong, just old bugs
        # that look like they were never fixed.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()

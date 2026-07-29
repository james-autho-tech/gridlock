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
        "plan_accuracy": savings.get("attributes", {}).get("plan_accuracy"),
        "profile_comparison_history": savings.get("attributes", {}).get("profile_comparison_history") or [],
        "profile_comparison_totals": savings.get("attributes", {}).get("profile_comparison_totals") or {},
        "carbon_now": as_float(carbon, None),
        "carbon_index": carbon.get("attributes", {}).get("index"),
        "carbon_forecast_data": carbon.get("attributes", {}).get("forecast_data") or [],
        "best_tariff": compare.get("state", "—"),
        "compare_html": compare.get("attributes", {}).get("compare_html") or "",
        "ev_planned_kwh": ev_dispatch.get("state", "0.00"),
        "log_entries": list(reversed(decision_log.get("attributes", {}).get("entries") or [])),
        "solar_today_kwh": solar.get("attributes", {}).get("today_kwh", 0),
        "solar_tomorrow_kwh": solar.get("attributes", {}).get("tomorrow_kwh", 0),
        "inverter_temp": as_float(get(status_attrs.get("inverter_temp_entity")), None),
        "battery_temp": as_float(get(status_attrs.get("battery_temp_entity")), None),
        "battery_soh": as_float(get(status_attrs.get("battery_soh_entity")), None),
        "battery_risk_profile": status_attrs.get("battery_risk_profile") or "balanced",
        "battery_degradation_cost": status_attrs.get("battery_degradation_cost"),
        "thermal_derate": status_attrs.get("thermal_derate"),
        "solar_forecast_data": solar.get("attributes", {}).get("forecast_data") or [],
        "soc_forecast_data": forecast.get("attributes", {}).get("forecast_data") or [],
        "learned_load_profile": forecast.get("attributes", {}).get("learned_load_profile") or [],
        "storm_state": storm.get("state", "Clear"),
        "storm_reason": storm.get("attributes", {}).get("reason") or "No active alerts",
        "ssen_count": ssen.get("state", "0"),
        "ssen_planned": ssen.get("attributes", {}).get("planned", 0),
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
          --violet:#a78bfa; --red:#f87171; }
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
</style>
</head>
<body>
<nav class="gl-nav">
  <button class="gl-nav-btn" data-tab="overview">Overview</button>
  <button class="gl-nav-btn" data-tab="plan">Plan</button>
  <button class="gl-nav-btn" data-tab="forecast">Forecast</button>
  <button class="gl-nav-btn" data-tab="tariffs">Tariffs</button>
  <button class="gl-nav-btn" data-tab="entities">Entities</button>
  <button class="gl-nav-btn" data-tab="log">Log</button>
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
  load_kwh: 'Load (kWh)', action: 'Action', ev_kwh: 'EV (kWh)', soc_pct: 'SoC (%)',
  cost_delta_p: 'Cost delta (p)', total_gbp: 'Total (£)',
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
  if (state.includes('Charg')) return 'var(--green)';
  if (state.includes('Export') || state.includes('Session')) return 'var(--cyan)';
  if (['Disabled','unavailable','unknown'].includes(state)) return 'var(--red)';
  return 'var(--violet)';
}
function esc(s) {
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
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
function renderFlow(f) {
  if (!f) return '';
  // Everything routes through a virtual centre hub — keeps 5
  // independent flows (solar/grid/battery/home/ev) simple to animate
  // without modelling every real physical wiring path.
  const CX = 210, CY = 195;
  const nodes = {
    solar:   { x: 210, y: 55,  icon: '☀️', label: 'Solar',   val: `${f.pv_kw.toFixed(2)} kW`,
               color: 'var(--amber)', active: f.pv_generating },
    grid:    { x: 55,  y: 195, icon: '⚡',  label: 'Grid',    val: `${f.grid_kw.toFixed(2)} kW`,
               color: f.exporting ? 'var(--cyan)' : 'var(--amber)', active: f.importing || f.exporting },
    ev:      { x: 365, y: 115, icon: '🚗', label: 'EV',      val: `${f.ev_kw.toFixed(2)} kW`,
               color: f.ev_protected ? 'var(--violet)' : 'var(--green)',
               active: f.ev_charging, badge: f.ev_protected ? '🛡️' : null },
    home:    { x: 365, y: 275, icon: '🏠', label: 'Home',    val: `${f.home_kw.toFixed(2)} kW`,
               color: 'var(--violet)', active: f.home_kw > 0.05 },
    battery: { x: 210, y: 335, icon: '🔋', label: 'Battery', val: `${f.battery_kw.toFixed(2)} kW`,
               color: f.battery_charging ? 'var(--green)' : 'var(--cyan)',
               active: f.battery_charging || f.battery_discharging },
  };
  // [outward, dotColor] — outward = node -> hub; false = hub -> node
  const dirs = {
    solar: [true, 'var(--amber)'],
    grid: [f.importing, f.exporting ? 'var(--cyan)' : 'var(--amber)'],
    ev: [false, f.ev_protected ? 'var(--violet)' : 'var(--green)'],
    home: [false, 'var(--violet)'],
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
    const dur = n.active ? '1.6s' : '0s';
    const w = n.active ? widthFor(mags[key]) : 2;
    return `
      <path class="flow-line ${n.active ? 'active' : ''}" d="M${n.x},${n.y} L${CX},${CY}"
            stroke="${n.active ? n.color : '#1e293b'}"
            style="stroke-width:${w.toFixed(1)}px" stroke-linecap="round" />
      ${n.active ? `<circle r="${Math.max(3.5, w * 0.4).toFixed(1)}" class="flow-dot" fill="${dotColor}" style="color:${dotColor}">
        <animateMotion dur="${dur}" repeatCount="indefinite" path="${path}" />
      </circle>` : ''}`;
  }).join('');
  const nodeEls = Object.values(nodes).map(n => `
    <g class="flow-node ${n.active ? 'active' : ''}" transform="translate(${n.x},${n.y})">
      <circle class="ring" r="30" style="stroke:${n.active ? n.color : 'var(--line)'};color:${n.color}" />
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
function renderEntities(entities) {
  const rows = Object.entries(entities || {}).map(([label, eid]) => `
    <div class="gl-ent-row">
      <span class="gl-ent-dot" style="background:${eid ? 'var(--green)' : 'var(--red)'}"></span>
      <span class="gl-ent-label">${esc(label)}</span>
      <span class="gl-ent-id num">${eid ? esc(eid) : 'not found — set in the add-on’s Configuration tab, or apps.yaml'}</span>
    </div>`).join('');
  return `<div class="gl-ent-list">${rows}</div>`;
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
function renderLog(entries) {
  if (!entries || !entries.length) {
    return '<div style="color:var(--dim)">No decisions logged yet — entries appear here as the plan changes.</div>';
  }
  const rows = entries.map(e => `
    <div class="gl-log-row">
      <span class="gl-log-dot" style="background:${dotColor(e.state)}"></span>
      <span class="gl-log-ts num">${fmtTs(e.ts)}</span>
      <span class="gl-log-state" style="color:${dotColor(e.state)}">${esc(e.state)}</span>
      <span class="gl-log-reason">${esc(e.reason)}</span>
    </div>`).join('');
  return `<div class="gl-log-list">${rows}</div>`;
}
function renderEnergyChart(solarData, socData) {
  // Two independent sensors merged by timestamp — solar comes straight
  // from Solcast and doesn't need a plan to exist yet, so it still
  // renders even if the battery-% line (which needs a computed plan)
  // isn't available yet, and vice versa.
  const pvByX = {};
  (solarData || []).forEach(p => { pvByX[p.x] = Number(p.y) || 0; });
  const socByX = {};
  (socData || []).forEach(p => { socByX[p.x] = Number(p.y); });
  const xs = Array.from(new Set([...Object.keys(pvByX), ...Object.keys(socByX)])).sort();
  if (!xs.length) {
    return '<div style="color:var(--dim)">No forecast data yet.</div>';
  }
  const W = 900, H = 200;
  const bw = W / xs.length;
  const maxPv = Math.max(...xs.map(x => pvByX[x] || 0), 0.1);
  const bars = xs.map((x, i) => {
    const pv = pvByX[x] || 0;
    if (pv <= 0) return '';
    const h = Math.max(1, (pv / maxPv) * (H - 24));
    const bx = i * bw;
    return `<rect x="${bx.toFixed(1)}" y="${(H - h).toFixed(1)}" width="${Math.max(0.5, bw - 1).toFixed(1)}" height="${h.toFixed(1)}" fill="var(--amber)" opacity="0.5">`
      + `<title>${esc(fmtDate(x))}: ${pv.toFixed(2)} kWh solar</title></rect>`;
  }).join('');
  const pts = xs.map((x, i) => (socByX[x] === undefined ? null : { i, pct: Math.min(100, Math.max(0, socByX[x])) }))
    .filter(Boolean)
    .map(({ i, pct }) => `${(i * bw + bw / 2).toFixed(1)},${(H - (pct / 100) * (H - 10) - 5).toFixed(1)}`)
    .join(' ');
  const line = pts ? `<polyline points="${pts}" fill="none" stroke="var(--cyan)" stroke-width="2.5" />` : '';
  return `<svg viewBox="0 0 ${W} ${H}" class="gl-combo-svg" preserveAspectRatio="none">${bars}${line}</svg>
    <div class="gl-combo-legend">
      <span><span class="gl-legend-dot" style="background:var(--amber)"></span>Solar (kWh, bars)</span>
      <span><span class="gl-legend-dot" style="background:var(--cyan)"></span>Battery % (line)</span>
    </div>`;
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
  return `<div class="gl-bars">${bars}</div>`;
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
  return `<svg viewBox="0 0 ${W} ${H}" class="gl-combo-svg" preserveAspectRatio="none" style="height:170px">
      <line x1="0" y1="${mid}" x2="${W}" y2="${mid}" stroke="var(--line)" stroke-width="1" />
      ${bars}
    </svg>
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
  return `<div class="gl-bars">${bars}</div>`;
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
  document.getElementById('app').innerHTML = `
    <div class="tab-page" data-tab="overview">
      <div class="gl">
        <div class="gl-eyebrow">⚡ GridLock Engine</div>
        <div class="gl-top">
          <span class="gl-state"><span class="gl-dot" style="background:${dotColor(d.state)}"></span>${d.state}</span>
          <span class="gl-reason">${d.reason}</span>
        </div>
        <div class="gl-grid">
          <div class="gl-tile"><div class="lbl">Import</div><div class="val num" style="color:var(--amber)">${d.import_p.toFixed(1)}p</div></div>
          <div class="gl-tile"><div class="lbl">Export</div><div class="val num" style="color:var(--cyan)">${d.export_p.toFixed(1)}p</div></div>
          <div class="gl-tile" title="${d.net_today_calc_import === null || d.net_today_calc_import === undefined ? '' : `GridLock's own estimate (calculated, not billing data): import £${Number(d.net_today_calc_import).toFixed(2)} · export £${Number(d.net_today_calc_export).toFixed(2)}`}"><div class="lbl">Today net</div><div class="val num" style="color:${Number(d.net_today) <= 0 ? 'var(--green)' : 'var(--amber)'}">£${d.net_today}</div></div>
          <div class="gl-tile" title="${d.savings_today === null || d.savings_today === undefined ? '' : `Today: £${Number(d.savings_today).toFixed(2)} · Month: £${Number(d.savings_month).toFixed(2)}`}"><div class="lbl">Saved (7d)</div>${d.savings_week === null || d.savings_week === undefined
            ? '<div class="val" style="color:var(--dim);font-size:14px">learning…</div>'
            : `<div class="val num" style="color:${Number(d.savings_week) >= 0 ? 'var(--green)' : 'var(--amber)'}">£${Number(d.savings_week).toFixed(2)}</div>`}</div>
          <div class="gl-tile"><div class="lbl">Plan cost 24h</div><div class="val num" style="color:${Number(d.plan_cost_24h) <= 0 ? 'var(--green)' : 'var(--amber)'}">£${Number(d.plan_cost_24h).toFixed(2)}</div></div>
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
      <div class="gl-wrap">
        <div class="gl-h">Live power flow</div>
        ${renderFlow(d.flow)}
      </div>
      <div class="gl-wrap">
        <div class="gl-h">Next up</div>
        ${d.plan_summary ? `<div class="gl-sub" style="margin-top:-2px">${esc(d.plan_summary)}</div>` : ''}
        <div class="gl-scroll gl-scroll-mini">${d.plan_html || '<div style="color:var(--dim)">Waiting for first plan — computes every 5 minutes.</div>'}</div>
        <button class="gl-more-btn" onclick="selectTab('plan')">Full 24h plan →</button>
      </div>
    </div>
    <div class="tab-page" data-tab="plan">
      <div class="gl-wrap">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <div class="gl-h" style="margin:0">30-minute action tape — full 24h plan</div>
          <button class="gl-more-btn" style="width:auto;padding:8px 16px;margin:0" onclick="downloadPlanCsv()" ${(d.plan_table && d.plan_table.rows || []).length ? '' : 'disabled'}>⬇ Download CSV</button>
        </div>
        ${d.plan_summary ? `<div class="gl-sub">${esc(d.plan_summary)}</div>` : ''}
        <div class="gl-scroll">${d.plan_html || '<div style="color:var(--dim)">Waiting for first plan — computes every 5 minutes.</div>'}</div>
      </div>
    </div>
    <div class="tab-page" data-tab="forecast">
      <div class="gl-wrap">
        <div class="gl-h">Energy forecast</div>
        <div class="gl-sub">The next 24h, half-hour by half-hour: expected solar generation (amber bars) against GridLock's planned battery % (cyan line) — the "battery calculator" behind the plan, and what it's charging/exporting around.</div>
        <div class="gl-grid" style="margin-bottom:4px">
          <div class="gl-tile"><div class="lbl">Solar today</div><div class="val num" style="color:var(--amber)">${Number(d.solar_today_kwh).toFixed(1)} kWh</div></div>
          <div class="gl-tile"><div class="lbl">Solar tomorrow</div><div class="val num" style="color:var(--amber)">${Number(d.solar_tomorrow_kwh).toFixed(1)} kWh</div></div>
        </div>
        ${renderEnergyChart(d.solar_forecast_data, d.soc_forecast_data)}
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
        <div class="gl-sub" style="margin-top:12px">Cycling protection: <b style="color:var(--ink)">${esc(d.battery_risk_profile)}</b>${d.battery_degradation_cost === null || d.battery_degradation_cost === undefined ? '' : ` — needs at least ${(Number(d.battery_degradation_cost) * 100).toFixed(1)}p/kWh spread before exporting/discharging the battery`}. Set <code>battery_risk_profile</code> (eco / balanced / max_profit) in apps.yaml.</div>
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
        ${d.compare_html || '<div style="color:var(--dim)">Waiting for first comparison run.</div>'}
      </div>
    </div>
    <div class="tab-page" data-tab="entities">
      <div class="gl-wrap">
        <div class="gl-h">Discovered entities</div>
        ${renderEntities(d.entities)}
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

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


def ha_get_state(entity_id):
    if not entity_id:
        return None
    req = urllib.request.Request(
        f"http://supervisor/core/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError):
        return None


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
    status = ha_get_state("sensor.gridlock_status") or {}
    status_attrs = status.get("attributes", {})
    soc = ha_get_state(status_attrs.get("soc_entity"))
    imp = ha_get_state(status_attrs.get("import_rate_entity"))
    exp = ha_get_state(status_attrs.get("export_rate_entity"))
    forecast = ha_get_state("sensor.gridlock_soc_forecast") or {}
    target = ha_get_state("sensor.gridlock_target_soc")
    compare = ha_get_state("sensor.gridlock_tariff_compare") or {}
    net = ha_get_state("sensor.gridlock_calculated_net_cost_today") or {}
    ev_dispatch = ha_get_state("sensor.gridlock_ev_dispatch_kwh") or {}

    pv_kw = sum(as_kw(ha_get_state(e))
                for e in (status_attrs.get("pv_power_entities") or []))
    battery_kw = as_kw(ha_get_state(status_attrs.get("battery_power_entity")))
    grid_kw = as_kw(ha_get_state(status_attrs.get("grid_power_entity")))
    home_kw = as_kw(ha_get_state(status_attrs.get("load_power_entity")))
    flow = {
        "pv_kw": round(pv_kw, 2),
        "battery_kw": round(battery_kw, 2),
        "grid_kw": round(grid_kw, 2),
        "home_kw": round(home_kw, 2),
        "pv_generating": is_on(ha_get_state(status_attrs.get("pv_generating_entity"))),
        "importing": is_on(ha_get_state(status_attrs.get("importing_entity"))),
        "exporting": is_on(ha_get_state(status_attrs.get("exporting_entity"))),
        "battery_charging": is_on(ha_get_state(status_attrs.get("battery_charging_entity"))),
        "battery_discharging": is_on(ha_get_state(status_attrs.get("battery_discharging_entity"))),
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
        "plan_cost_24h": forecast.get("attributes", {}).get("plan_cost_24h", 0),
        "net_today": net.get("state", "0.00"),
        "best_tariff": compare.get("state", "—"),
        "compare_html": compare.get("attributes", {}).get("compare_html") or "",
        "ev_planned_kwh": ev_dispatch.get("state", "0.00"),
        "entities": {
            "Battery SoC": status_attrs.get("soc_entity"),
            "Import rate": status_attrs.get("import_rate_entity"),
            "Export rate": status_attrs.get("export_rate_entity"),
            "EV charging": status_attrs.get("ev_entity"),
            "IOG dispatch": status_attrs.get("dispatch_entity"),
            "Saving sessions": status_attrs.get("saving_events_entity"),
            "Daily import cost": status_attrs.get("daily_import_cost_entity"),
            "Daily standing charge": status_attrs.get("daily_standing_charge_entity"),
        },
    }


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GridLock</title>
<style>
  :root { --bg:#0b1220; --panel:#111a2c; --line:#1e293b; --ink:#e2e8f0;
          --dim:#64748b; --cyan:#38bdf8; --green:#34d399; --amber:#fbbf24;
          --violet:#a78bfa; --red:#f87171; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:system-ui,sans-serif; padding:20px; }
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
  .flow-wrap { display:flex; justify-content:center; }
  .flow-svg { width:100%; max-width:440px; height:auto; }
  .flow-line { fill:none; stroke:#1e293b; stroke-width:2; }
  .flow-line.active { stroke-width:2.5; }
  .flow-dot { filter:drop-shadow(0 0 4px currentColor); }
  .flow-node circle.ring { fill:var(--panel); stroke:var(--line); stroke-width:1.5; }
  .flow-node.active circle.ring { stroke-width:2; }
  .flow-node text.icon { font-size:20px; text-anchor:middle; dominant-baseline:central; }
  .flow-node text.label { font-size:9px; letter-spacing:1px; text-transform:uppercase;
                           fill:var(--dim); text-anchor:middle; }
  .flow-node text.val { font-size:12px; font-weight:700; text-anchor:middle;
                         font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .gl-ent-list { display:flex; flex-direction:column; gap:6px; }
  .gl-ent-row { display:flex; align-items:baseline; gap:8px; font-size:13px;
                padding:4px 0; border-bottom:1px solid #14203a; }
  .gl-ent-dot { width:7px; height:7px; border-radius:50%; flex:0 0 auto; }
  .gl-ent-label { color:var(--dim); flex:0 0 150px; }
  .gl-ent-id { color:#cbd5e1; font-size:12px; word-break:break-all; }
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
<div id="app">Loading…</div>
<script>
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
function renderFlow(f) {
  if (!f) return '';
  // Everything routes through a virtual centre hub — keeps 4
  // independent flows (solar/grid/battery/home) simple to animate
  // without modelling every real physical wiring path.
  const CX = 200, CY = 165;
  const nodes = {
    solar: { x: 200, y: 55,  icon: '☀️', label: 'Solar',   val: `${f.pv_kw.toFixed(2)} kW`,
              color: 'var(--amber)', active: f.pv_generating },
    grid:  { x: 65,  y: 165, icon: '⚡',  label: 'Grid',    val: `${f.grid_kw.toFixed(2)} kW`,
              color: f.exporting ? 'var(--cyan)' : 'var(--amber)', active: f.importing || f.exporting },
    home:  { x: 335, y: 165, icon: '🏠', label: 'Home',    val: `${f.home_kw.toFixed(2)} kW`,
              color: 'var(--violet)', active: f.home_kw > 0.05 },
    battery:{ x: 200, y: 275, icon: '🔋', label: 'Battery', val: `${f.battery_kw.toFixed(2)} kW`,
              color: f.battery_charging ? 'var(--green)' : 'var(--cyan)',
              active: f.battery_charging || f.battery_discharging },
  };
  // [outward, dotColor] — outward = node -> hub; false = hub -> node
  const dirs = {
    solar: [true, 'var(--amber)'],
    grid: [f.importing, f.exporting ? 'var(--cyan)' : 'var(--amber)'],
    home: [false, 'var(--violet)'],
    battery: [f.battery_discharging, f.battery_charging ? 'var(--green)' : 'var(--cyan)'],
  };
  const lines = Object.entries(nodes).map(([key, n]) => {
    const [outward, dotColor] = dirs[key];
    const path = outward ? `M${n.x},${n.y} L${CX},${CY}` : `M${CX},${CY} L${n.x},${n.y}`;
    const dur = n.active ? '1.6s' : '0s';
    return `
      <path class="flow-line ${n.active ? 'active' : ''}" d="M${n.x},${n.y} L${CX},${CY}"
            stroke="${n.active ? n.color : '#1e293b'}" />
      ${n.active ? `<circle r="3.5" class="flow-dot" fill="${dotColor}" style="color:${dotColor}">
        <animateMotion dur="${dur}" repeatCount="indefinite" path="${path}" />
      </circle>` : ''}`;
  }).join('');
  const nodeEls = Object.values(nodes).map(n => `
    <g class="flow-node ${n.active ? 'active' : ''}" transform="translate(${n.x},${n.y})">
      <circle class="ring" r="26" style="stroke:${n.active ? n.color : 'var(--line)'}" />
      <text class="icon" y="-2">${n.icon}</text>
      <text class="val" style="fill:${n.active ? n.color : 'var(--dim)'}" y="42">${n.val}</text>
      <text class="label" y="-32">${n.label}</text>
    </g>`).join('');
  return `<div class="flow-wrap"><svg class="flow-svg" viewBox="0 0 400 320">
    ${lines}${nodeEls}
  </svg></div>`;
}
function renderEntities(entities) {
  const rows = Object.entries(entities || {}).map(([label, eid]) => `
    <div class="gl-ent-row">
      <span class="gl-ent-dot" style="background:${eid ? 'var(--green)' : 'var(--red)'}"></span>
      <span class="gl-ent-label">${esc(label)}</span>
      <span class="gl-ent-id num">${eid ? esc(eid) : 'not found — set explicitly in apps.yaml'}</span>
    </div>`).join('');
  return `<div class="gl-ent-list">${rows}</div>`;
}
async function refresh() {
  let d;
  try {
    d = await (await fetch('api/status')).json();
  } catch (e) {
    document.getElementById('app').innerHTML = '<div class="gl">Could not reach GridLock — is the add-on running?</div>';
    return;
  }
  document.getElementById('app').innerHTML = `
    <div class="gl">
      <div class="gl-eyebrow">⚡ GridLock Engine</div>
      <div class="gl-top">
        <span class="gl-state"><span class="gl-dot" style="background:${dotColor(d.state)}"></span>${d.state}</span>
        <span class="gl-reason">${d.reason}</span>
      </div>
      <div class="gl-grid">
        <div class="gl-tile"><div class="lbl">Import</div><div class="val num" style="color:var(--amber)">${d.import_p.toFixed(1)}p</div></div>
        <div class="gl-tile"><div class="lbl">Export</div><div class="val num" style="color:var(--cyan)">${d.export_p.toFixed(1)}p</div></div>
        <div class="gl-tile"><div class="lbl">Today net</div><div class="val num">£${d.net_today}</div></div>
        <div class="gl-tile"><div class="lbl">Plan cost 24h</div><div class="val num" style="color:var(--violet)">£${Number(d.plan_cost_24h).toFixed(2)}</div></div>
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
      <div class="gl-h">Discovered entities</div>
      ${renderEntities(d.entities)}
    </div>
    <div class="gl-wrap">
      <div class="gl-h">30-minute action tape</div>
      <div class="gl-scroll">${d.plan_html || '<div style="color:var(--dim)">Waiting for first plan — computes every 5 minutes.</div>'}</div>
    </div>
    <div class="gl-wrap">
      <div class="gl-h">Tariff comparison</div>
      ${d.compare_html || '<div style="color:var(--dim)">Waiting for first comparison run.</div>'}
    </div>
  `;
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
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()

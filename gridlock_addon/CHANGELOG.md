# Changelog

## 2.13.0
- Saving Sessions now show real dates (e.g. "29 Jul, 18:00") instead
  of just a weekday, which was ambiguous across months.
- New `storm_watch_entity_override` Configuration tab field, matching
  the existing `ssen_postcode_override` pattern — Storm Watch can now
  be set from apps.yaml (`storm_watch_entity:`) or the add-on's
  Configuration tab, whichever's easier.
- Clearer "not found" message in the Discovered Entities panel,
  pointing at the Configuration tab as well as apps.yaml.
- Nav bar reordered by how often each tab actually gets used:
  Overview, Plan, Forecast, Tariffs, Entities, Log (Entities/Log are
  mostly one-time setup/diagnostic, so they moved to the end).
- "Plan cost 24h" and "Today net" tiles are now colour-coded — green
  when the plan nets you money (zero or negative cost), amber when
  it's going to cost you, so it's readable at a glance.
- New **Battery forecast** chart on the Forecast tab — the planned
  battery % over the next 24h, i.e. the actual "battery calculator"
  behind the plan (was already computed for the optimiser, just never
  shown).
- New **Learned house usage** chart on the Forecast tab, showing the
  per-half-hour load profile GridLock has learned from live readings
  (same idea as [REDACTED]'s load learning) — previously tracked
  internally but invisible in the UI.

## 2.12.0
- Add a Configuration tab (Supervisor add-on's native UI) exposing
  entity overrides for anything auto-discovery might get wrong:
  EV charging/power, IOG dispatch, import/export rate, saving
  events, grid/battery/load power, SSEN postcode. Written to
  addon_overrides.json, read as a fallback between apps.yaml's own
  values and auto-discovery — apps.yaml itself is never touched.
  Not a "select your inverter/EV brand" system (that would need real
  entity-naming data from other hardware brands to build reliably,
  which isn't available) — a form-based way to fix discovery misses
  without hand-editing YAML, for the hardware GridLock already
  supports.
- grid_power_entity / battery_power_entity are now also overridable
  directly in apps.yaml (previously had no override path at all).

## 2.11.0
- Add a **Forecast** tab: Solcast solar forecast as a bar chart
  (today/tomorrow totals + hourly breakdown), Storm Watch status,
  SSEN Power Track outage status, and upcoming Saving Sessions —
  all data GridLock already pulled in but never surfaced anywhere.
- New sensor.gridlock_solar_forecast and sensor.gridlock_storm_status
  publish this so it's available outside the web UI too.

## 2.10.0
- Surface Storm Watch (MeteoAlarm) and SSEN postcode config in the
  Discovered Entities panel — previously invisible there since
  they're user-configured, not auto-discovered.
- Split the action tape: a compact "Next up" preview stays on
  Overview, full 24h detail moves to its own **Plan** tab.

## 2.9.3
- Fix the actual "Loading…" root cause, found via the browser console
  (Firefox): `Uncaught SyntaxError: unexpected token: identifier` at
  the one place in the page's JS with a backslash-escaped apostrophe
  (`GridLock\'s`). The source file has the correct two-character `\'`
  escape, but something between the add-on and the browser (Ingress
  proxy content handling, most likely) was stripping the backslash,
  truncating the string early and leaving `s plan changes...` to be
  parsed as JS. Removed the only escaped apostrophe in the file rather
  than rely on an escape sequence that isn't surviving the trip intact.

## 2.9.2
- Send `Cache-Control: no-store` on the web UI's page response — it
  has no separate .js file (the JS is inline in the HTML), so a
  cached page silently serves stale code after every update with no
  visible sign anything's wrong.

## 2.9.1
- Fix the web UI hanging on "Loading…" forever with no error: it was
  making 20+ sequential blocking HTTP calls to HA's API per page
  load (one per entity), so any single slow one stalled the whole
  response with nothing to show for it. Now fetches all states in one
  bulk call. Also added a 15s client-side timeout so a genuine failure
  shows an error instead of hanging indefinitely.

## 2.9.0
- Add a readable decision log: a running history of what GridLock
  actually did and why (state changes, not every 5-min tick), viewable
  in the web UI's new **Log** tab instead of only ever seeing the
  current status.
- Add this changelog.

## 2.8.3
- Split the web UI into tabs (Overview / Entities / Tariffs) behind a
  sticky top nav instead of one long scrolling page.
- Flow diagram scales to fill most of the screen width, with a
  pulsing glow on active nodes/lines and a glowing hub dot.

## 2.8.2
- Fix EV dispatch kWh: Octopus merges contiguous slots into one
  dispatch window and reports one total for it — that total was being
  shown on every slot inside the window instead of split across them.

## 2.8.1
- Fix PV power triple-counting in the flow diagram — Sigenergy exposes
  both an aggregate and four per-string PV power sensors at once; only
  the aggregate is used now.

## 2.8.0
- Add an EV node + protection badge to the live power-flow diagram.
- Surface PV/Grid/Battery/Load/EV power entities in the Discovered
  Entities panel so discovery failures are visible instead of silent.

## 2.7.0
- Add a live animated power-flow diagram (Solar/Grid/Battery/Home) to
  the Ingress web UI.
- Track how much energy Octopus's Intelligent dispatch has committed
  to the car (planned/completed kWh).
- Fix the action tape not reflecting live EV Protection/Storm/Session
  overrides for the current slot — it showed the theoretical
  battery-only plan even when a live override was actually applied.

## 2.6.0
- Learn real house-load usage from history instead of a flat daily
  average, via an auto-discovered power sensor.

## 2.5.x
- Fix export day-rate discovery and Hypervolt/Octopus entity naming
  drift across integration versions (dead/restored duplicate entities
  picked over the live ones).

## 2.4.x
- Auto-discover Octopus and Hypervolt entities by naming pattern —
  no manual entity ID config needed for a single account/meter/charger.
- Self-heal on a broken `apps.yaml` (backs up and restores the
  template) instead of silently failing to start.
- Fix `set -o errexit` silently killing the add-on's startup script on
  any non-zero exit from its helper scripts.

## 2.3.x
- Fix AppDaemon config persistence (`addon_config` maps to `/config`
  inside the container, not `/addon_config`).
- Fix missing `latitude`/`longitude`/`elevation` required by AppDaemon,
  pulled from HA core's own config at startup.

## 2.2.x and earlier
- Initial Supervisor add-on packaging (HACS-style install, bundled
  AppDaemon runtime, no separate AppDaemon add-on required).

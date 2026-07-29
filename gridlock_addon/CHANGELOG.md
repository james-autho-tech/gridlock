# Changelog

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

![GridLock](docs/banner.svg)

# GridLock v3

Self-contained battery optimisation engine for Sigenergy + Octopus IOG.
A linear-programming planner (PuLP) solves a rolling 48h/96-slot horizon
jointly — rates + IOG dispatches + Solcast + a learned load profile →
charge/export/eco plan, executed every 5 minutes — with three real
operational modes (`eco` / `balanced` / `max_profit`, see
`battery_risk_profile` in apps.yaml), a hard on-peak reserve constraint,
EV (Hypervolt) protection with load separation, Saving Session auto-join
+ force export, Storm Watch (Met Office red warnings, SSEN Power Track
outages, manual toggle), a 15-minute HA/Solcast failsafe deadman switch,
tariff comparison, safe-mode fault handling, and a heartbeat for the
HA-side watchdog.

The optimiser and adapters live in `apps/gridlock/core/` as plain,
AppDaemon-free Python — see `tests/` for the unit-test suite
(`pip install "pulp>=2.7,<4" pytest && pytest tests/`, no HA/AppDaemon needed).

## Screenshots

**Overview** — live status, cost/savings tiles, battery progress
![Overview](docs/screenshots/overview.png)

## Install (Supervisor add-on — no AppDaemon add-on needed)

Bundles its own AppDaemon runtime — no separate AppDaemon add-on
needed. Settings → Add-ons → Add-on Store → ⋮ →
**Repositories** → add `https://github.com/james-autho-tech/gridlock`
→ find "GridLock" → Install. Config lives in
`/addon_configs/gridlock/` once started — see
[gridlock_addon/DOCS.md](gridlock_addon/DOCS.md). If auto-discovery
picks the wrong entity for something (check the web UI's "Discovered
entities" panel), the add-on's own **Configuration** tab has override
fields for it — no YAML editing needed.

This path is newer/less battle-tested than the HACS route below —
if the build fails, check the add-on's Supervisor log first (likely
a stale base-image tag in `gridlock_addon/build.yaml`).

## Install (HACS + existing AppDaemon add-on)

Requires the [AppDaemon](https://github.com/hassio-addons/addon-appdaemon)
add-on and [HACS](https://hacs.xyz/) already installed.

1. HACS → the three-dot menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/james-autho-tech/gridlock`,
   category: **AppDaemon**.
3. Find "GridLock" in HACS → Automation and install. This places
   `gridlock.py`, `apps.yaml`, and the `core/` package (the LP optimiser
   and its adapters — HACS mirrors the whole `apps/gridlock/` folder, not
   just one file) in AppDaemon's `apps/gridlock/` automatically. Updates
   then show up in HACS like any other integration.
4. The LP optimiser needs `pulp<4` installed in the **AppDaemon add-on's
   own** Python environment (not this repo's) — add `pulp<4` to that
   add-on's Configuration → **Python packages** list and restart
   AppDaemon (pinned below 4.0 — see `gridlock_addon/Dockerfile`'s
   comment for why). No
   GLPK/system solver needed on this path: that add-on's base image is
   glibc-based, so PuLP's own bundled CBC solver runs fine (GLPK is only
   needed on GridLock's own Alpine-based Supervisor add-on — see
   `gridlock_addon/Dockerfile` — where PuLP's bundled solver doesn't run).
5. Edit `apps/gridlock/apps.yaml`: tariff rates and battery/model
   parameters. Octopus (import/export rate, IOG dispatch, saving
   sessions) and Hypervolt (EV charging) entities are **auto-discovered
   by naming pattern at startup** — nothing to set for a single
   account/meter/charger. Check the AppDaemon log (or the add-on's
   Ingress web UI's "Discovered entities" panel) for "Multiple
   entities match" warnings if you have more than one Octopus
   account/meter; only then set the affected key explicitly, as a
   **literal value** directly in `apps.yaml`:

       import_rate: sensor.octopus_energy_electricity_AAAAAAAA_1111111111111_current_rate

   Don't use `!secret` here — unlike Home Assistant core, AppDaemon's
   app-config YAML loader has no built-in secrets.yaml support. Using
   `!secret` in this file causes AppDaemon to fail to parse it
   entirely and the whole app silently stops publishing anything, with
   only a terse "Failed to read file" in the log to go on. (The
   add-on's `run` script self-heals from this now — detects the parse
   failure, backs up the broken file, restores the template — but
   better to just not hit it.)

   Optional postcode for SSEN Power Track, same reasoning — literal
   value: `ssen_postcode: "SW1A 1"`.
6. Copy `ha_support.yaml` (from the HACS-managed clone, or
   `/addon_configs/a0d7b954_appdaemon/apps/gridlock/ha_support.yaml`)
   to `/config/packages/gridlock.yaml` (helpers + fail-safe watchdog
   — see comments for why this bit must live in HA). Restart HA.
7. In `dashboard.yaml`, replace the `YOURACCOUNT`/`YOURMPAN_IMPORT`/
   `YOURMPAN_EXPORT` placeholders with the same entity IDs you used in
   `apps.yaml` (Lovelace cards aren't templated, so this has to
   match literally). Then paste it into a new dashboard via the raw
   config editor. Requires HACS frontend cards: apexcharts-card,
   power-flow-card-plus, html-template-card.
8. Watch the AppDaemon log for the startup banner and entity warnings.

## Install (manual, no HACS)

1. Clone or copy `apps/gridlock/` (this repo) into your AppDaemon
   `apps/` folder (HAOS add-on:
   `/addon_configs/a0d7b954_appdaemon/apps/`), so you end up with
   `apps/gridlock/gridlock.py` + `apps/gridlock/core/` + `apps.yaml`.
2. Steps 4–8 above.

## Running more than one site

`core/config.py`'s `SiteConfig` and the persisted state files (learned
load profile, savings history, decision log, cost tracking) are all
namespaced by the AppDaemon app's own config key — so a second physical
site (a different property, or a second battery) just needs its own
block in `apps.yaml`, using the same `gridlock.py`:

    gridlock_cabin:
      module: gridlock
      class: GridLock
      sigen_mode: select.sigen_plant_remote_ems_control_mode   # the cabin's own entities
      ...

Each block gets its own discovery, its own plan, and its own state files
— nothing is shared between them. On the Supervisor add-on specifically,
the Configuration tab's entity-override fields only ever apply to the
first/primary site block (it's a fixed single-site HA form); a second
block still works, it just can't take overrides from that tab — set any
entities discovery gets wrong for it directly in that block's own
`apps.yaml`, the same as any override.

## Entities published

- `sensor.gridlock_status` (plan_html, action, reason, plus discovered-
  entity attributes including battery_soh_entity, battery_risk_profile,
  battery_degradation_cost, thermal_derate)
- `sensor.gridlock_soc_forecast` (forecast_data, plan_cost_24h, learned_load_profile)
- `sensor.gridlock_target_soc`
- `sensor.gridlock_tariff_compare` (compare_html)
- `sensor.gridlock_calculated_net_cost_today` (import/export cost —
  real Octopus billing data when available, GridLock's own live-tracked
  calculation as a fallback and always-shown cross-check otherwise)
- `sensor.gridlock_ssen_local_outages`
- `sensor.gridlock_heartbeat`
- `sensor.gridlock_ev_dispatch_kwh` (planned_kwh, completed_kwh)
- `sensor.gridlock_decision_log` (entries — timestamped state changes)
- `sensor.gridlock_solar_forecast` (forecast_data, today_kwh, tomorrow_kwh)
- `sensor.gridlock_storm_status` (reason)
- `sensor.gridlock_savings` (today, week, month, all_time — £ actually
  saved vs a self-consumption-only baseline; daily_cost_history —
  last 28 days' real spend; plan_accuracy — most recent day's morning
  forecast vs actual outcome; profile_comparison_history/_totals —
  what each battery_risk_profile's own morning plan predicts, day by
  day and summed)
- `sensor.gridlock_carbon_intensity` (GB grid carbon intensity, gCO2/kWh,
  from National Grid ESO's public API — informational only, not
  factored into cost planning)

## Hardware support

Built and tested against Sigenergy only — that's the only inverter this
has ever actually controlled. `core/registry.py`'s `HASensorRegistry`
also recognises GivTCP (GivEnergy) and Solis naming conventions for
**read-only telemetry discovery** (SoC, capacity, power), but
`core/inverter.py`'s adapters for those two raise rather than guess at
unverified `select`/`number` service-call semantics for a real battery
inverter — this project exists specifically to avoid mis-stating a real
inverter's control mode, so it won't fabricate a control mapping it
can't test. PRs adding a verified GivTCP/Solis control adapter are welcome.

## License

Personal, non-commercial use only — see [LICENSE.md](LICENSE.md).
Forks/PRs on GitHub are welcome; redistribution elsewhere or
commercial use needs permission first.

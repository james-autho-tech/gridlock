# GridLock v2

Self-contained battery optimisation engine for Sigenergy + Octopus IOG.
Cost-greedy 24h/48-slot planner: rates + IOG dispatches + Solcast + load
model → charge/export/eco plan, executed every 5 minutes. Includes EV
(Hypervolt) protection, Saving Session auto-join + force export, Storm
Watch (Met Office red warnings, SSEN Power Track outages, manual toggle),
tariff comparison, safe-mode fault handling, and a heartbeat for the
HA-side watchdog.

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
   `gridlock.py` + `apps.yaml` in AppDaemon's `apps/gridlock/`
   automatically. Updates then show up in HACS like any other
   integration.
4. Edit `apps/gridlock/apps.yaml`: tariff rates and battery/model
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
5. Copy `ha_support.yaml` (from the HACS-managed clone, or
   `/addon_configs/a0d7b954_appdaemon/apps/gridlock/ha_support.yaml`)
   to `/config/packages/gridlock.yaml` (helpers + fail-safe watchdog
   — see comments for why this bit must live in HA). Restart HA.
6. In `dashboard.yaml`, replace the `YOURACCOUNT`/`YOURMPAN_IMPORT`/
   `YOURMPAN_EXPORT` placeholders with the same entity IDs you used in
   `apps.yaml` (Lovelace cards aren't templated, so this has to
   match literally). Then paste it into a new dashboard via the raw
   config editor. Requires HACS frontend cards: apexcharts-card,
   power-flow-card-plus, html-template-card.
7. Watch the AppDaemon log for the startup banner and entity warnings.

## Install (manual, no HACS)

1. Clone or copy `apps/gridlock/` (this repo) into your AppDaemon
   `apps/` folder (HAOS add-on:
   `/addon_configs/a0d7b954_appdaemon/apps/`), so you end up with
   `apps/gridlock/gridlock.py` + `apps.yaml`.
2. Steps 4–7 above.

## Entities published

- `sensor.gridlock_status` (plan_html, action, reason, plus discovered-
  entity attributes including battery_soh_entity, battery_risk_profile,
  battery_degradation_cost)
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
  saved vs a self-consumption-only baseline)

## License

Personal, non-commercial use only — see [LICENSE.md](LICENSE.md).
Forks/PRs on GitHub are welcome; redistribution elsewhere or
commercial use needs permission first.

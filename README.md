# GridLock v2

Self-contained battery optimisation engine for Sigenergy + Octopus IOG.
Cost-greedy 24h/48-slot planner: rates + IOG dispatches + Solcast + load
model → charge/export/eco plan, executed every 5 minutes. Includes EV
(Hypervolt) protection, Saving Session auto-join + force export, Storm
Watch (Met Office red warnings, SSEN Power Track outages, manual toggle),
tariff comparison, safe-mode fault handling, and a heartbeat for the
HA-side watchdog.

## Install (HACS)

Requires the [AppDaemon](https://github.com/hassio-addons/addon-appdaemon)
add-on and [HACS](https://hacs.xyz/) already installed.

1. HACS → the three-dot menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/james-autho-tech/gridlock`,
   category: **AppDaemon**. (Private repo — HACS needs its own
   configured GitHub token to have read access to your account's
   private repos, which it does by default since it's the same
   account that set HACS up.)
3. Find "GridLock" in HACS → AppDaemon and install. This places
   `gridlock.py` + `gridlock.yaml` in AppDaemon's `apps/gridlock/`
   automatically. Updates then show up in HACS like any other
   integration.
4. Edit `apps/gridlock/gridlock.yaml`: tariff rates, and add the
   account-identifying entity IDs to AppDaemon's own `secrets.yaml`
   (`/addon_configs/a0d7b954_appdaemon/secrets.yaml` — a different
   file from HA core's `/config/secrets.yaml`), e.g.:

       gridlock_octopus_dispatch_entity: binary_sensor.octopus_energy_a_AAAAAAAA_intelligent_dispatching
       gridlock_import_rate_entity: sensor.octopus_energy_electricity_AAAAAAAA_1111111111111_current_rate
       gridlock_export_rate_entity: sensor.octopus_energy_electricity_AAAAAAAA_2222222222222_export_current_rate
       gridlock_saving_events_entity: event.octopus_energy_a_AAAAAAAA_octoplus_saving_session_events
       gridlock_import_rates_previous_entity: event.octopus_energy_electricity_AAAAAAAA_1111111111111_previous_day_rates
       gridlock_import_rates_today_entity: event.octopus_energy_electricity_AAAAAAAA_1111111111111_current_day_rates
       gridlock_import_rates_tomorrow_entity: event.octopus_energy_electricity_AAAAAAAA_1111111111111_next_day_rates
       gridlock_export_rates_today_entity: event.octopus_energy_electricity_AAAAAAAA_2222222222222_current_day_rates
       gridlock_export_rates_tomorrow_entity: event.octopus_energy_electricity_AAAAAAAA_2222222222222_next_day_rates
       gridlock_daily_import_cost_entity: sensor.octopus_energy_electricity_AAAAAAAA_1111111111111_current_accumulative_cost
       gridlock_daily_standing_charge_entity: sensor.octopus_energy_electricity_AAAAAAAA_1111111111111_current_standing_charge
       gridlock_postcode: "SW1A 1"   # optional, enables SSEN Power Track polling

   This file is local to the AppDaemon install and never touched by
   git/HACS, so your account/MPAN/postcode never end up in the repo.
5. Copy `ha_support.yaml` (from the HACS-managed clone, or
   `/addon_configs/a0d7b954_appdaemon/apps/gridlock/ha_support.yaml`)
   to `/config/packages/gridlock.yaml` (helpers + fail-safe watchdog
   — see comments for why this bit must live in HA). Restart HA.
6. In `dashboard.yaml`, replace the `YOURACCOUNT`/`YOURMPAN_IMPORT`/
   `YOURMPAN_EXPORT` placeholders with the same entity IDs you used in
   `gridlock.yaml` (Lovelace cards aren't templated, so this has to
   match literally). Then paste it into a new dashboard via the raw
   config editor. Requires HACS frontend cards: apexcharts-card,
   power-flow-card-plus, html-template-card.
7. Watch the AppDaemon log for the startup banner and entity warnings.

## Install (manual, no HACS)

1. Clone or copy `apps/gridlock/` (this repo) into your AppDaemon
   `apps/` folder (HAOS add-on:
   `/addon_configs/a0d7b954_appdaemon/apps/`), so you end up with
   `apps/gridlock/gridlock.py` + `gridlock.yaml`.
2. Steps 4–7 above.

Optional built-in self-updater (not needed if you installed via
HACS): set `update_repo` + `update_token` in gridlock.yaml with a
fine-grained PAT scoped to **Contents: Read only** on this repo,
kept out of the file via AppDaemon `secrets.yaml`
(`update_token: !secret gridlock_pat`). The engine checks every 6h,
publishes `sensor.gridlock_version` (with `update_available`),
notifies on a new version, and with `auto_update: true` pulls the
new file, syntax-checks it, backs up the old one, and lets AppDaemon
hot-reload it. Bump `VERSION` in gridlock.py and push to `main` to
release.

## Entities published

- `sensor.gridlock_status` (plan_html, action, reason)
- `sensor.gridlock_soc_forecast` (forecast_data, plan_cost_24h)
- `sensor.gridlock_target_soc`
- `sensor.gridlock_tariff_compare` (compare_html)
- `sensor.gridlock_calculated_net_cost_today`
- `sensor.gridlock_ssen_local_outages`
- `sensor.gridlock_heartbeat`

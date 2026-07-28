# GridLock v2

Self-contained battery optimisation engine for Sigenergy + Octopus IOG.
Cost-greedy 24h/48-slot planner: rates + IOG dispatches + Solcast + load
model → charge/export/eco plan, executed every 5 minutes. Includes EV
(Hypervolt) protection, Saving Session auto-join + force export, Storm
Watch (Met Office red warnings, SSEN Power Track outages, manual toggle),
tariff comparison, safe-mode fault handling, and a heartbeat for the
HA-side watchdog.

## Install (Supervisor add-on — no AppDaemon add-on needed)

Bundles its own AppDaemon runtime, same distribution model as
[REDACTED]'s `[REDACTED]_addon`. Settings → Add-ons → Add-on Store → ⋮ →
**Repositories** → add `https://github.com/james-autho-tech/gridlock`
→ find "GridLock" → Install. Config lives in
`/addon_configs/gridlock/` once started — see
[gridlock_addon/DOCS.md](gridlock_addon/DOCS.md).

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
   `gridlock.py` + `gridlock.yaml` in AppDaemon's `apps/gridlock/`
   automatically. Updates then show up in HACS like any other
   integration.
4. Edit `apps/gridlock/gridlock.yaml`: tariff rates and battery/model
   parameters. Octopus (import/export rate, IOG dispatch, saving
   sessions) and Hypervolt (EV charging) entities are **auto-discovered
   by naming pattern at startup** — nothing to set for a single
   account/meter/charger. Check the AppDaemon log (or the add-on's
   Ingress web UI's "Discovered entities" panel) for "Multiple
   entities match" warnings if you have more than one Octopus
   account/meter; only then set the affected key explicitly — either
   directly in `gridlock.yaml`, or via AppDaemon's own `secrets.yaml`
   (`/addon_configs/a0d7b954_appdaemon/secrets.yaml` — a different
   file from HA core's `/config/secrets.yaml`) with a `!secret` ref if
   you'd rather not commit the value:

       # in secrets.yaml:
       gridlock_import_rate_entity: sensor.octopus_energy_electricity_AAAAAAAA_1111111111111_current_rate
       # in gridlock.yaml:
       import_rate: !secret gridlock_import_rate_entity

   Optional postcode for SSEN Power Track (same secrets.yaml pattern):
   `ssen_postcode: !secret gridlock_postcode` with
   `gridlock_postcode: "SW1A 1"` in secrets.yaml.
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

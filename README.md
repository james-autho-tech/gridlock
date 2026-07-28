# GridLock v2

Self-contained battery optimisation engine for Sigenergy + Octopus IOG.
Cost-greedy 24h/48-slot planner: rates + IOG dispatches + Solcast + load
model → charge/export/eco plan, executed every 5 minutes. Includes EV
(Hypervolt) protection, Saving Session auto-join + force export, Storm
Watch (Met Office red warnings, SSEN Power Track outages, manual toggle),
tariff comparison, safe-mode fault handling, and a heartbeat for the
HA-side watchdog.

## Install

1. Copy `gridlock.py` and `gridlock.yaml` into your AppDaemon `apps/`
   folder (HAOS add-on: `/addon_configs/a0d7b954_appdaemon/apps/`).
2. Edit `gridlock.yaml`: entity IDs, `ssen_postcode`, tariff rates.
3. Copy `ha_support.yaml` to `/config/packages/gridlock.yaml`
   (helpers + fail-safe watchdog — see comments for why this bit
   must live in HA). Restart HA.
4. Paste `dashboard.yaml` into a new dashboard via the raw config
   editor. Requires HACS cards: apexcharts-card, power-flow-card-plus,
   html-template-card.
5. Watch the AppDaemon log for the startup banner and entity warnings.

## Entities published

- `sensor.gridlock_status` (plan_html, action, reason)
- `sensor.gridlock_soc_forecast` (forecast_data, plan_cost_24h)
- `sensor.gridlock_target_soc`
- `sensor.gridlock_tariff_compare` (compare_html)
- `sensor.gridlock_calculated_net_cost_today`
- `sensor.gridlock_ssen_local_outages`
- `sensor.gridlock_heartbeat`

## Deploying from a private GitHub repo ([REDACTED]-style)

Repo layout = `gridlock.py`, `gridlock.yaml` etc at the repo **root**
(this repo). Cloning it into `apps/` gives AppDaemon a `gridlock/`
subfolder containing them, which it discovers automatically — no
nesting needed inside the repo itself.

**Initial deploy** — clone into AppDaemon's apps dir. Since the repo
is private, use a fine-grained PAT scoped to **Contents: Read only**
on this repo (a separate, minimal token from anything with write
access — this one lives on the HA box):

    cd /addon_configs/a0d7b954_appdaemon/apps
    git clone https://<READ_ONLY_TOKEN>@github.com/james-autho-tech/gridlock.git

This creates `apps/gridlock/gridlock.py` + `gridlock.yaml`, which
AppDaemon picks up automatically. `dashboard.yaml` and
`ha_support.yaml` also land in that folder but aren't AppDaemon
apps — AppDaemon ignores YAML without a `module`/`class` key, so
they're inert; move them out if you'd rather not see them there.

**Updates** — two options:

1. Built-in self-updater: set `update_repo` + `update_token` (same
   read-only PAT works) in gridlock.yaml. The engine checks every 6h,
   publishes `sensor.gridlock_version` (with `update_available`),
   notifies on a new version, and with `auto_update: true` pulls the
   new file, syntax-checks it, backs up the old one, and lets
   AppDaemon hot-reload it. Bump the `VERSION` string in gridlock.py
   and push to `main` to release. `update_path` defaults to
   `gridlock.py`, matching this repo's root layout.
2. Plain `git pull` (manually or via an HA shell_command) inside
   `apps/gridlock/`.

Do not commit your real `update_token` — keep gridlock.yaml with
secrets out of the repo or use AppDaemon `secrets.yaml`
(`update_token: !secret gridlock_pat`).

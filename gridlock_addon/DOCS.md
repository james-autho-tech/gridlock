# GridLock add-on — setup

## First start

On first start the add-on writes a template config to its persistent
storage folder, `/addon_configs/gridlock/` (visible over Samba / the
File editor / Studio Code Server add-ons):

- `apps/gridlock/apps.yaml` — model parameters, tariff rates.
  Octopus and Hypervolt entities are **auto-discovered by naming
  pattern at startup** — nothing to set for a single account/meter/
  charger. If discovery is ambiguous (multiple Octopus accounts/
  meters), set the affected key explicitly here as a **literal
  value** — not `!secret`. AppDaemon's app-config loader has no
  built-in secrets.yaml support the way HA core does; a `!secret` tag
  here makes the whole file fail to parse and the app silently stops
  publishing anything. The add-on self-heals from that (backs up the
  broken file, restores the template) but it's simplest to just not
  use it.

Open the add-on's sidebar panel (once Ingress is enabled via "Show in
sidebar" on this add-on's Info page) — the "Discovered entities" card
shows exactly which entity got picked for each field, with a red dot
for anything not found. That's the fastest way to confirm discovery
worked, or to see what to override if it picked the wrong one.

Edit `apps.yaml`/`secrets.yaml` as needed, then **restart the
add-on** to pick up changes.

`gridlock.py` is reset from the add-on image on every start — don't
hand-edit it in `addon_config`, it won't persist across restarts.
Ship code changes through the add-on itself (bump `config.yaml`'s
`version`, tag a release) rather than editing the running container.

## The fail-safe watchdog (HA core, not AppDaemon)

The watchdog automation that reverts the inverter to safe
self-consumption if GridLock's heartbeat goes stale has to live in HA
core, not AppDaemon — the whole point is surviving AppDaemon itself
hanging or dying, so it can't depend on AppDaemon to install or run
it. **The add-on installs and keeps this up to date automatically** —
on every start it writes `packages/gridlock.yaml` (the watchdog
automation plus the enable/mode-override helpers) directly into HA
core's own config directory and asks HA to reload it live, no manual
copying or restart required in the common case.

This needs one extra permission granted to the add-on
(`homeassistant_config:rw`, read/write access to HA's own `/config`).
Supervisor will prompt you to approve it the first time you update to
a version that added it — if you don't see the watchdog automation
under Settings → Automations after that, check the add-on's own log
for a `homeassistant_config:rw` warning: if the permission wasn't
granted, the sync is skipped entirely and a one-time manual copy (of
`ha_support.yaml` from the main repo, to
`/config/packages/gridlock.yaml`, followed by restarting HA core) is
the fallback.

## The dashboard

Open via "Open Web UI" on the add-on's Info page, or the sidebar
shortcut once enabled. Six tabs:

- **Overview** — live KPIs (current SoC, today's net cost, solar so
  far), the power-flow diagram, and a banner if the inverter is
  currently in Bypass (grid-passthrough — the genuine hardware
  fallback, not a normal state).
- **Plan** — the full 48h slot-by-slot table shown in this add-on's
  screenshots: import/export rate, PV/load/grid/charge/battery kWh,
  the action taken (Charge / Export / ECO), SoC, and running cost —
  plus the natural-language summary above it explaining the plan in
  one or two sentences.
- **Forecast** — three synced charts: SoC trace, solar forecast vs.
  actual, and learned load profile.
- **Tariffs** — how today's plan compares against other Octopus
  products (see `compare_tariffs` in `apps.yaml`) at the rates you
  actually saw today.
- **Entities** — every entity GridLock discovered or was told about,
  grouped by category, with current state — the same data as the
  sidebar's Discovered Entities card, in more detail.
- **Log** — the decision log: every action change and why, oldest at
  the bottom, newest at the top (capped to the most recent ~200
  entries).

## Power circuits (Shellys, or any power-monitoring entity)

If you have Shelly relays (or anything else exposing a `device_class:
power` sensor) monitoring individual appliances/circuits, GridLock can
show their live draw on the Forecast tab and factor them into the load
forecast — without listing entity IDs in `apps.yaml` and without
building any renaming UI of its own:

1. In Home Assistant: Settings → Areas, labels & zones → Labels →
   create a label named **"GridLock Power"** (id `gridlock_power`).
2. Apply that label to any power sensor you want tracked — e.g. a
   Shelly's own `sensor.*_switch_0_power` entity (not the
   `binary_sensor.*` diagnostic ones like Overcurrent/Overheating,
   which carry no wattage).
3. To rename what a circuit represents, rename the entity itself
   (Settings → Devices & services → Entities) — GridLock just displays
   whatever name is already there.

New/removed tags are picked up within 5 minutes, no restart needed.
Each tagged circuit is subtracted from the whole-house load sample
before it's learned, then forecast separately in its own right, so a
circuit with its own distinct pattern (a dryer that only runs some
days, say) doesn't get smeared into the general house-load baseline
the way just ignoring it would.

Why a label rather than an `apps.yaml` list: AppDaemon (what GridLock
actually runs under) has no way to query Home Assistant's own
registries at all, labels included — the lookup happens HA-side
instead, via a small template sensor in the auto-installed
`packages/gridlock.yaml` (see "The fail-safe watchdog" above for how
that file gets there).

The EV charger GridLock already tracks shows up alongside these
automatically too — it doesn't need (or use) this label at all.

## Modes (`battery_risk_profile` in `apps.yaml`)

The planning engine is a linear program, not a greedy heuristic — it
solves the whole 48h horizon jointly, so mode changes how it *values*
a slot, not a separate code path per mode:

| Mode | Behaviour |
|---|---|
| `eco` | The battery never sells — only direct solar surplus can export once the battery is full. Optimises purely for minimal import cost. |
| `balanced` (default) | Exports are allowed, but only where `export_rate − import_rate/efficiency` actually clears the degradation cost below — i.e. it only sells when doing so is a genuine net gain, not just non-negative. |
| `max_profit` | Degradation cost forced to 0. Sells any margin above round-trip cost, keeping only enough SoC for forecasted load until the next off-peak slot. |

`battery_degradation_cost` (£/kWh) overrides the mode's default
degradation figure if set — check the Forecast tab's battery health
panel for the SoH sensors this is meant to weigh against.

## Other config knobs worth knowing about (`apps.yaml`)

- `reserve_margin_pct` (default `0.15`) — extra slack the on-peak
  reserve holds back on top of the bare forecasted load for the rest
  of a peak stretch. The plan re-solves every 5 minutes and can't
  claw back charge an earlier slot already sold — raise this if
  you're seeing more Bypass than expected; lower it toward 0 to
  squeeze closer to the theoretical maximum if your load is very
  predictable.
- `target_daily_net_cost` (balanced mode only, unset by default) —
  once today's cumulative real grid cost drops to/below this figure,
  stop exporting for the rest of today.
- `floor_soc` (default `0`) — reserve the plan won't discharge below,
  even if profitable.
- `min_export_pct` (default `5.0`) — smallest export block worth
  bothering with, as a % of battery capacity; a whole contiguous block
  below this gets dropped back to self-consumption, but a genuinely
  good window is never split up to satisfy it.
- `cheap_rate_threshold` (default `0.10`) — import rate at/below which
  a slot counts as "off-peak", both for pacing self-consumption toward
  the next cheap window and as the hard floor for grid-charging.
- `storm_watch_entity` / `storm_watch_target_soc` — MeteoAlarm-driven
  override: charge to target and hold, no exports, regardless of
  price, while an alert matching the configured severity is active.
- `ssen_postcode` — enables SSEN Power Track outage polling directly
  (no HA sensor needed); leave commented out to disable it.

See the fully-commented `apps.yaml.example` shipped in the add-on
image for every option, including hardware entity overrides and the
tariff-comparison block.

## Troubleshooting

- **Dashboard looks frozen / plan hasn't updated in a while** — check
  the add-on's own Supervisor log (Info page → Log tab) for
  `Excessive time spent in callback` warnings. AppDaemon runs this app
  on a single worker thread; if a tick genuinely hangs, nothing after
  it (including the next scheduled tick) can run until it clears.
  Restarting the add-on recovers immediately; if this happens
  repeatedly, it's worth reporting with the log excerpt.
- **A field in Discovered Entities has a red dot** — either the
  integration it depends on isn't set up yet, or discovery guessed
  wrong. Set the matching `*_override` key on this add-on's
  Configuration tab, or the equivalent key in `apps.yaml` directly.
- **Plan shows "⚠️ Bypass"** — this means the battery is genuinely at
  its floor with grid import needed to cover load that slot, not just
  that SoC happens to be low — a slot that fully covers load from the
  battery with zero grid import is the reserve mechanism working as
  intended, not a Bypass condition.
- **A `!secret` tag in `apps.yaml` breaks everything** — see the note
  under First start above; use literal values here instead.

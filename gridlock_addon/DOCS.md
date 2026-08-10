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
shortcut once enabled. Tabs:

- **Overview** — live KPIs (current SoC, today's net cost, solar so
  far), the power-flow diagram, and a banner if the inverter is
  currently in Bypass (grid-passthrough — the genuine hardware
  fallback, not a normal state).
- **Plan** — the full 48h slot-by-slot table shown in this add-on's
  screenshots: import/export rate, PV/load/grid/charge/battery kWh,
  the action taken (Charge / Export / ECO / Bypass), SoC, and running
  cost — plus the natural-language summary above it explaining the
  plan in one or two sentences, and a static key explaining each
  Action value.
- **Forecast** — three synced charts (SoC trace, solar forecast vs.
  actual), plus battery health, risk profile comparison, carbon
  intensity, learned load profile, Storm Watch, SSEN Power Track, and
  Saving Sessions.
- **Billing** — daily real grid spend history, and Bill reconciliation
  (GridLock's own live-tracked cost estimate against the real bill
  entity from your Octopus integration, month-to-date totals, and a
  breakdown by tagged circuit or off-peak/on-peak split).
- **Circuits** — only shown once at least one power circuit is
  detected (see below); live draw and daily energy history per
  circuit.
- **GridWarm** — only shown once a heat pump zone is configured (see
  below); predicted temperature and heating cost per zone, plus COP.
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
show their live draw on the Circuits tab and factor them into the load
forecast — without listing entity IDs in `apps.yaml` and without
building any renaming UI of its own.

**Shelly relays are picked up automatically, no setup needed** — any
`sensor.*` entity with "shelly" in its entity ID ending in `_power`
(covers every naming variant across Shelly generations/firmwares:
`_power`, `_switch_0_power`, etc.) is included on its own, by naming
convention alone (`core/registry.py`'s `find_shelly_power_entities()`).
For anything else (a different brand's power sensor), or to make
totally sure a specific Shelly is picked up regardless of naming:

1. In Home Assistant: Settings → Areas, labels & zones → Labels →
   create a label named exactly **"GridLock Power"** (GridLock looks the
   label up by this display name, not a fixed ID, so the label's own
   internal ID doesn't matter — just the name).
2. Apply that label to any power sensor you want tracked — the label
   must go on the **entity itself**, not its device (Home Assistant
   doesn't roll device/area labels up to entities, so a label on the
   device is invisible to GridLock). Look for the entity named "Power"
   with a flash icon and `unit_of_measurement: W` — the exact entity
   ID suffix varies by Shelly model/firmware (`_power`,
   `_switch_0_power`, etc.), so go by the icon/unit rather than the
   name. Skip the `binary_sensor.*` diagnostic ones
   (Overcurrent/Overheating/etc.), which carry no wattage.
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

## Main fuse load management

On by default at 100A — standard for a UK single-phase domestic supply, which
this add-on is built for. If several high-power things run at once (an EV
charger, a hot tub, a heat pump, and the battery all charging together
overnight on cheap-rate electricity, say), combined site import can genuinely
exceed what the main fuse is rated for and trip it, cutting power to the
whole house.

GridLock can't control the EV charger, hot tub, or heat pump at all — the
only thing it actually commands is the battery. So rather than trying to
predict what those other loads are about to do, it reacts to the live
combined site-import reading (the same grid CT clamp already used
everywhere else) and **smoothly throttles the battery's own charge rate**
down to whatever headroom is genuinely left — full rate whenever there's
room, a reduced rate when there isn't, right down to 0 only if other loads
alone leave no headroom at all. It deliberately never discharges the battery
to help: the whole point of an overnight cheap-rate window is to charge it,
so draining it back out to make room for itself would defeat the purpose.

This is the single highest-priority check every tick, overriding even Storm
Watch — a tripped fuse cuts power to everything, including whatever Storm
Watch was trying to protect against.

If you're not on a 100A single-phase supply (e.g. 3-phase), set
`main_fuse_amps` to your actual per-phase rating, or `false` to disable this
entirely. See `apps.yaml` for the two threshold percentages
(`load_mgmt_warn_pct`/`load_mgmt_critical_pct`) if you want to tune how much
margin is kept below the real trip point.

## GridWarm: heat pump thermal model + anticipatory plan

If you have a heat pump, GridLock can predict each zone's temperature and
heating cost from a simple physics model of its own heat loss and heat
input — shown on its own **GridWarm** tab alongside the heat pump's COP.
That tab only appears once at least one zone is configured (see below), the
same on-demand pattern the Circuits tab already uses. **Prediction never
writes to any climate or heating entity** — it's safe to try even on a heat
pump GridLock knows nothing else about. A hot water tank zone can
additionally opt into active control (see "Active control" below), which
does write to one specific switch you configure — off by default, and kept
deliberately separate from prediction, which always stays advisory-only
regardless.

A plain weather-compensation curve only ever reacts to the *current*
outdoor temperature. GridWarm looks ahead instead, using the same
`weather.*` entity's forecast the dashboard's header already shows current
conditions from (just the forecast data instead of the current reading) —
if it's about to get milder, it eases the target down now, since passive
warming will do some of the work; if it's about to turn colder, it nudges
the target up now, getting ahead of the cold snap while the heat pump is
still running efficiently rather than playing catch-up once it's already
cold. The dashboard compares this against reacting with no lookahead at
all, in both kWh and £, so you can see whether the lookahead is actually
worth anything for your house — it isn't a guaranteed win every day, since
it depends on how much the forecast genuinely swings.

This adjusts the *target* the model works from, not a literal flow
temperature — there's no separate flow-temperature variable simulated here
— but the real effect is the same: heat less now if warmth is coming, a
little more now if cold is coming. Heat output itself stays "low and slow"
(gentle and continuous) unless a zone is genuinely far behind, rather than
cycling to full power like a boiler.

A "zone" is either a room (heated by a shared heat pump whose output varies
with outdoor temperature) or a hot water tank (heated by the same heat
pump's separate DHW circuit, losing heat to indoor ambient rather than the
outdoors — with no weather entity, a tank simply has no trend to anticipate
and holds steady, which is expected). Both use the same model, just with
different numbers. Most houses only have one real *heating* zone even if
there are several thermostats scattered around it for reading temperature
in different rooms — in that case, list just one zone (whichever room's
sensor is the most representative) with `heat_share: 1.0`, not one entry
per thermostat. Only list more than one zone if the house genuinely has
separate, independently-heated loops (their own valves/zone controllers,
not just extra thermometers). There's no auto-discovery here — heat-pump-
controller entity names are specific to the installed hardware — so each
zone is listed explicitly under `gridwarm.zones` in `apps.yaml`, which has a
fully-commented single-zone-plus-tank example. Set `gridwarm.active: false`,
or leave the whole block out, to disable this entirely.

Each individual zone can also be set `active: false` on its own — it's
still predicted and shown on the dashboard as normal, just with the
forecast-driven adjustment forced off, so it simply tracks whatever the
thermostat itself is set to. Useful for a room you deliberately want to
keep as cool as possible rather than nudged up ahead of a cold snap like
the others.

Some of the numbers involved (heat loss rate, static heat gain, thermal
mass) are genuinely hard to know exactly without instrumenting the house —
if you've previously derived your own figures (e.g. by timing how fast a
room cools with the heating off), reuse them; otherwise the example's
comments explain what each one means and a reasonable starting point. The
model is deliberately approximate for Phase 1 (in particular, `heat_share`
— how much of a shared heat pump's total output reaches any one room — is a
rough guess, not a measurement) — compare the dashboard's predicted vs.
actual temperature line over a few days and adjust the numbers that look
most off, the same way you'd tune any forecast.

**Room zones stay advisory-only for now.** Writing to a live thermostat in a
lived-in house is a different risk category from a hot water tank — wrong
comfort decisions affect people living there, not just cost — so active
control is deliberately limited to DHW for the moment, not bundled in for
rooms from the start.

### Active control (hot water tank only)

A tank zone can set `control_entity` to a switch that forces the heat pump
to heat the tank on or off — GridWarm will then actually command it each
tick based on its own plan, instead of just predicting. This is **off
unless `control_entity` is explicitly set** — nothing else about GridWarm
changes behaviour just because this exists.

Use a dedicated "force water tank heating" override built into your heat
pump's own integration for exactly this, if it has one — **not** the
tank's main DHW power/enable switch, which may also gate other internal
functions you don't want interrupted (e.g. anti-legionella disinfection
cycles).

**Test it manually before ever setting `control_entity`, and don't trust
naming or upstream documentation alone.** On the Midea-based "Heatpump
Controller" ESPHome integration this session was built against, the
seemingly obvious candidate — `switch.<device>_forced_water_tank_heating`,
a register [the project's own example automations](https://github.com/Mosibi/Midea-heat-pump-ESPHome)
use for exactly this kind of external control — turned out on real
hardware to trigger the resistive backup/immersion heater instead of the
heat pump compressor (confirmed by a massive power draw with the
compressor never engaging). The register name and the upstream project's
own documented usage both suggested otherwise; neither was reliable
enough on their own. Before enabling this, go to Developer Tools →
Actions in Home Assistant, call `switch.turn_on` against your candidate
entity, and confirm the compressor actually runs and power draw looks
like a heat pump, not a resistive element, before adding it to
`apps.yaml`.

Two safety bounds apply on top of whatever the plan wants, both biased
toward heating rather than withholding it (commanding heat on is always
the safe failure mode here):

- `control_safety_min_temp` (default 45°C) — a hard floor; heating is
  forced on below this regardless of the plan, full stop.
- `control_max_off_hours` (default 6) — caps how long this can hold
  heating off continuously. This exists because GridWarm has no visibility
  into your heat pump's own internal cycles (anti-legionella disinfection
  in particular) — better to occasionally heat "unnecessarily" than risk
  silently suppressing a safety cycle for an extended, indefinite stretch.
  This is a conservative margin, not a confirmed interaction — the
  disinfection function itself is never touched by this feature at all,
  but whether it can still run its own schedule while this switch holds
  heating off hasn't been verified against real hardware behaviour.

A real HA helper, `input_boolean.gridlock_gridwarm_control_<zone name>`, is
auto-created as a manual pause switch — flip it (from Settings → Helpers,
or the Pause button next to the zone's Control status on the GridWarm tab)
to instantly stop GridWarm touching the switch and hand control back to the
heat pump's own normal logic. Every command GridWarm actually sends is
also visible in the Log tab, same as every other decision it makes.

**Given this is the first thing in GridLock that writes to a heating
device at all, watch it closely for the first day or two** — confirm hot
water is actually available when needed, and pause it immediately via the
helper above if anything looks wrong.

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

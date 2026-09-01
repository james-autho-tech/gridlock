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
- **Tariffs** — how today's plan compares against other EV/off-peak
  products from Octopus, EDF, E.ON, Utility Warehouse, Ecotricity,
  Outfox Energy, ScottishPower, and Good Energy (see `compare_tariffs`
  in `apps.yaml` — add/edit entries there for anything else, published
  rates only, never guessed). Octopus Agile can be included too
  (`agile_region` in `apps.yaml`) — real half-hourly rates pulled live
  from Octopus's own public API rather than a flat+windows
  approximation, since Agile has no fixed daily pattern to encode
  statically. Import only; export stays whatever's actually configured.

  **Reading the numbers**: negative = credit (you'd end the period in
  profit), positive = cost — lower is always better regardless of sign.
  Bar length shows how much *extra* each option would cost you compared
  with your best/current one, not its own raw size, so a barely-worse
  option draws a barely-longer bar. `Current (live rates)` and the Agile
  row use real, live rate data; every `compare_tariffs` entry is marked
  `(est.)` because it's a fixed rate + time-window approximation of that
  tariff's published structure (typed into `apps.yaml`), not that
  product's real live dispatch — if you're actually on one of the listed
  tariffs, a small gap between it and "Current" is expected, not a bug
  (Intelligent Octopus Go's real dispatch window shifts night to night;
  the static entry can only approximate it as a fixed window).
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

## Component warranty tracking (`warranties` in `apps.yaml`)

Off unless configured. A plain list — track any component with its own
calendar warranty (an energy controller, a gateway, a heat pump, the
battery itself, ...), shown on its own **Warranty** tab (only appears
once at least one entry is configured, same on-demand pattern as
Circuits/GridWarm). Dates are DD-MM-YYYY (UK format) or YYYY-MM-DD —
both are accepted, and always shown back to you as UK format regardless
of which you typed in.

Most components are a plain calendar countdown: an install date plus a
warranty duration in years. The battery is different — Sigenergy's own
SigenStor warranty (confirmed from published EU documentation, not a
UK-specific source; worth checking against your own paperwork) is
throughput-based, not a cycle count: covered for its warranty period OR
until a fixed total energy throughput is reached, whichever comes
first. Set `throughput_cap_mwh` on the battery's entry to opt it into
this — everything else in the list just needs `install_date` and
`warranty_years`.

Per-module throughput caps (sum whichever you have): BAT 5.0 = 18.20
MWh, BAT 6.0 = 20.44 MWh, BAT 8.0 = 27.30 MWh, BAT 10.0 = 30.66 MWh.

Throughput tracking reads two real sensors from the Sigen inverter
integration itself (`*_daily_battery_charge_energy` /
`*_daily_battery_discharge_energy`, auto-discovered) — these reset
daily, so GridLock rolls each day's final reading into its own
persisted lifetime total (`warranty_state.json`), the same pattern
every other daily-to-lifetime rollover in this app already uses. No
native lifetime/"total"-class battery throughput sensor exists on this
integration (confirmed against a real entity dump) — PV production and
total load consumption both have one, battery charge/discharge only
has the daily-resetting kind.

Discharge energy specifically is what's tracked against the throughput
cap (charge is shown alongside for reference) — which side of
charge/discharge Sigenergy's own warranty wording actually counts isn't
confirmed from published documentation; discharge is the more
conservative choice and the more common convention for a "useful
energy delivered" throughput warranty. "Equivalent full cycles" is
shown too, purely as a more familiar way to picture the same number —
the real warranty measures throughput, not cycles.

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

**Learning**: the model's two loss terms — `heat_loss_degrees` (the
dominant one, scales with the internal/external temperature difference)
and `heat_loss_watts` (a smaller, fixed background loss) — both refine
themselves against real cooling periods over time, whenever a zone is
genuinely cooling with heating off at both ends of a tick. Each
observation is added to a rolling buffer (last 500); once there are
enough of them spanning a real spread of conditions (8+, with at least
~2°C of variation in the internal/external difference — not every
observation from a similar mild night), a proper line fit separates the
two terms properly, rather than solving one from a single reading while
assuming the other is already correct. Below that threshold, only
`heat_loss_degrees` refines (the simpler single-point method, holding
`heat_loss_watts` at its current value) so something still improves
during early data collection rather than nothing at all. Either way,
the actual numbers only ever move via the same gradual EMA blend the
learned house-load profile already uses — a single fit, even a good
one, can't swing them on its own; a real, consistent gap between your
house and the numbers you originally typed in shows up within a couple
of weeks. Hover the 🧠 next to a zone's name for both current learned
figures against what they started at, and how many observations they're
based on. This only refines the *prediction* — it's still advisory-only,
nothing about active control changes.

**Renaming a zone**: click the ✏️ next to its name on the GridWarm tab
and type a new one (blank resets it) — no `apps.yaml` edit or add-on
restart needed. This is purely cosmetic and stored by the web UI itself;
the zone's real underlying name (used internally for its learned
parameters and pause helper) never changes, so renaming can't disturb
anything already learned.

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

A hot water tank zone (`tank_litres` set) also gets a usable-hot-water
estimate on the GridWarm tab, shown as "X showers". This is deliberately
not the same as "how full the tank is" — a tank at or below your configured
shower temperature (`shower_temp_c`, default 40°C) has zero usable hot
water no matter how many litres it holds, since you can't mix hot water UP
to a higher temperature by adding cold, while a tank well above that
temperature yields *more* usable litres than its own physical capacity,
since most of a comfortable shower is topped up from the cold tap, not the
tank. `cold_mains_temp_c` (default 10°C) and `litres_per_shower` (default
40L) tune the conversion — override either with your own real numbers if
you know them.

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

On top of that, there's a global master switch — the **READ-ONLY / ACTIVE**
toggle on the GridWarm tab (`input_select.gridlock_gridwarm_mode`, auto-
created, defaults to `READ-ONLY`). Nothing gets written to any
`control_entity` unless this is explicitly switched to `ACTIVE`, regardless
of what's configured in `apps.yaml` — a config mistake or an old file
lying around can't silently start controlling hardware on its own. Both
this and each zone's own pause helper have to say "go" for a write to
actually happen.

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

### Heat pump diagnostics (`gridwarm.diagnostics`)

Off unless configured. GridWarm tab, "Heat pump activity" card shows
four things for every watched entity:

- **Temperature vs activity** — every temperature entity (outdoor
  ambient, room, tank) charted as a line over the last 24h, with every
  on/off activity timeline (below) aligned on the exact same time axis,
  so "it got cold, then the compressor kicked in" reads as one glance
  instead of separate disconnected views.
- **Live status** — its current value right now, at a glance.
- **Activity** — a timeline of exactly when it turned on/off (or changed
  between any other states) over the last 24h, built from history pulled
  every 30 minutes. Answers "when did it heat", "when did DHW come on"
  directly, rather than a flat list of raw states with no sense of
  duration. An entity that never changed in the window (most of a raw
  Modbus dump — capability flags, reserved bits) doesn't get a timeline
  row, to keep the noisy ones from drowning out the ones that actually
  did something; a numeric sensor with no temperature unit (voltage,
  frequency, wifi signal) is dropped from both this and the temperature
  chart, since a session log of every distinct reading is just noise and
  it isn't a temperature either — its current value still shows in Live
  status above.
- **External commands detected** — a live HA event-bus listener catching
  any real service call touching a watched entity — a genuine external
  command, from an automation, a script, or someone in the UI, with the
  actual service and value. The point is telling "something externally
  commanded this" apart from "the device is just reporting its own state"
  without manually exporting and reading a raw HA logbook CSV — confirmed
  useful against a real mystery `number.set_value` call this way, on an
  entity GridLock had never touched.

Two ways to choose which entities to watch, and they combine if both are
set:

- **`entity_prefix`** (easiest) — watches every entity, in any domain,
  whose entity_id contains this substring, re-checked on every 30-minute
  poll so an entity that appears on the device later (a firmware update
  exposing a new sensor) is picked up on its own, no restart needed. Most
  controller integrations (ESPHome, Modbus bridges, etc.) give every
  entity a shared device-name prefix, so this is usually "the device's own
  name" and nothing more — check Developer Tools > States for what your
  own controller's entities actually share.
- **`entities`** — an explicit list, for a device with no clean shared
  prefix, or to narrow a noisy device (a raw Modbus register dump is
  mostly manufacturer diagnostics — reserved bits, capability flags,
  internal pump outputs — you'll never want flagged) down to just the
  handful you actually care about.

On startup (and after every config reload), GridLock logs exactly how many
entities it resolved and their full list — check the add-on log for a line
starting "GridWarm diagnostics: watching N entities" to confirm your
config actually took effect before checking the dashboard.

## Modes (`battery_risk_profile` in `apps.yaml`)

The planning engine is a linear program, not a greedy heuristic — it
solves the whole 48h horizon jointly. **Mode only changes one thing:
how much it costs, in the maths, to cycle the battery.** Everything
else described below (self-consumption, the on-peak reserve, Storm
Watch, off-grid) works exactly the same regardless of which mode is
active.

That one thing — the **degradation cost** (£/kWh) — is actually two
separate figures per mode, because using the battery for your own load
and selling it to the grid are different decisions with different
real-world stakes:

```
selling is worth it when:  export_rate  >  (import_rate ÷ efficiency)  +  export_degradation
                            ^ what you'd earn      ^ what re-buying that       ^ battery wear
                              selling it now          energy back would cost     cost per kWh
```

| Mode | Self-consumption cost | Export cost | What that means in practice |
|---|---|---|---|
| `eco` | `0.09` £/kWh | `0.25` £/kWh | The *lowest* self-consumption cost of the three — no hesitation to use the battery for your own load. But the export bar is deliberately steep: at typical Octopus Agile/IOG spreads it sits above nearly everything, so in practice the battery almost never sells — not because it's hard-blocked, just because the price rarely clears a genuinely exceptional bar. |
| `balanced` (default) | `0.15` £/kWh | `0.15` £/kWh (same figure) | Self-consumption and export share one moderate bar. Exports happen on the routine good days (the better end of a normal Agile/IOG spread), not just rare outliers, but plenty of marginal days still don't clear it. |
| `max_profit` | `0` | `0.03` £/kWh | No hesitation to self-consume at any spread. The export bar is a small floor — just enough to stop pointless fractional-penny cycling — so almost any genuine arbitrage gets taken. |

Override either figure directly with `battery_degradation_cost`
(self-consumption) and/or `export_degradation_cost` (£/kWh) in
`apps.yaml` if you want numbers in between, or ones based on your own
battery's real replacement cost — check the Forecast tab's battery
health panel for the SoH trend these are meant to be weighed against.

**Degradation only ever governs export and grid-charging decisions —
never whether to use the battery to cover your own load.** That's a
separate, mode-independent standing rule:

> **Whenever the import price is above the cheap-rate threshold, the
> battery always covers your load instead of importing, if it has
> charge above the floor to give — full stop, in every mode, even
> `eco`.** Grid import only steps in when the battery is genuinely too
> depleted to help. This isn't a trade-off the mode dial affects; a
> cheaper-to-import-than-degrade slot still self-consumes rather than
> pull from the grid, because the alternative — importing while a
> charged battery sits idle — was found to make no sense to a real user
> watching it happen.

### How everything stacks together

Several independent mechanisms can all have an opinion about what the
battery should do right now. Highest priority wins; each one either
takes over completely or leaves the decision to the level below it:

| Priority | Mechanism | Overrides mode entirely? |
|---|---|---|
| 1 (highest) | **Off-grid**, confirmed by the inverter's own grid-connection sensor | Yes — no grid exists to trade against, so price/mode stop mattering: self-consumption only, never charging or exporting. If Storm Watch is *also* active, its own "Holding" label and reasoning are kept (more informative — it usually explains *why* you're off-grid), but the outcome is identical: hold only. |
| 2 | **Storm Watch**, while active *and* the battery doesn't already have enough banked for the estimated outage | Yes — charges to target and holds, no exports, regardless of price or mode. Stands down (falls through to normal planning) once the reserve genuinely covers the estimated outage. |
| 3 | **Saving Session** (a joined Octopus event) | Yes, for that slot — forces export regardless of mode, since the session reward is the whole point. |
| 4 | **EV Protection**, while your EV is charging concurrently | Partially — blocks battery discharge to the house (so it doesn't fight the EV for the shared circuit) and switches to "Command Charging (PV First)" so any solar still reaches the house; doesn't touch export decisions elsewhere in the plan. |
| 5 | **On-peak reserve** | Not an override as such — a standing constraint the mode-driven plan always has to satisfy: enough SoC must survive to reach the next cheap slot without hitting the floor, crediting any solar surplus expected before then. |
| 6 (baseline) | **Mode-driven plan** (the table above) | This is what runs when nothing above is active — CHARGE in cheap slots, self-consume on-peak (always, per the standing rule), export when the mode's own degradation maths says it's worth it. |

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
  Stands down instead if the battery already has enough banked to
  cover the estimated outage — SSEN's own estimated restoration time
  when the trigger is a real SSEN outage, otherwise `storm_fallback_hours`
  (default 10) — in which case the normal plan runs as if there were no
  storm at all (still shown as "Active", it just isn't overriding
  anything for that slot). No safety margin added on top of the
  estimate: exactly enough is treated as enough, by design.
- `ssen_postcode` — enables SSEN Power Track outage polling directly
  (no HA sensor needed); leave commented out to disable it.
- `grid_connection_status_entity` — Sigenergy's own "grid connection
  status" sensor (auto-discovered off the same device as `sigen_mode` if
  not set). Overrides EVERYTHING else, including Storm Watch, the moment
  the inverter reports actually being off-grid: forces Maximum Self
  Consumption and stops planning against a grid that isn't there. Storm
  Watch is a prediction (SSEN outage feeds, weather alerts); this is a
  direct hardware confirmation, so it takes priority when both fire for
  what's probably the same real event. Shown as its own pill in the
  dashboard header once discovered.

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

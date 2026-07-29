# Changelog

## 2.27.0
- **Self-consumption no longer rations available battery charge by
  default.** If there's charge above the floor, it's now spent
  covering the load in front of it, immediately — not held back for a
  hypothetically-better-value slot later in the same stretch. This
  reverts the default behaviour from the pacing/weighting work in
  2.23.0/2.24.1 for the ECO branch specifically; that logic never
  touched EXPORT decisions (a separate part of the optimiser) and
  still doesn't. The old behaviour (ration self-consumption toward a
  future off-peak window, and leave the battery alone during an
  already-cheap slot) is still available as an opt-in via
  `conserve_battery_for_peak: true` in `apps.yaml`, off by default.

## 2.26.0
- New **"Grid kWh" column** on the plan table, next to "Load kWh" —
  shows exactly how much of that slot's load actually came from the
  grid, so the "Grid £"/"Total £" columns (renamed from the plain
  "Cost"/"Total" they were before) are no longer something you have
  to take on faith. Every cost figure has only ever reflected real
  grid import at that slot's actual rate — nothing for banking energy
  for later, nothing speculative — but repeated questions about ECO
  slots "costing money" made clear the table wasn't showing its own
  working. Now it does: Load minus Grid is exactly what PV/battery
  covered for free.

## 2.25.1
- **Widened the bypass-mode trigger from `floor_soc + 0.5` to
  `floor_soc + 2.0`.** Reported from a real plan: SoC repeatedly
  bottomed out at 1-2% and stayed there, never hitting the old 0.5-point
  margin, so "Unknown" bypass never actually engaged despite the
  battery genuinely being empty in practice. Real SoC sensors report
  in whole percent and the BMS itself likely keeps some invisible
  reserve below what's shown, so requiring near-exact equality to the
  floor was never going to fire in the real world. Confirmed 1% and 2%
  now correctly trigger it while 3%+ doesn't.

## 2.25.0
- **New minimum export size** (`min_export_pct`, default 5% of battery
  capacity). Caught from a reported plan: an isolated slot with a
  merely-okay export rate sold a fraction of a percent of the battery
  for about a penny of genuine extra credit — technically a marginal
  improvement by the cost math, but not worth the SoC it spent right
  before a stretch that needed every bit of reserve to reach off-peak
  without hitting the floor. Now a whole contiguous export block is
  only kept if its total sale clears the threshold; anything smaller
  reverts to self-consumption. A genuinely good export window (like
  the big evening sell-off) is unaffected — it was always well above
  this threshold anyway.
- New `cheap_rate_threshold` documented in `apps.yaml` (default 0.10)
  — this already existed as a code default but was never actually
  written into the template; it governs both the off-peak pacing
  boundary and the hard no-peak-charging rule from 2.24.2.

## 2.24.3
- **Bypass mode now shows clearly in the status dot and decision log**,
  the same way ECO/CHARGE/EXPORT/EV Protection already do — it was
  only ever mentioned in the reason text before ("...battery at floor
  — bypass mode"), so it was easy to miss unless you read the full
  sentence. The state label itself now says e.g. "Self Consumption —
  Bypass", with its own amber dot colour.

## 2.24.2
- **Grid-charging is now a hard rule, not just cost-math discouragement:
  never charges on a peak-rate slot, full stop — the only exception is
  Storm Watch, which charges to target regardless of rate on purpose
  (a critical weather alert), and which bypasses the planner entirely
  rather than going through this rule.** Previously the optimiser only
  avoided peak charging because round-trip efficiency loss usually
  made it cost more than it saved — true for a flat two-tier tariff
  like IOG, but not guaranteed for a tariff with a genuinely tiered
  peak (e.g. a moderate day rate next to a much higher super-peak),
  where charging during the cheaper-but-still-peak slot could
  mathematically reduce total cost despite neither slot being
  off-peak. Now that's blocked outright: a slot only gets considered
  for CHARGE if its own rate is at or below the configured cheap-rate
  threshold.

## 2.24.1
- **Fixed pacing rationing evenly by time instead of by need.** Caught
  from a real reported plan: a 0.98kWh slot got nearly its full ask
  covered by the battery, then a 3.13kWh spike two slots later got the
  *exact same* ration and mostly had to import at 27.4p — because
  pacing (2.23.0) split the remaining headroom evenly across
  slots-until-off-peak, with no regard for which of those slots
  actually needs it. Now each slot's share is weighted by its own
  forecasted load-minus-PV against the whole stretch's total, so a big
  predicted load gets proportionally more of the remaining battery and
  a small one doesn't soak up a flat ration it didn't need. Same total
  budget, same floor-safe guarantee — just allocated where the plan
  already knows it'll matter.

## 2.24.0
- New **plan summary** — a one-sentence digest above the plan table
  (Overview and Plan tabs), e.g. *"Running mainly on self-consumption,
  pausing if your EV starts charging. export looks good in 9h (35p) —
  sells ~10% of battery capacity then. import drops to off-peak in
  12h. only a 10% top-up planned then — tomorrow's forecast (18kWh
  solar) covers the rest."* Every figure in it is read straight off
  the plan already computed each tick (dominant action, best export
  slot and its rate/volume, next off-peak window, the planned
  grid-charge top-up and tomorrow's solar forecast next to it) — not
  an invented explanation of the optimiser's reasoning, since it
  doesn't record one anywhere to honestly report.

## 2.23.2
- **Fixed the whole ingress page's `<script>` block silently breaking**
  in the browser ("Uncaught SyntaxError: unterminated regular
  expression literal"), introduced by the CSV export in 2.23.0. Root
  cause: the page template is a plain Python triple-quoted string, and
  the JS inside used `\n` (regex character class, and the CSV blob's
  line-join) — Python itself was interpreting those as real newline
  characters before the page was ever served, splitting a regex
  literal and a string literal across lines. Static checks against the
  source file didn't catch this (the escape sequences look correct on
  disk; the corruption only happens when Python evaluates the string),
  which is how it slipped through in 2.23.0/2.23.1 — now caught by
  actually evaluating the template the way the server does, not just
  reading the file. Fixed by making the template a raw string so
  Python leaves the embedded JS/CSS alone.

## 2.23.1
- Two follow-up fixes to the pacing/bypass logic from 2.23.0, both
  caught by direct questions rather than a bug report:
  - **Bypass mode no longer engages while PV is actively generating.**
    An empty battery with free solar arriving should still charge from
    it via normal self-consumption — bypass is only for "genuinely
    nothing useful to do," and absorbing PV is always useful.
  - **Self-consumption no longer spends stored battery charge while
    already sitting in a cheap/off-peak slot.** Discharging the
    battery to serve load when the grid import rate is already at the
    cheap threshold saves nothing over importing fresh, and costs a
    real round-trip efficiency loss for zero benefit — now it leaves
    the battery alone and imports instead, keeping whatever's stored
    available for the next expensive stretch.

## 2.23.0
- **Paced battery discharge toward the next off-peak window.** Plain
  self-consumption slots (no CHARGE or EXPORT candidate beat the
  hill-climb there — see 2.22.1) used to drain the battery at whatever
  rate the load demanded, which could hit the floor early in a long
  peak stretch and then import the rest of it at the full peak rate.
  Now, whenever a cheaper window is still visible later in the
  horizon, the remaining charge above the floor is rationed evenly
  across the slots left until then — recalculated fresh every slot
  from the actual battery level, so it self-corrects if PV covers some
  of those slots along the way. This only touches the "nothing better
  to do" fallback: a genuinely good EXPORT opportunity (or an active
  Saving Session, handled separately) still discharges flat-out, never
  paced.
- New **"Unknown" bypass mode**: once the battery's actually at the
  floor, GridLock now sends Sigenergy's documented bypass state
  instead of "Maximum Self Consumption" — the inverter has nothing
  left to give at that point, so there's no reason to leave it
  actively hunting for battery power that isn't there. Applies
  uniformly everywhere GridLock would otherwise command self-
  consumption (planned ECO slots, EV Protection, fault/safe-mode
  fallback), decided centrally in `apply()` from the live SoC reading.
- New **hardware discharge-cutoff safety net**: `floor_soc` only ever
  existed in GridLock's own 5-minute planning loop, with nothing
  stopping the real battery discharging past it if that loop ever hung
  mid-command. Now auto-discovers and syncs
  `number.sigen_plant_ess_discharge_cut_off_state_of_charge` (or your
  own override) to match `floor_soc` on every apply.
- **`floor_soc` default changed from 10 to 0.** If you're happy for
  the battery to reach empty, there's nothing to change; set it back
  above 0 in `apps.yaml` if you'd rather keep a margin.
- New **CSV export** on the Plan tab — downloads the full 24h plan
  table (rate, PV, load, action, SoC, cost, and each slot's import/
  export rank against the rest of the horizon) for anyone who wants
  to check GridLock's numbers themselves.
- Fixed the flow diagram's Battery node value label getting clipped
  off the bottom of the SVG.

## 2.22.1
- **Fixed a real planning bug**, caught from a user-reported full 24h
  plan: once the battery hit its floor, EXPORT slots kept selling PV
  surplus at whatever that slot's rate happened to be, instead of
  charging the empty battery — traced one exact figure (a reported
  "-20.5p" cost with SoC flat at the floor) to precisely 1.60kWh PV
  surplus × 12.8p, confirming zero battery discharge was actually
  involved despite the row being labelled EXPORT. Now, when there's
  nothing left to discharge, the slot falls back to self-consumption
  instead of still selling the PV.
- End-to-end effect (verified against a scenario shaped like the
  reported data): the optimiser now holds the battery back through
  mediocre-rate morning/midday windows — still capturing PV-overflow
  revenue automatically whenever the battery's already full, same as
  before — and concentrates the actual battery discharge into the
  genuinely best rate window later in the day, instead of squandering
  charge early at a fraction of the price it could fetch a few hours
  on.

## 2.22.0
- New **thermal derating**: once the inverter's temperature reads
  above 60°C, every charge/discharge command GridLock sends now gets
  scaled down (linear taper to 25% by 75°C, same thresholds already
  shown on the Battery health panel). Heat in power electronics scales
  roughly with current², so a lower commanded rate genuinely reduces
  further heat generation — an extra safety margin on top of whatever
  thermal protection the inverter already has built in, not a
  replacement for it (unverified what that protection actually is).
  Verified the derate curve at every boundary before shipping,
  including against a real reported 62.4°C (→ 88% of configured rate).
  Shown on the Battery health panel whenever it's actively reducing
  the rate, and in the decision log's reason text.

## 2.21.0
- New **carbon intensity** tracking (Forecast tab) — GB grid
  gCO2/kWh from National Grid ESO's free public API, 30-min blocks
  matching GridLock's own slot size, colour-coded by the API's own
  very-low/low/moderate/high/very-high bands. Informational only,
  not fed into cost planning — no solid basis to pick a £-per-gCO2
  conversion rate, so it's shown, not decided on.
- New **risk profile comparison** (Forecast tab) — once a day,
  alongside the existing plan-accuracy forecast snapshot, also
  computes what `eco`/`balanced`/`max_profit` would each have
  predicted for that day using the same real rates/PV/load, then
  sums across every day recorded. Not a real-outcome backtest (that
  would mean running all three profiles continuously rather than
  just the active one — too expensive to do every 5 minutes) but a
  genuine forecast-vs-forecast comparison building up over time,
  cheap since it only runs once daily.
- Fixed a bug in the day-rollover logic that would have silently
  wiped the "forecast" (plan accuracy) and any profile-comparison
  data recorded earlier that day — it was replacing the whole day's
  history entry instead of merging into it. Caught before it could
  lose real data, verified with a multi-day rollover test.

## 2.20.1
- Bumped the `balanced` risk profile's degradation cost from 3p/kWh
  to 5p/kWh — 3p meant the optimiser would actively discharge the
  battery for margins that barely covered assumed wear, especially on
  cheaply-acquired overnight charge. `eco` (9p) and `max_profit` (1p)
  unchanged.

## 2.20.0
- Storm Watch panel redesigned to match the SSEN/Battery health tile
  style, for visual consistency across the Forecast tab.
- Ran a full audit of apps.yaml against the four-piece override
  wiring (gridlock.py fallback chain, Configuration tab options +
  schema, run script) — all 15 overrides now confirmed consistent
  across every piece, closing the gap where the schema bug slipped
  through last release.
- New **Daily cost history** chart (Forecast tab) — real grid spend
  per day over the last 28 days, reusing the same data already
  persisted for the Savings feature.
- New **plan accuracy** tracking: snapshots the plan's own 24h cost
  forecast at the start of each day, then compares it against what
  actually happened once the day ends — shown as plain "predicted £X,
  actual £Y" figures, not an invented accuracy score.
- New **proactive notifications** for Storm Watch starting/clearing, a
  Saving Session being joined, and EV Protection engaging — always to
  HA's persistent_notification (no setup needed), and optionally also
  to a specific `notify_service` (e.g. your phone) if configured.
  Fires once per event, not every 5-minute tick it continues.

## 2.19.1
- Fixed the plan table's Cost/Total columns (and "Plan cost 24h")
  showing rising cost during pure self-consumption slots, before any
  real grid import was happening — a battery_risk_profile degradation
  assumption was bleeding into the displayed figures. Self-consumption
  discharging your own battery to serve load doesn't touch a meter, so
  it now correctly shows £0 for those slots; only real grid import/
  export shows a cost, matching what you'd actually see on a bill.
  The degradation cost still discourages the optimiser from cycling
  the battery for thin arbitrage margins internally — it's just no
  longer mixed into the user-facing £ figures. Verified against a
  synthetic overnight scenario (SoC draining to the floor): every
  slot above the floor now shows exactly £0.00, cost only appears
  once real grid import begins.

## 2.19.0
- **Fixed another uncommented-placeholder bug**: `battery_degradation_cost:
  0.03` was live in the shipped apps.yaml template — same class of issue
  as the export-value one just fixed. It's now commented out (an
  optional override), which also means it would have silently blocked
  the new setting below from ever taking effect for anyone using the
  template as-is.
- New `battery_risk_profile` setting (eco / balanced / max_profit) —
  a friendlier dial on the existing degradation-cost deterrent (the
  optimiser's only real lever against cycling the battery for thin
  arbitrage margins) rather than a new invented wear model, since
  there's no solid Sigenergy degradation-vs-cycle-depth data to build
  one from. eco needs a much bigger price spread before it'll
  discharge; max_profit takes any margin above cost; balanced matches
  today's existing default (unchanged behaviour if you don't set it).
  An explicit `battery_degradation_cost` still overrides the profile.
- New **Battery health** panel (renamed from System temperature) adds
  a State of Health tile (auto-discovered, prefers the plant-level
  aggregate) alongside the existing temperature ones, plus a line
  showing the active risk profile and its effective £/kWh threshold.

## 2.18.0
- **Fixed a real bug**: `daily_export_value_entity` in the shipped
  apps.yaml template was uncommented, pointing every fresh install at
  a placeholder entity name that doesn't exist for anyone — silently
  making "Today net" ignore all export credit (defaulted to £0) unless
  someone happened to override it correctly. Also had no auto-discovery
  or Configuration tab override at all, unlike every other entity.
- Export cost now auto-discovers the same way import cost already did
  (sibling `_current_accumulative_cost` sensor off the export MPAN),
  plus a `daily_export_value_entity_override` Configuration tab field
  and it's now shown in Discovered Entities.
- New: GridLock also tracks its own running import/export total every
  tick from live grid power + direction + rates — used automatically
  whenever the Octopus sensor is missing or reports unknown/
  unavailable (so "Today net" is never silently wrong for want of a
  working sensor), and always shown as a tooltip on the Today net tile
  labelled as a calculation, not billing data, so it's never confused
  with the real figure when both are available.

## 2.17.0
- New **savings tracking**: GridLock now runs a shadow
  self-consumption-only battery alongside the real one, tick by tick,
  using the same real PV/load/rate readings — the gap between what
  that hypothetical would have cost and what was actually paid (from
  the existing real net-cost sensor) is what the active plan is
  actually worth. New `sensor.gridlock_savings` (today/week/month/
  all_time), plus a "Saved (7d)" tile on the Overview page. History is
  persisted so week/month totals survive restarts; resets daily so a
  bad day can't compound forever into a misleading baseline.

## 2.16.1
- Saving Sessions now shows actual Octopoints earned per session
  (`rewarded_octopoints`, already in the Octopus integration's own
  data — no calculation needed) instead of just the pts/kWh rate, plus
  a running total across every settled session. Shows "pending" for
  sessions Octopus hasn't settled yet (usually the day or two after a
  session ends).

## 2.16.0
- Removed the self-updater feature (`update_repo`/`update_token`/
  `auto_update`) — this required a fine-grained GitHub PAT and was
  redundant with the Supervisor add-on's own update mechanism (HA's
  Add-on Store already handles this), so it added a security-sensitive
  config option nobody needed to see.
- `gridlock.py` no longer tracks its own separate `VERSION` — it now
  reads the add-on's `config.yaml` version at startup (the single
  source of truth for the add-on install path), instead of two version
  numbers that had to be bumped in sync by hand on every release.
- Removed references to other projects from the docs/comments —
  everything here describes GridLock's own behaviour on its own terms.

## 2.15.0
- Live power flow diagram is now Sankey-style: each line's thickness
  scales to that flow's actual kW, relative to whichever flow is
  biggest right now, instead of every line being the same width. The
  centre hub also grows/shrinks with total flow. Makes it obvious at a
  glance where most of the power's actually going, not just that it's
  flowing.
- Dropped the self-updater block from apps.yaml's template — it's
  only relevant to the older HACS/manual install path, not this
  Supervisor add-on (which updates through the HA Add-on Store), and
  was just clutter in the config every add-on user sees. Still fully
  documented in the README for HACS installs, and the code itself is
  unchanged.

## 2.14.2
- SSEN Power Track no longer shows a misleading "0 local fault(s)"
  when no postcode is set (polling is off until one's configured, so
  it was always reading as "all clear" by default) — now says plainly
  that polling is off and where to set the postcode.

## 2.14.1
- Fix Inverter/Battery cells in the System temperature panel showing
  the identical value — every Sigenergy entity is namespaced
  "sigen_inverter_..." regardless of which subsystem it's actually
  about (battery SoC is literally "sigen_inverter_battery_state_of_
  charge"), so matching on "inverter" as a keyword matched everything
  and both discoveries landed on the same sensor. Now matches "pcs"
  (Power Conversion System — the actual inverter internals) first,
  explicitly excluding anything with "cell"/"battery" in the name.
- Inverter temp / Battery cell temp now show up in the Discovered
  Entities panel like every other auto-discovered entity, plus new
  Configuration tab overrides (`inverter_temp_entity_override`,
  `battery_temp_entity_override`) and apps.yaml keys, for setups where
  the "pcs"/"cell" keywords still pick the wrong sensor.

## 2.14.0
- Decision log now drops in a "Still: ..." check-in once an hour even
  when nothing's changed, instead of going completely silent for
  however long the plan stays steady — a long quiet stretch is normal
  (most ticks change nothing) but looked identical to the engine
  having stopped.
- New **System temperature** panel on the Forecast tab — auto-discovers
  the Sigenergy inverter and battery-cell temperature sensors, shown
  as colour-coded tiles (green/amber/red). Solar and battery
  efficiency both fall off in high heat; this is a sanity check on the
  forecast, not a factor fed into it (no reliable derating curve to
  calculate that from).
- The 24h plan table now has **Cost** (this slot's £ cost/saving) and
  **Total** (running total through that slot) columns — makes it
  possible to see which slots are actually moving the needle rather
  than just the 24h total.
- Plan rows are now tinted by action (green charge, cyan export/
  session, amber hold/EV-protection, grey eco) instead of just the
  Action column text, for a faster scan down 48 rows.

## 2.13.1
- Merged the Forecast tab's Solar forecast and Battery forecast charts
  into one **Energy forecast** chart — solar generation as bars against
  the planned battery % as a line, on a shared 24h timeline. Also fixes
  the previous Solar chart rendering as a flat line: it was aggregating
  by hour across however many days Solcast happened to return (could be
  a week+ on some accounts), so a handful of far-future days dwarfed the
  scale and every real bar rounded down to nothing. The new chart is
  built from the same 48 half-hour slots the plan itself uses, so it's
  always exactly the next 24h.

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
  per-half-hour load profile GridLock has learned from live readings —
  previously tracked internally but invisible in the UI.

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

# Changelog

## 3.6.3 - 2026-08-07

### Fix
- HA config directory detection now checks both `/homeassistant_config` and `/homeassistant/config` and uses whichever actually exists, instead of assuming a single fixed path

## 3.6.2 - 2026-08-07

### Fix
- Reverted the 3.6.1 `hassio_role` change (didn't fix it, disproved against Home Assistant's own Samba add-on)
- `map:` entries switched to the structured `type`/`read_only` syntax instead of the old shorthand, to fix `homeassistant_config` not mounting

## 3.6.1 - 2026-08-07

### Fix
- `hassio_role` raised from `default` to `manager` — `homeassistant_config:rw` was never actually mounting under `default`, so the fail-safe watchdog automation and the Power Circuits label bridge silently never installed, on every install to date

## 3.6.0 - 2026-08-07

### Improvement
- New: labelled power circuits — tag any power sensor (e.g. a Shelly relay) with a "GridLock Power" label in HA and it shows up as a live bar chart on the Forecast tab, no apps.yaml editing needed
- Labelled circuits are now subtracted from the whole-house load sample and forecast separately, so their own pattern doesn't distort the learned baseline
- EV charger now also shown on the same Power Circuits card, for consistency

## 3.5.8 - 2026-08-05

### Fix
- Added the missing STORM_HOLD row to the new Action key table

## 3.5.7 - 2026-08-05

### Improvement
- Replaced the Action column's hover tooltips with a plain, always-visible key table above the full plan

## 3.5.6 - 2026-08-05

### Improvement
- Added hover explanations for the Action column's Bypass and Forced Bypass pills, plus a summary tooltip on the column header

## 3.5.5 - 2026-08-05

### Improvement
- Plan table now labels deliberate cheap-import passthrough slots "Bypass" instead of "ECO" — battery held back on purpose, not self-consumption
- Renamed the genuine floor-forced case to "Forced Bypass" so it stays visually distinct (warning styling) from the new, cost-optimal "Bypass" label

## 3.5.4 - 2026-08-05

### Improvement
- Power Down now also rewards exporting above its own export-side baseline, on top of the existing import-reduction credit
- Saving Session tooltip and CSV export now show the export baseline alongside the import one
- Power Up's export baseline is discovered but not yet used in the plan

## 3.5.3 - 2026-08-05

### Improvement
- Plan table's Saving/Power Up tooltips now show the actual predicted baseline (kWh) alongside your real import, so a small reward is self-explanatory (proportional to a small baseline gap, not money left on the table) instead of looking unexplained
- New `session_baseline_kwh` field in the plan table and CSV export

## 3.5.2 - 2026-08-05

### Improvement
- Plan table's Saving/Power Up cells now show the expected reward in pence directly on the row, not just in a hover tooltip, and the column headers explain what each programme actually rewards (importing less / consuming more, not exporting) so a row showing ECO with 0 grid import doesn't read as "the reward wasn't worth it"
- Added Intelligent Octopus Go, EDF GoElectric, and E.ON Next Drive to the tariff comparison list (`compare_tariffs`) — rates checked against each supplier's own published figures; IOG's variable smart-dispatch bonus hours can't be represented in a static comparison, so it only reflects the guaranteed overnight window

## 3.5.1 - 2026-08-05

### Improvement
- Date/time axis labels added to the Forecast tab's three bar charts (Learned House Usage, Carbon Intensity, Real grid spend per day) — previously bare bars with no ticks, only a hover tooltip

## 3.5.0 - 2026-08-03

### Improvement
- Octopus Saving Sessions discovery now recognises both the new "Power Down" entity naming and the old "Saving Sessions" naming, and joins sessions via whichever service actually exists — old entities/services are removed in January 2027 per Octopus's own deprecation notice
- New: Octoplus "Power Up" (formerly Free Electricity Sessions) support — a genuinely separate programme from Power Down (rewards consuming *more* than a predicted baseline, credited in £ at your own unit rate, not octopoints; no join step, automatic once enrolled)
- The LP now weighs both programmes' real economics directly: Power Down's octopoints-per-kWh reward for reducing below a predicted baseline, and Power Up's £-per-kWh credit for exceeding one — both read from the integration's own baseline sensor (a genuine forward-looking per-half-hour prediction, not a guess), modelled with a MILP-safe formulation that can't be gamed and stays solvable even when a slot is unavoidably forced past its baseline in either direction
- Plan table (and dashboard) now shows a Power Up marker alongside the existing Saving Session one, plus the expected £ reward/credit per slot
- Session rewards are reported as their own figure, kept fully separate from the real grid import/export cost figures — they're real money, but not money reflected on the electricity bill

### Fix
- Caught and fixed a real Big-M bug before it shipped: the two session-reward MILP constraints each need a bound big enough to cover the "release" case in *both* directions, not just one — the initial version was infeasible (and reported a negative reward) whenever a slot was genuinely forced above a Power Down baseline, caught by a dedicated regression test built specifically to probe that case

## 3.4.3 - 2026-08-03

### Improvement
- New notification: once daily in the evening, GridLock checks tomorrow's plan for a genuine solar shortfall (reads the LP's own reserve_shortfall, not a hand-rolled load comparison, so it can't false-positive on a normal low-overnight-SoC summer day) and suggests plugging in the EV if solar alone won't cover the reserve

## 3.4.2 - 2026-08-03

### Improvement
- Saving Session join notification now includes a fresh, best-effort plan note — how much of the battery it currently expects to discharge across that specific session's window
- Plan table (and CSV export) now has a "Saving session" column marking every slot that falls inside a joined Saving Session window

## 3.4.1 - 2026-08-03

### Fix
- The 48h plan now respects EV concurrent-charging during dispatch slots (charge rate capped to `ev_concurrent_charge_kw`, battery export blocked) instead of only applying that rule live, to the current slot

## 3.4.0
- **Self-consumption and export now have separate £/kWh cost
  thresholds per mode**, replacing two special cases that made eco and
  max_profit harder to reason about (and, in eco's case, impossible to
  tune at all):
  - `eco` used to hard-block battery export outright (`export_ub`
    forced to 0 regardless of price) — replaced with a soft, much
    higher export-specific threshold (0.25 default). At typical
    Octopus Agile/IOG spreads this still sits above nearly everything,
    so day-to-day behaviour is effectively unchanged, but a genuinely
    exceptional price can now clear it instead of being hard-blocked
    no matter how good it gets. Self-consumption (using the battery
    for your own load) is untouched — same 0.09 default as before.
  - `max_profit` used to hard-force its degradation cost to 0
    regardless of what was configured, self-consumption and export
    alike — sold at literally any positive margin, including a
    fraction of a penny, and silently ignored an explicit override.
    Self-consumption cost is now a clean, respected 0 (unchanged
    behaviour); export gets a small real floor instead (0.03 default)
    — "go ham", but with a tiny buffer against pointless micro-cycling
    rather than zero threshold at all.
  - `balanced` is unchanged — same single 0.15 threshold (from 3.3.2)
    for both self-consumption and export.
  - New optional `export_degradation_cost` in `apps.yaml`, alongside
    the existing `battery_degradation_cost` (which now applies to
    self-consumption specifically).
  - Fixed a bug this change would otherwise have introduced in the
    Risk Profile Comparison panel: its per-mode forecast used
    `dataclasses.replace()` to swap in each mode's degradation cost for
    comparison, but only ever set `degradation`, not
    `export_degradation` — every comparison profile would have
    silently inherited whichever mode is *currently* live's export
    threshold instead of its own. Fixed before it could ship broken.

## 3.3.2
- **Balanced mode's default degradation cost raised from 5p to
  15p/kWh** — the Risk Profile Comparison panel showed balanced
  tracking max_profit within ~6% (£48.22 vs £51.47 forecasted), nowhere
  near eco. Checked directly against a real day's plan (3.5p cheap
  import, mostly 10-24p export): every single export slot in it
  cleared a 5p degradation cost, so balanced was in practice selling
  from the battery almost every day regardless of how good the window
  actually was. Verified before picking the new number — doubling to
  10p (the obvious first guess) only suppresses the weakest ~12 of 51
  real slots; the 15-24p range that makes up most of what was actually
  happening stays comfortably profitable even at 10p. 15p was chosen
  as the number that leaves only the genuinely best few slots per day
  (20-24p+) still clearing it, cutting out the routine daily cycling.
  This is a real trade-off (less forecasted profit for less wear), not
  a free improvement — override with `battery_degradation_cost` in
  `apps.yaml` if you want a different point on that trade-off.

## 3.3.1
- **Fix: a genuine solver timeout could surface as "Engine error"
  instead of the intended safe reserve-infeasible fallback.** 3.2.5
  added a hard time limit to both solver backends specifically so a
  hard MILP instance could never hang the app indefinitely — assumed a
  timeout would always come back as a plain non-optimal status. It
  doesn't, for GLPK specifically: confirmed directly against a real
  glpsol binary that when `--tmlim` cuts the search off *before* it
  ever finds a first feasible solution (as opposed to finding one and
  then running out of time), glpsol exits non-zero and PuLP raises
  `PulpSolverError` rather than reporting a status — which escaped
  `_solve_lp` entirely and hit the much broader "Engine error" handler
  instead, exactly as seen live in production right after 3.2.5 shipped
  (the failsafe still caught it and fell back to safe self-consumption
  correctly — this wasn't a safety issue, just the wrong path with a
  noisier error). `_solve_lp` now catches this and reports
  `infeasible=True` like any other non-optimal status. Also raised the
  solver time limit from 8s to 15s, since the real instance that
  triggered this was genuinely solvable, just slower than 8s allowed —
  still well within a single tick's budget even at 3 solves worst case.

## 3.3.0
- **The fail-safe watchdog (HA core automation + helpers) is now
  installed and kept up to date automatically** — no more manually
  copying `ha_support.yaml` into `/config/packages/` and restarting
  Home Assistant yourself. On every start the add-on writes/refreshes
  `packages/gridlock.yaml` directly into HA core's own config
  directory and asks HA to reload it live (`automation.reload`,
  `input_boolean.reload`, `input_select.reload`), so a brand-new or
  updated watchdog picks up without a full HA restart in the common
  case.
  - Needs a new permission: `homeassistant_config:rw` (read/write
    access to HA's own `/config`, not just this add-on's own
    persistent storage). Supervisor will prompt for approval on
    update; if it's declined or not yet granted, the add-on logs a
    clear warning and skips the sync rather than failing to start —
    the old manual copy-and-restart flow still works as a fallback.
  - Why this matters: this is the safety net specifically meant to
    survive AppDaemon itself hanging (see 3.2.5) — it's independent of
    the AppDaemon process by design, so it can't be something
    AppDaemon has to remember to set up for itself either. Making it
    automatic closes the gap where it could be silently missing (or
    stale) on a real, already-deployed install without anyone
    noticing until an incident needed it.

## 3.2.6
- **Docs only.** Expanded `README.md` (the add-on store's Info-tab
  intro) and `DOCS.md` (the Documentation tab) — feature list and
  hardware/account requirements, a tour of the dashboard's six tabs, a
  table of the three optimiser modes, a reference for the config knobs
  most worth knowing about (`reserve_margin_pct`, `target_daily_net_cost`,
  `floor_soc`, `min_export_pct`, `cheap_rate_threshold`, Storm Watch,
  SSEN), and a troubleshooting section (frozen dashboard/stuck tick,
  red-dot discovery, the real meaning of a Bypass row, the `!secret`
  gotcha). No code changed.

## 3.2.5
- **Fix: the app could hang for hours with no error logged, freezing
  the plan and every dashboard reading it.** Confirmed in production —
  the add-on's own log showed a single AppDaemon worker thread stall
  for over three hours mid-tick, then resume, with zero exception or
  traceback anywhere. Root cause: 3.2.3's PV-routing-priority fix added
  one binary "battery full" variable per slot, turning the solve from a
  pure LP into a MILP — and neither solver backend (GLPK, CBC) had a
  time limit configured, so a hard branch-and-bound instance on real
  data could in principle run indefinitely, blocking the app's single
  worker thread (and therefore every subsequent scheduled tick) until it
  eventually finished. Two changes: (1) both solvers now get a hard
  8-second `timeLimit` — if a solve can't finish in time it returns
  whatever it has (or nothing), which surfaces as `PlanResult.infeasible`
  and falls back to safe self-consumption, the same existing safety net
  already used for a genuinely-unsolvable reserve; (2) the PV-routing
  gate's Big-M coefficient is now `pv[i]` itself (the tightest valid
  bound — `pv_to_grid` can never exceed that slot's own PV anyway)
  instead of an arbitrary `1e5` constant, which was needlessly weakening
  the MILP's relaxation at every branch-and-bound node and is the likely
  reason it was slow to solve at all. A 96-slot solve on realistic data
  now completes in well under a second.

## 3.2.4
- **Fix: plan rows showing "⚠️ Bypass" when the battery fully covered
  load with zero grid import.** The plan table relabeled any ECO slot
  as Bypass purely because its *projected* SoC ended near the floor
  with no forecast PV — it never checked whether the slot actually
  needed the grid. Draining the battery down to the floor by the last
  slot before a cheap recharge is the reserve mechanism working exactly
  as intended, not a failure; only relabel a slot as Bypass when it
  genuinely had to import for load that slot (`grid_kwh > 0`).
- **Fix: "Planned outages" tile showing the literal string `1e-9`
  instead of `0`.** GridLock's own `set_state()` override nudges every
  real zero in a published attributes dict to `1e-9` (a workaround for
  Home Assistant silently dropping true-zero attribute values) — the
  web UI displayed that raw value unrounded, so zero planned outages
  rendered as `1e-9` in the tile's "active outage" colour instead of a
  clean `0` in green. Not a live-data problem — it happens regardless
  of which SSEN postcode is configured.

## 3.2.3
- **Fix: PV surplus could bypass the battery and export directly even
  while there was still headroom to charge.** The LP treated
  `pv_to_battery` vs `pv_to_grid` as a free economic choice — and once
  the on-peak reserve was satisfied, selling PV directly at a decent
  export rate often looked cheaper than storing it for later use, so
  the battery could sit pegged at whatever level overnight charging left
  it (e.g. ~75%) for an entire high-solar day while every kWh of surplus
  PV exported straight past it. That's not actually a choice the
  software gets to make: a Sigenergy inverter running self-consumption
  mode routes surplus PV into the battery until it's full in hardware,
  before any of it can reach the grid — no matter what the plan's own
  arithmetic says is more profitable. `pv_to_grid` is now hard-gated so
  it can only be nonzero once a slot's SoC is genuinely at capacity;
  since there's no "waste PV" variable, the solver's only way to
  balance the energy equation while the battery isn't full is to route
  surplus into `pv_to_batt` instead. A plain Big-M constraint on SoC
  alone isn't sound here (it goes infeasible below full — this needed
  one binary "battery full" indicator per slot instead, so it's
  technically a small MILP now rather than a pure LP for this one
  constraint). Separately verified — with the routing modeled correctly
  — that charging fully overnight from a cheap rate is *still* the
  right call whenever the export rate is decent, not a leftover bug:
  any headroom deliberately left for "free" solar to fill just displaces
  that same solar's real export revenue, which is worse than the small
  overnight import cost. Reducing overnight charge to "just the morning
  gap" on the assumption solar charging is free was the wrong fix; this
  release fixes the actual hardware-modeling gap instead.

## 3.2.2
- **Fix: battery cycling for load during genuinely cheap/off-peak
  slots.** With a fully-charged battery and nothing better to do with
  the charge, the LP still drained it to serve load during cheap
  slots — because battery self-consumption only costs the degradation
  rate in the objective (e.g. 5p/kWh), and that's often *less* than a
  genuinely cheap import rate (e.g. 10p), making the LP "prefer"
  cycling stored charge to "save" a few pence that were never really
  saved: that charge came from the grid at this same cheap rate a slot
  or two earlier (or gets topped up at it again shortly), so routing
  load through the battery instead of importing it directly is a real
  net loss (round-trip efficiency + degradation) for zero benefit. The
  old heuristic had this exact rule ("leave the battery alone and
  import instead" once already in a cheap slot); the LP rewrite dropped
  it. Off-peak load (whenever the battery isn't also charging that
  slot) now comes straight from grid or PV, never the battery.

## 3.2.1
- **New "Battery kWh" plan table column.** The only way to see how much
  battery a slot actually used was to subtract its SoC from the row
  above's — and that's exactly backwards to get wrong: a row's SoC is
  the level *after* that slot's own action, so a slot's real usage is
  (previous row's SoC − this row's SoC), not the other way round. Read
  the wrong direction and the best-priced export slot (which correctly
  sold the most) looks like it barely sold anything, while an ordinary
  ECO slot right after it (not selling anything, just serving a small
  load) looks like a big sale at a cheap price — confirmed against a
  real report that read it exactly backwards. `battery_kwh` is taken
  directly from the optimiser's own per-slot variables and needs no
  diff against any other row to mean something.

## 3.2.0
- **New `reserve_margin_pct` (default 0.15)**: the on-peak reserve
  constraint now holds back extra slack on top of the bare forecasted
  load for the rest of a peak stretch, rather than reserving for
  exactly 100% of a point-estimate. Why: the plan re-solves every 5
  minutes and can't claw back charge an earlier slot already exported
  or discharged — if the learned load forecast for a later slot drifts
  upward *after* an earlier slot already sold against the old, lower
  estimate, that energy is gone, and no amount of "try harder" in a
  later solve recovers it. A reserve built with zero margin cuts
  exactly to the wire against its own forecast being right, which real
  house load rarely is slot to slot — this is a direct, concrete
  response to a real report of the battery being sold down too far and
  landing in Bypass with genuine load still ahead of it. Set it to 0
  to go back to the exact-forecast reserve if your load is very
  predictable and you'd rather have the extra export; raise it further
  if you're still seeing Bypass you don't think should happen.

## 3.1.3
- **Fix: battery draining to empty and hitting Bypass mid-afternoon on a
  day forecast at 30-50+ kWh solar.** Root cause: Solcast only publishes
  a "today" and a "tomorrow" forecast sensor — the 48h horizon (added in
  the LP rewrite) reaches into the day *after* tomorrow for a large part
  of the day from any afternoon/evening "now", which neither sensor
  covers at all. `core/slots.py` was defaulting missing PV data to
  `0.0` — "assume zero solar", the single most pessimistic possible
  planning assumption — for that entire stretch, correctly-by-its-own-
  logic draining the battery for load it (wrongly) believed had no
  solar coming to offset it. Now falls back to the same time-of-day
  from 24h earlier (real Solcast data, already fetched, just for the
  wrong day) whenever Solcast's own coverage runs out, rather than
  inventing a zero.

## 3.1.2
- **Fix: the actual cause of the corrupted Plan tab.** 3.1.1 fixed a
  real bug (a stray NaN able to slip past a truthy-check guard) but it
  wasn't the one causing the reported corruption — confirmed by
  checking a pre-3.1.1 report against that same tick's still-intact
  `plan_html` string: every "missing" plan_table cell, without
  exception, was a value that should have been exactly `0`/`0.0` for
  that slot. Something between AppDaemon's `set_state()` and Home
  Assistant's own state storage silently drops any dict key or list
  element equal to exactly zero (also confirmed independently on
  `sensor.gridlock_solar_forecast`'s forecast points at night, where
  `pv=0` made the whole point vanish rather than read as zero).
  Rather than chase which exact layer does this, `GridLock.set_state()`
  now nudges every such value by a display-invisible epsilon
  (`1e-9`) before publishing — applied once at the single call site
  every sensor this app publishes goes through, so it covers all of
  them, not just plan_table. The web UI's one place that read a
  0/1 flag with a truthy check (the EV-dispatch column) is updated to
  compare numerically instead, since `1e-9` is still truthy in JS.

## 3.1.1
- **Fix: corrupted Plan tab** — real-world report showed the Action/EV kWh/
  SoC/Grid £/Total £ columns full of stray numbers and "NaN". Root cause:
  `pulp.value(x) or 0.0`, used to guard every solver read in
  `core/optimizer.py`, doesn't actually catch `NaN` — `NaN` is truthy in
  Python, so `float('nan') or 0.0` evaluates to `nan`, not `0.0`. A stray
  NaN reaching a published plan_table row correlated with that row
  arriving short by a few values, which silently misaligned every column
  after the gap in the web UI. Fixed at the source (`core/optimizer.py`'s
  `_val()` now explicitly rejects non-finite values, not just `None`) and
  defended in two more places: `gridlock.py` sanitises every plan_table
  cell and now hard-asserts each row's length before publishing it
  (dropping, not corrupting, a slot if this ever recurs), and the web UI
  independently validates row length before rendering. Also removes the
  one previously-intentional `None` in the row (the EV kWh cell) in
  favour of a number plus an explicit new `dispatch` column, since a
  `None` in the row was itself part of what made the row length variable.

## 3.1.0
- **Web UI visual overhaul**: action pill badges (CHARGE/EXPORT/ECO/
  STORM_HOLD/BYPASS) with a glowing warning treatment for bypass;
  price-heatmap-shaded rate columns (relative to your own configured
  `cheap_rate_threshold`, not a fixed pence figure); mini SoC progress
  bars in the plan table instead of raw percentages; KPI sparklines on
  Import/Export/Today-net/Saved-7d/Plan-cost tiles, all from real
  already-computed data (nothing fabricated); a live weather + Solcast
  yield + Storm Watch status widget in the header; an animated "⚠️
  BYPASS ACTIVE" banner and pulsing warning flow-line when the inverter
  drops into bypass; the Forecast tab's solar/SoC chart replaced with 3
  perfectly-aligned stacked charts (PV vs load, SoC curve, rate
  step-bars) sharing one synced hover tooltip; the Tariffs tab is now an
  interactive relative-cost bar comparison; the Entities tab groups
  discovered entities into Battery/Inverter/Grid/EV/Weather/Tariff cards
  with live connection-status dots; Log tab entries get a 🔴 prefix on
  bypass/fault entries.
- **New: switch `eco`/`balanced`/`max_profit` live from the web UI**, no
  AppDaemon restart — a 3-way segmented control in the header now
  actually changes the running strategy (via a new
  `input_select.gridlock_mode_override` helper, auto-created the same
  way `input_boolean.gridlock_enable` already was; "auto" defers to
  apps.yaml's `battery_risk_profile`, unchanged for anyone who never
  touches the control). This is the one functional change in this
  release — everything else above is presentation-only, reading data
  the engine already published.
- New `daily_savings_history` on `sensor.gridlock_savings` (baseline
  minus actual, per day) — feeds the Saved (7d) KPI sparkline; same
  already-tracked numbers `_savings_totals` sums, just kept per-day.
- New `cheap_rate_threshold` on `sensor.gridlock_status` — lets the web
  UI's heatmap/price colouring scale to your actual configured
  threshold instead of guessing at one.

## 3.0.0
- **Replaced the hill-climbing heuristic optimiser with a real linear
  program** (PuLP, GLPK-backed on this add-on's Alpine image). Solves
  the whole 48h/96-slot horizon jointly instead of searching step by
  step — the plan-cost figures should be equal or better than before,
  never worse, for the same rates/PV/load.
- **`battery_risk_profile`'s three values (`eco`/`balanced`/`max_profit`)
  are now real behavioural modes**, not just a degradation-cost scalar
  under one shared behaviour: `eco` now hard-blocks all battery-to-grid
  export (only direct solar surplus can be sold); `balanced` exports
  only where the margin clears the degradation cost, with an optional
  `target_daily_net_cost` cutoff; `max_profit` zeroes the degradation
  cost and sells any profitable margin, preserving only enough SoC for
  load until the next off-peak slot. Existing configs keep working
  unchanged — nothing new to set unless you want the daily-cost cutoff.
- **Hard on-peak reserve constraint**: the LP now guarantees enough SoC
  survives an on-peak stretch to cover the rest of its forecasted load
  before allowing further export/self-consumption drain, rather than the
  old soft export-cap heuristic doing the same job on a best-effort basis.
- **New 15-minute HA/Solcast failsafe**: if the SoC sensor or the
  Solcast forecast (when configured) goes continuously unavailable for
  more than 15 minutes, GridLock drops to local self-consumption
  immediately rather than keep planning against stale data — separate
  from, and faster to react to, the existing broad-exception safe mode.
- **Real-time bypass guardrail**: the inverter's own mode entity is now
  watched directly — an unexpected external change (manual override, a
  fault reverting it to `Unknown`/bypass) triggers an immediate re-plan
  instead of waiting up to 5 minutes for the next scheduled tick.
- **EV load separation**: the learned house-load profile now subtracts
  live EV draw while the car's actually charging, so plug-in events stop
  distorting the learned per-half-hour baseline.
- **Auto-reads nominal battery capacity and a hardware charge/discharge
  rate ceiling** from HA where available (a capacity sensor; the
  charge/discharge `number.*` entities' own declared `max`), clamping a
  misconfigured `apps.yaml` rate to what the inverter actually supports.
- **Multiple sites from one AppDaemon instance**: persisted state
  (learned load profile, savings history, decision log, cost tracking)
  is now namespaced per `apps.yaml` block, so a second `gridlock_<site>:`
  block no longer shares/corrupts the first site's files. An existing
  single-site install's state is migrated across automatically on first
  start under this version — nothing manual needed.
- **Internals split into a pure-Python `core/` package** (optimiser,
  dedup, failsafe, HA entity discovery, inverter/tariff/forecast
  providers) with no AppDaemon/HA dependency, plus a `tests/` unit-test
  suite that runs without a live HA instance
  (`pip install pulp pytest && pytest tests/`).
- **`conserve_battery_for_peak` is superseded** — the LP always paces
  battery use optimally across the whole horizon on its own; the key is
  still parsed (logged, not an error) but no longer changes behaviour.
- Read-only telemetry discovery added for GivTCP/Solis naming
  conventions (capacity/SoC/power) — control (mode switching, charge/
  discharge limits) stays Sigenergy-only; see the README's "Hardware
  support" section for why.

## 2.30.0
- **New `export_rate_kw` setting** — caps how fast EXPORT is allowed
  to sell per slot, separate from `discharge_rate_kw` (self-consumption
  still uses the full hardware rate for real load). Lowering it spreads
  the same total volume across more slots for a gentler peak discharge
  current. Defaults to `discharge_rate_kw` — **no behaviour change
  unless set explicitly**: tested at half rate on a real evening
  export window and found ~24% less total profit, not "the same
  money" — the good export window is only ever so many slots long,
  and once it runs out of slots to spread into, less total volume
  gets sold at a good rate overall. Worth setting deliberately with
  that tradeoff in mind, not as a blind default.

## 2.29.1
- **New "Charge kWh" column**, next to "Grid kWh". Reported directly:
  a CHARGE row showed Grid=4.41kWh against Load=3.91kWh but SoC only
  moved 1 point (1%→2%), which looked like the battery was somehow
  charging far slower than its rated rate. Root cause: CHARGE mode
  never discharges the battery for the concurrent load (it's served
  directly from the grid instead), so "Grid kWh" there was always the
  battery top-up *and* the house load added together — in this exact
  row, only ~0.5kWh of that 4.41kWh actually went into the battery,
  the rest was the load passing straight through. Verified against the
  exact reported numbers: 0.5kWh charge + 3.91kWh load = 4.41kWh,
  matching precisely. The new column isolates just the top-up amount.

## 2.29.0
- **Extended the planning horizon from 24h to 28h.** Root cause of
  the battery still hitting empty (and bypass) before the next
  off-peak window, even after the export-reserve fix in 2.28.0: for
  slots late in the evening (e.g. the export event, or the tail after
  it), the *next* off-peak window sometimes fell just past the exact
  24h boundary — invisible to `next_cheap_idx`/`remaining_deficit`,
  so there was nothing to ration EXPORT or self-consumption against.
  Confirmed directly: a slot at Thu 18:00 (from a plan built at Wed
  23:00) couldn't see the following off-peak window at all under the
  old 24h horizon; under 28h it's visible 5.5h ahead, exactly where
  the reserve logic needs it. The extra 4h always lands in the small
  hours of the next day (PV is correctly ~0 there regardless), and
  stays within Octopus's day-ahead rates and Solcast's forecast
  window for all but the very latest "now" times. ~22ms per plan
  recompute in testing (up from ~16ms), negligible against the
  5-minute tick; plan_table payload grows from ~8KB to ~9.3KB, still
  well under HA's ~16KB attribute limit.

## 2.28.1
- **Bypass mode now shows in the plan table itself**, not just the
  live status line. It was only ever computed inside `apply()` for
  whatever's happening right now — the plan table's row labels (and
  even row 0, "now") never reflected it, so a whole overnight stretch
  genuinely running in bypass would just show plain "ECO" throughout.
  Rows now show **"ECO (Bypass)"** wherever the forecasted SoC for
  that slot is at/near the floor with no PV expected — the same
  condition `apply()` uses live, just evaluated against the forecast
  instead of the live reading, so it's visible for the whole stretch
  it applies to rather than only whichever slot happens to be current.

## 2.28.0
- **EXPORT now reserves enough charge to reach the next off-peak
  window before selling anything further.** Reported directly: the
  evening export event was selling flat-out, and if that left too
  little behind, the following few hours before the next cheap window
  paid real grid cost for ordinary self-consumption. EXPORT now caps
  itself, per slot, at whatever's left over *after* protecting the
  same forecasted self-consumption need the pacing/weighting logic
  already tracks (`remaining_deficit`/`next_cheap_idx`) — it only
  sells genuine surplus beyond that, rather than everything down to
  the floor. No cap at all when there's no off-peak window in sight
  (nothing to reserve for) — a genuinely good, one-off export rate
  still sells flat-out exactly as before.

## 2.27.1
- **Added a fine-grained (0.05kWh) refinement pass after the main
  0.5kWh hill-climb**, to close small residual grid-import gaps the
  coarse step is too blunt to profitably fix. Reported directly: a
  tiny 0.06kWh shortfall at the very edge of the 24h horizon cost
  1.6p imported at peak rate — buying that same amount off-peak
  overnight would only cost ~0.2p, a clear win, but a whole 0.5kWh
  charge step costs more than the 1.6p it'd save (most of the step
  goes unused), so the coarse pass correctly left it alone. The new
  finer pass picks up exactly this kind of small, genuine improvement
  without changing how the coarse pass behaves for everything else.
  Negligible performance cost (~16ms per full plan recompute in
  testing, against a 5-minute tick interval). The hard no-peak-charge
  rule from 2.24.2 applies identically to both passes.

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

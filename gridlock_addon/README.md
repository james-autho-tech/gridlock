# GridLock add-on

Bundles GridLock's AppDaemon planning engine as a standalone Home
Assistant add-on — no separate AppDaemon add-on required.

## What it does

GridLock is a linear-programming battery optimiser for a Sigenergy
inverter running against a half-hourly Octopus tariff (Agile or
Intelligent Octopus Go). Every 5 minutes it re-solves a 48-hour plan —
charge, discharge, export, self-consume — over the whole horizon at
once, rather than deciding slot by slot, weighing import/export rates,
Solcast solar forecasts, learned house load, and battery degradation
against each other before driving the inverter directly.

- **Three modes** — `eco` (the battery only ever self-consumes, never
  sells), `balanced` (sells only where the margin clears degradation
  cost — the default), `max_profit` (sells any real margin above
  round-trip cost).
- **Storm Watch** — charges to a configured target and holds through a
  MeteoAlarm severe-weather warning, overriding the normal plan.
- **EV protection** — pauses or derates battery activity while a
  Hypervolt charging session is active, so the two never fight over
  the same circuit.
- **Live dashboard** (Ingress, no separate port/login) — the full 48h
  plan table, synced forecast charts, a tariff comparison against
  other Octopus products, discovered-entity health, and a decision
  log explaining every action it took and why.
- **Failsafe** — falls back to safe self-consumption if Home Assistant
  or the Solcast link goes stale, or if the solver can't produce an
  answer in time, rather than acting on stale or partial data.
- **Independent watchdog** — a second, HA-core-side safety net that
  reverts the inverter to safe self-consumption if GridLock's own
  heartbeat goes stale, so a hung/crashed AppDaemon process can't leave
  the inverter stuck mid-command. Installed and kept up to date
  automatically, no manual setup.

## Requirements

- A Sigenergy inverter integrated into Home Assistant — this add-on
  reads and writes Sigenergy-specific entities directly (mode,
  charge/discharge limits, SoC).
- An Octopus Energy account on a half-hourly tariff (Agile or
  Intelligent Octopus Go), via the BottlecapDave `octopus_energy`
  integration.
- Solcast solar forecasting — optional, but the planning is
  meaningfully worse without it if you have PV.
- A Hypervolt EV charger — optional.

See [DOCS.md](DOCS.md) for setup and configuration, and
[CHANGELOG.md](CHANGELOG.md) for release history.

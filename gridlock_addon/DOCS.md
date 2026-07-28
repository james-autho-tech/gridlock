# GridLock add-on — setup

On first start the add-on writes a template config to its persistent
storage folder, `/addon_configs/gridlock/` (visible over Samba / the
File editor / Studio Code Server add-ons):

- `apps/gridlock/gridlock.yaml` — model parameters, tariff rates.
  Octopus and Hypervolt entities are **auto-discovered by naming
  pattern at startup** — nothing to set for a single account/meter/
  charger. If discovery is ambiguous (multiple Octopus accounts/
  meters), set the affected key explicitly here as a **literal
  value** — not `!secret`. AppDaemon's app-config loader has no
  built-in secrets.yaml support the way HA core does (confirmed by
  checking how [REDACTED] does it — that's custom code [REDACTED] wrote
  itself, not an AppDaemon feature); a `!secret` tag here makes the
  whole file fail to parse and the app silently stops publishing
  anything. The add-on self-heals from that (backs up the broken
  file, restores the template) but it's simplest to just not use it.

Open the add-on's sidebar panel (once Ingress is enabled via "Show in
sidebar" on this add-on's Info page) — the "Discovered entities" card
shows exactly which entity got picked for each field, with a red dot
for anything not found. That's the fastest way to confirm discovery
worked, or to see what to override if it picked the wrong one.

Edit `gridlock.yaml`/`secrets.yaml` as needed, then **restart the
add-on** to pick up changes.

`gridlock.py` is reset from the add-on image on every start — don't
hand-edit it in `addon_config`, it won't persist across restarts.
Ship code changes through the add-on itself (bump `config.yaml`'s
`version`, tag a release) rather than editing the running container.

Also copy `ha_support.yaml` (from the main repo) to
`/config/packages/gridlock.yaml` and restart Home Assistant — this
add-on only runs the planning engine, the HA-side helpers/watchdog
automation still need to live in HA core.

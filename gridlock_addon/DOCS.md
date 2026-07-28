# GridLock add-on — setup

On first start the add-on writes a template config to its persistent
storage folder, `/addon_configs/gridlock/` (visible over Samba / the
File editor / Studio Code Server add-ons):

- `apps/gridlock/gridlock.yaml` — model parameters, tariff rates,
  `!secret`-referenced entity IDs.
- `secrets.yaml` — put the account-identifying entity IDs here (same
  `!secret` pattern as Home Assistant core, but this file is separate
  from `/config/secrets.yaml`). See the main repo README for the
  exact keys expected (`gridlock_import_rate_entity`, etc).

Edit both, then **restart the add-on** to pick up changes.

`gridlock.py` is reset from the add-on image on every start — don't
hand-edit it in `addon_config`, it won't persist across restarts.
Ship code changes through the add-on itself (bump `config.yaml`'s
`version`, tag a release) rather than editing the running container.

Also copy `ha_support.yaml` (from the main repo) to
`/config/packages/gridlock.yaml` and restart Home Assistant — this
add-on only runs the planning engine, the HA-side helpers/watchdog
automation still need to live in HA core.

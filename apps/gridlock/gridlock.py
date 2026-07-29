import json
import os
import re
import urllib.request

# The Supervisor add-on's config.yaml version is the single source of
# truth (set by `run` via GL_VERSION) — falls back to "dev" for the
# HACS/manual install path, which has no add-on manifest to read.
VERSION = os.environ.get("GL_VERSION") or "dev"

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta, timezone, time as dtime

SLOT_MIN = 30
# 28h, not a clean 24h — a plan built late in the evening needs to see
# past the *next* off-peak window, not just to the same clock time
# tomorrow. With exactly 24h, a slot near the end of the horizon (e.g.
# during an evening export event) can find the next off-peak window
# sitting just past the boundary, invisible to next_cheap_idx/
# remaining_deficit — so EXPORT and self-consumption pacing have
# nothing to ration against and can leave too little reserve to reach
# it. The extra 4h always lands in the small hours of the following
# day (PV is correctly ~0 there regardless of forecast availability),
# and stays within Octopus's day-ahead published rates and Solcast's
# today/tomorrow forecast for all but the very latest "now" times.
HORIZON_SLOTS = 56  # 28h
RISK_PROFILES = {"eco": 0.09, "balanced": 0.05, "max_profit": 0.01}


class GridLock(hass.Hass):

    # ------------------------------------------------------------------
    # INIT
    # ------------------------------------------------------------------
    def initialize(self):
        self.log(f"=== GRIDLOCK {VERSION} PLANNING ENGINE STARTING ===")

        a = self.args
        # Entity overrides from the Supervisor add-on's Configuration
        # tab (written by run to addon_overrides.json) — a UI form for
        # the fields most likely to need fixing if auto-discovery picks
        # the wrong entity, without hand-editing apps.yaml. Checked
        # after apps.yaml's own explicit values, before discovery.
        self.overrides = self._load_addon_overrides()

        # Hardware
        self.ent_mode = a["sigen_mode"]
        self.ent_disch_limit = a["sigen_discharge_limit"]
        self.ent_charge_limit = a["sigen_charge_limit"]
        self.ent_soc = a["sigen_soc"]

        # Inputs
        self.ent_ev = (a.get("ev_charging") or self.overrides.get("ev_charging_override")
                       or self._find_hypervolt_charging())

        # Octopus entities embed your account/MPAN in the entity_id
        # itself, so instead of requiring them in config, discover them
        # from the entity registry by naming pattern (BottlecapDave's
        # Octopus Energy integration convention). Explicit config (or a
        # secrets.yaml !secret ref) still wins if you set one — needed
        # if you have multiple Octopus accounts/meters and discovery
        # would be ambiguous.
        # Dispatch/saving-events naming varies by integration version —
        # some setups use "octopus_energy_a_<account>_", newer/device-
        # linked ones use "octopus_energy_<device-uuid>_" — so match
        # broadly on the stable suffix and let the live-state tiebreak
        # in _find_entity sort out any dead/restored duplicates.
        self.ent_dispatch = (a.get("octopus_dispatch")
                             or self.overrides.get("octopus_dispatch_override")
                             or self._find_entity(
            prefix="binary_sensor.octopus_energy_", suffix="_intelligent_dispatching"))
        self.ent_import_rate = (a.get("import_rate")
                                or self.overrides.get("import_rate_override")
                                or self._find_entity(
            prefix="sensor.octopus_energy_electricity_", suffix="_current_rate", avoid="export"))
        self.ent_export_rate = (a.get("export_rate")
                                or self.overrides.get("export_rate_override")
                                or self._find_entity(
            prefix="sensor.octopus_energy_electricity_", suffix="_export_current_rate"))
        self.ent_saving_events = (a.get("octopus_saving_events")
                                  or self.overrides.get("octopus_saving_events_override")
                                  or self._find_entity(
            prefix="event.octopus_energy_", suffix="_octoplus_saving_session_events"))

        # Rate curve sources (BottlecapDave rate events) — matched off
        # the import/export rate entities' account/MPAN stem, trying
        # both known naming variants (see _find_sibling docstring).
        import_stem = self._mpan_stem(self.ent_import_rate, "_current_rate")
        export_stem = self._mpan_stem(self.ent_export_rate, "_export_current_rate")
        self.ent_rates = [e for e in [
            a.get("import_rates_previous") or self._find_sibling(
                import_stem, "event", ["_previous_day_rates"]),
            a.get("import_rates_today") or self._find_sibling(
                import_stem, "event", ["_current_day_rates"]),
            a.get("import_rates_tomorrow") or self._find_sibling(
                import_stem, "event", ["_next_day_rates"]),
        ] if e]
        self.ent_export_rates = [e for e in [
            a.get("export_rates_today") or self._find_sibling(
                export_stem, "event", ["_export_current_day_rates", "_current_day_rates"]),
            a.get("export_rates_tomorrow") or self._find_sibling(
                export_stem, "event", ["_export_next_day_rates", "_next_day_rates"]),
        ] if e]

        # Solcast detailed curves
        self.ent_solcast = [e for e in [
            a.get("solcast_detail_today",
                  "sensor.solcast_pv_forecast_forecast_today"),
            a.get("solcast_detail_tomorrow",
                  "sensor.solcast_pv_forecast_forecast_tomorrow")] if e]

        # Load — a discovered/configured power sensor (kW) gets sampled
        # every tick and blended into a learned per-half-hour-of-day
        # profile (persisted to disk), which _load_kwh() prefers once a
        # slot has data. typical_daily_house_kwh / load_hourly_weights
        # remain the fallback for slots not learned yet.
        self.ent_load_power = (a.get("load_power_entity")
                               or self.overrides.get("load_power_entity_override")
                               or self._find_load_entity())
        self.load_profile = self._load_load_profile()
        self.decision_log = self._load_decision_log()

        # Savings tracking: a shadow self-consumption-only "baseline"
        # battery, run tick-by-tick alongside the real one using the
        # same real PV/load/rate readings — the gap between what that
        # baseline would have cost and what actually got paid (from the
        # existing real net-cost sensor) is what GridLock's active
        # scheduling is worth. Resets daily; history persisted so
        # week/month totals survive restarts.
        self._last_actual_energy_cost = 0.0
        self.savings_day = None
        self.baseline_soc = None
        self.baseline_cost_today = 0.0
        self.savings_history = self._load_savings_history()
        # Plan accuracy: snapshot the freshly-computed plan's 24h grid
        # cost forecast on the first tick of each day, then once that
        # day ends, file it alongside the real outcome already in
        # savings_history — "here's what the plan predicted that
        # morning vs what actually happened", no invented accuracy
        # score, just the two real numbers side by side.
        self.plan_accuracy_day = None
        self.day_start_forecast = 0.0
        self._load_savings_state()

        # Live power-flow entities (Solar/Grid/Battery/Home diagram in
        # the Ingress web UI) — magnitude from power sensors, direction
        # from the boolean sensors rather than trusting a sign
        # convention we can't verify across inverter integrations.
        self.ent_pv_power_entities = a.get("pv_power_entities") or self._find_sigen_pv_power()
        self.ent_grid_power = (a.get("grid_power_entity")
                               or self.overrides.get("grid_power_entity_override")
                               or self._find_sigen_power("grid"))
        self.ent_battery_power = (a.get("battery_power_entity")
                                  or self.overrides.get("battery_power_entity_override")
                                  or self._find_sibling(
            self._mpan_stem(self.ent_soc, "_state_of_charge"), "sensor", ["_power"])
                                  or self._find_sigen_power("battery"))
        self.ent_pv_generating = self._find_sigen_binary("pv_generating")
        self.ent_importing = self._find_sigen_binary("importing_from_grid")
        self.ent_exporting = self._find_sigen_binary("exporting_to_grid")
        self.ent_battery_charging = self._find_sigen_binary("battery_charging")
        self.ent_battery_discharging = self._find_sigen_binary("battery_discharging")
        self.ent_ev_power = (a.get("ev_power_entity")
                             or self.overrides.get("ev_power_entity_override")
                             or self._find_entity(prefix="sensor.", contains="hypervolt_ev_power")
                             or self._find_hypervolt_ev_power())
        self.ent_inverter_temp = (a.get("inverter_temp_entity")
                                  or self.overrides.get("inverter_temp_entity_override")
                                  or self._find_sigen_temp("pcs", exclude=["cell", "battery"])
                                  or self._find_sigen_temp("inverter", exclude=["cell", "battery"]))
        self.ent_battery_temp = (a.get("battery_temp_entity")
                                 or self.overrides.get("battery_temp_entity_override")
                                 or self._find_sigen_temp("cell"))
        self.ent_battery_soh = (a.get("battery_soh_entity")
                                or self.overrides.get("battery_soh_entity_override")
                                or self._find_sigen_soh())
        self.ent_discharge_cutoff = (a.get("discharge_cutoff_entity")
                                     or self.overrides.get("discharge_cutoff_entity_override")
                                     or self._find_sigen_discharge_cutoff())

        # Parameters
        self.battery_kwh = float(a.get("battery_capacity_kwh", 10.0))
        self.daily_house_kwh = float(a.get("typical_daily_house_kwh", 12.0))
        self.load_weights = a.get("load_hourly_weights")  # optional list[24]
        self.efficiency = float(a.get("inverter_efficiency", 0.90))
        # battery_risk_profile picks a sensible default degradation cost
        # (£/kWh discharged — the optimiser's only real lever against
        # cycling the battery for wafer-thin arbitrage margins); an
        # explicit battery_degradation_cost always wins over the
        # profile if both are set. These are reasoned defaults, not a
        # real wear model — there's no solid Sigenergy degradation-vs-
        # cycle-depth data to build a genuine SoH-driven one from.
        self.battery_risk_profile = str(a.get("battery_risk_profile", "balanced")).lower()
        if self.battery_risk_profile not in RISK_PROFILES:
            self.log(f"Unknown battery_risk_profile {self.battery_risk_profile!r} "
                     "— falling back to 'balanced'.", level="WARNING")
            self.battery_risk_profile = "balanced"
        self.degradation = float(a.get("battery_degradation_cost",
                                       RISK_PROFILES[self.battery_risk_profile]))
        self.floor_soc = float(a.get("floor_soc", 20.0))
        self.charge_kw = float(a.get("charge_rate_kw", 10.0))
        self.discharge_kw = float(a.get("discharge_rate_kw", 10.0))
        self.ev_concurrent_charge_kw = float(a.get("ev_concurrent_charge_kw", 5.0))
        self.cheap_rate = float(a.get("cheap_rate_threshold", 0.10))
        self.min_export_pct = float(a.get("min_export_pct", 5.0))
        # Default (False): use whatever's in the battery for self-
        # consumption immediately, slot by slot - if there's charge
        # above the floor, spend it on this slot's load rather than
        # rationing it for a hypothetically-better-value slot later.
        # Set True to instead hold some back during an already-cheap
        # slot / ration it across a stretch heading toward a future
        # off-peak window, spending it only where the math says it's
        # worth the most - cheaper in aggregate, but means slots with
        # charge sitting right there can still show a grid cost.
        self.conserve_battery = bool(a.get("conserve_battery_for_peak", False))
        # Max rate EXPORT is allowed to sell at, per slot - separate
        # from discharge_rate_kw (the hardware limit self-consumption
        # can still use in full when real load needs it). Defaults to
        # the same as discharge_rate_kw (no change in behaviour) —
        # lowering it spreads the same volume across more slots for a
        # gentler peak discharge current, but this is a real financial
        # tradeoff, not a free win: the good export window is only ever
        # so many slots long, and once it runs out of slots to spread
        # into, less total volume gets sold at a good rate overall.
        # Tested at half rate on a real evening window: ~24% less
        # profit, not "the same money" — set deliberately, not blindly.
        self.export_rate_kw = float(a.get("export_rate_kw", self.discharge_kw))
        self.default_import = float(a.get("default_import_rate", 0.2839))
        self.default_export = float(a.get("default_export_rate", 0.15))
        self.export_margin = float(a.get("export_margin", 0.02))

        # Mode strings (Sigenergy EMS)
        self.mode_charge = a.get("mode_charge", "Command Charging (Grid First)")
        self.mode_discharge = a.get("mode_discharge", "Command Discharging (PV First)")
        self.mode_eco = a.get("mode_eco", "Maximum Self Consumption")

        # Storm Watch — accepts a single entity, or a list of
        # {entity, severity: [...]} dicts for per-source severity filters
        default_sev = a.get("storm_watch_severity", ["Extreme"])
        raw = a.get("storm_watch_entity") or self.overrides.get("storm_watch_entity_override")
        self.storm_sources = []
        for item in (raw if isinstance(raw, list) else [raw] if raw else []):
            if isinstance(item, dict):
                self.storm_sources.append(
                    (item.get("entity"), item.get("severity", default_sev)))
            else:
                self.storm_sources.append((item, default_sev))
        self.storm_target_soc = float(a.get("storm_watch_target_soc", 100.0))

        # Proactive notifications — always to HA's persistent_notification
        # (works for everyone, no setup), and also to a specific notify
        # service (e.g. notify.mobile_app_yourphone) if configured. Only
        # on genuine transitions (event just started), not every 5-min
        # tick it continues.
        self.notify_service = a.get("notify_service")
        self._prev_storm_active = False
        self._prev_ev_protection = False

        # SSEN Power Track — engine polls the open API directly
        self.ssen_postcode = str(
            a.get("ssen_postcode") or self.overrides.get("ssen_postcode_override") or ""
        ).upper().strip()
        self.ssen_url = a.get(
            "ssen_api_url",
            "https://external.distribution.prd.ssen.co.uk"
            "/opendataportal-prd/v4/api/getallfaults")
        self.ssen_state = {"local": 0, "planned": 0, "severe": False,
                           "faults": []}

        # Comparison tariffs (see apps.yaml)
        self.compare_tariffs = a.get("compare_tariffs", [])

        # Daily financials — both matched off their own MPAN's stem.
        # Export used to be explicit-only on the assumption Octopus
        # doesn't expose an accumulative-cost sibling for export MPANs
        # the same way it does for import ones — unverified, and wrong
        # if it does exist: "Today net" would silently ignore all
        # export credit and just default to 0 rather than error, so a
        # wrong assumption here fails silently instead of loudly.
        self.ent_daily_import_cost = a.get("daily_import_cost_entity") or self._find_sibling(
            import_stem, "sensor", ["_current_accumulative_cost"])
        self.ent_daily_export_value = (a.get("daily_export_value_entity")
                                       or self.overrides.get("daily_export_value_entity_override")
                                       or self._find_sibling(
            export_stem, "sensor", ["_current_accumulative_cost"]))
        self.ent_daily_standing_charge = a.get("daily_standing_charge_entity") or self._find_sibling(
            import_stem, "sensor", ["_current_standing_charge"])

        # GridLock's own running total of import/export cost, tracked
        # every tick from live grid power + direction + rates — used
        # whenever the Octopus sensors above are missing/unavailable
        # (get_float_state's default kicks in), and always published
        # alongside the real figures so the difference is visible
        # rather than silently substituted.
        self.cost_tracking_day = None
        self.tracked_import_cost_today = 0.0
        self.tracked_export_value_today = 0.0
        self._load_cost_tracking_state()

        self.plan = []          # list of slot dicts after optimisation
        self.plan_built_at = None

        if not self.entity_exists("input_boolean.gridlock_enable"):
            self.log("Create input_boolean.gridlock_enable as a real HA helper "
                     "(Settings > Devices > Helpers). Falling back to virtual "
                     "entity; the UI toggle will NOT work until you do.",
                     level="WARNING")
            self.set_state("input_boolean.gridlock_enable", state="on")

        # Triggers
        if self.ent_ev:
            self.listen_state(self.on_trigger, self.ent_ev)
        else:
            self.log("Could not discover a Hypervolt charging switch — set "
                      "ev_charging explicitly in apps.yaml if you have "
                      "an EV charger to protect.", level="WARNING")
        if self.ent_dispatch:
            self.listen_state(self.on_trigger, self.ent_dispatch)
        else:
            self.log("Could not discover an octopus_dispatch entity — set it "
                      "explicitly in apps.yaml if you use Intelligent "
                      "Octopus Go.", level="WARNING")
        self.listen_state(self.on_trigger, "input_boolean.gridlock_enable")
        if self.entity_exists("input_boolean.gridlock_storm_watch"):
            self.listen_state(self.on_trigger, "input_boolean.gridlock_storm_watch")
        for ent, _ in self.storm_sources:
            if ent and self.entity_exists(ent):
                self.listen_state(self.on_trigger, ent)
            elif ent:
                self.log(f"Storm Watch entity '{ent}' not found",
                         level="WARNING")
        if self.ent_saving_events and self.entity_exists(self.ent_saving_events):
            self.listen_state(self.on_saving_event, self.ent_saving_events)
            self.check_and_join_sessions()
        elif self.ent_saving_events:
            self.log(f"Saving Sessions entity '{self.ent_saving_events}' "
                     "not found", level="WARNING")

        if self.ssen_postcode:
            self.run_every(self.poll_ssen, "now", 300)

        # National Grid ESO's carbon intensity API — free, public, no
        # postcode/auth needed, so always on. Informational only (shown
        # on the Forecast tab), not fed into cost planning: turning a
        # gCO2/kWh figure into a £ trade-off would need an arbitrary
        # conversion rate with no solid basis to pick one from.
        self.run_every(self.poll_carbon_intensity, "now", 1800)

        # Re-plan every 5 min, financials piggyback on the same tick
        self.run_every(self.tick, "now", 300)

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------
    def _all_states_flat(self):
        """Flat {entity_id: state_dict} for every entity currently known
        to HA, regardless of which shape self.get_state() returns."""
        try:
            states = self.get_state() or {}
        except Exception:  # noqa: BLE001 — discovery is best-effort
            return {}
        flat = {}
        for key, value in states.items():
            if isinstance(value, dict) and "state" not in value:
                flat.update(value)  # {domain: {entity_id: {...}}}
            else:
                flat[key] = value  # already flat {entity_id: {...}}
        return flat

    def _all_entity_ids(self):
        return list(self._all_states_flat().keys())

    @staticmethod
    def _is_live(state_obj):
        state = str((state_obj or {}).get("state", "")).lower()
        return state not in ("", "unavailable", "unknown")

    def _find_entity(self, prefix=None, suffix=None, contains=None, avoid=None):
        """Discover a single entity_id by prefix/suffix/substring, e.g.
        for the Octopus Energy integration's account/MPAN-in-entity_id
        naming. Some setups end up with a dead/restored duplicate
        alongside the live entity (e.g. after re-linking an integration)
        — when multiple names match, prefer ones that aren't
        unavailable/unknown before falling back to "just pick one"."""
        flat = self._all_states_flat()
        matches = [eid for eid in flat
                   if (not prefix or eid.startswith(prefix))
                   and (not suffix or eid.endswith(suffix))
                   and (not contains or contains in eid)
                   and (not avoid or avoid not in eid)]
        if not matches:
            return None
        live = [eid for eid in matches if self._is_live(flat.get(eid))]
        pool = live or matches
        if len(pool) > 1:
            self.log(f"Multiple entities match (prefix={prefix!r} "
                     f"suffix={suffix!r} contains={contains!r}): {pool} — "
                     "set it explicitly in apps.yaml/secrets.yaml to "
                     "disambiguate.", level="WARNING")
        return pool[0]

    def _find_hypervolt_charging(self):
        """Discover a Hypervolt charging switch — naming varies (some
        installs suffix with a device id), so match loosely rather than
        by a fixed prefix/suffix like the Octopus entities."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("switch.") and "hypervolt" in eid]
        charging = [eid for eid in candidates if "charging" in eid]
        pool = charging or candidates
        live = [eid for eid in pool if self._is_live(flat.get(eid))]
        pool = live or pool
        if not pool:
            return None
        if len(pool) > 1:
            self.log(f"Multiple Hypervolt charging switches found {pool} — "
                     "set ev_charging explicitly in apps.yaml.",
                     level="WARNING")
            return pool[0]
        if not charging:
            self.log(f"No 'charging' switch found among Hypervolt entities; "
                     f"using {pool[0]} as a best guess for ev_charging — "
                     "verify this is correct.", level="WARNING")
        return pool[0]

    def _find_load_entity(self):
        """Discover a house-load power sensor — naming varies by
        inverter integration, so match loosely on keywords rather than
        a fixed prefix/suffix. The sampling logic needs an
        instantaneous power (kW) reading, not a cumulative/daily
        energy total, so "_power" entities are preferred when both
        kinds show up (e.g. Sigenergy exposes total_load_power
        alongside daily_load_consumption/total_load_consumption)."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and "sigen" in eid
                      and ("load" in eid or "consumption" in eid)]
        power_candidates = [eid for eid in candidates if "power" in eid]
        pool_source = power_candidates or candidates
        live = [eid for eid in pool_source if self._is_live(flat.get(eid))]
        pool = live or pool_source
        if not pool:
            return None
        if len(pool) > 1:
            self.log(f"Multiple candidate load-power entities found {pool} "
                     "— set load_power_entity explicitly in apps.yaml.",
                     level="WARNING")
        return pool[0]

    def _find_sigen_power(self, keyword):
        """Single sigen sensor whose entity_id contains "power" +
        keyword — for the flow diagram's grid/battery power readings."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and "sigen" in eid
                      and "power" in eid and keyword in eid]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        return pool[0] if pool else None

    def _find_sigen_pv_power(self):
        """PV power entities to sum for the flow diagram. Real-world
        find: Sigenergy exposes BOTH an aggregate (sigen_plant_pv_power
        / sigen_inverter_pv_power) AND per-string pv1-4_power sensors
        simultaneously — summing everything double/triple-counts, so
        prefer a single aggregate (no digit before "_power") and only
        fall back to summing per-string ones if no aggregate exists."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and "sigen" in eid
                      and "pv" in eid and "power" in eid]
        aggregates = [eid for eid in candidates if not re.search(r"pv\d_power", eid)]
        if not aggregates:
            return candidates  # only per-string sensors exist — sum them
        plant = [eid for eid in aggregates if "plant" in eid]
        pool = plant or aggregates
        if len(pool) > 1:
            self.log(f"Multiple aggregate PV power entities found {pool} — "
                     f"using {pool[0]}; set pv_power_entities explicitly "
                     "in apps.yaml if wrong.", level="WARNING")
        return [pool[0]]

    def _find_sigen_temp(self, keyword, exclude=None):
        """Single sigen temperature sensor matching keyword — inverter
        and battery efficiency both fall off at high temperature, so
        this is shown alongside the solar forecast as a sanity check
        on it, not fed into the forecast numbers themselves (no solid
        derating curve to calculate that from). Real-world find: every
        Sigenergy entity is namespaced "sigen_inverter_..." regardless
        of which subsystem it's actually about (e.g. battery SoC is
        "sigen_inverter_battery_state_of_charge"), so "inverter" alone
        matches everything and isn't a useful filter — exclude lets a
        caller rule out keywords from a different sensor's search."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and "sigen" in eid
                      and "temperature" in eid and keyword in eid
                      and not any(x in eid for x in (exclude or []))]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        return pool[0] if pool else None

    def _find_sigen_soh(self):
        """Battery State of Health — prefer the plant-level aggregate
        (sigen_plant_battery_state_of_health) over the per-inverter one
        for the same reason _find_sigen_pv_power prefers the aggregate:
        one clear number rather than several near-identical readings."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and "sigen" in eid
                      and "state_of_health" in eid]
        plant = [eid for eid in candidates if "plant" in eid]
        pool = plant or candidates
        live = [eid for eid in pool if self._is_live(flat.get(eid))]
        pool = live or pool
        return pool[0] if pool else None

    def _find_sigen_discharge_cutoff(self):
        """The inverter's OWN hardware-level discharge floor (number.
        sigen_plant_ess_discharge_cut_off_state_of_charge) — separate
        from, and currently unrelated to, this app's own floor_soc
        planning parameter. Without this, floor_soc is only ever
        enforced in software (this app deciding what to command each
        tick) with no hardware backstop if the tick loop ever hangs or
        crashes mid-command."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("number.") and "sigen" in eid
                      and "discharge" in eid and ("cut_off" in eid or "cutoff" in eid)]
        plant = [eid for eid in candidates if "plant" in eid]
        pool = plant or candidates
        live = [eid for eid in pool if self._is_live(flat.get(eid))]
        pool = live or pool
        return pool[0] if pool else None

    def _find_sigen_binary(self, keyword):
        """Single sigen binary_sensor matching keyword — used for flow
        direction (charging/discharging, importing/exporting) instead
        of trusting a power sensor's sign convention."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("binary_sensor.") and "sigen" in eid
                      and keyword in eid]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        return pool[0] if pool else None

    def _find_hypervolt_ev_power(self):
        """Fallback if the exact "hypervolt_ev_power" name doesn't match
        (e.g. a device-id infix) — Hypervolt also exposes several other
        power sensors (ct/generation/grid/house/session), so require
        both "ev" and "power" rather than "power" alone."""
        flat = self._all_states_flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and "hypervolt" in eid
                      and "power" in eid and "ev" in eid]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        if len(pool) > 1:
            self.log(f"Multiple candidate EV power entities found {pool} "
                     "— set ev_power_entity explicitly in apps.yaml.",
                     level="WARNING")
        return pool[0] if pool else None

    def _load_profile_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "load_profile.json")

    def _load_load_profile(self):
        try:
            with open(self._load_profile_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_load_profile(self):
        try:
            with open(self._load_profile_path(), "w") as f:
                json.dump(self.load_profile, f)
        except OSError:
            pass

    def _update_load_profile(self, now):
        """Blend a tick's power reading into the learned per-half-hour-
        of-day average kWh, exponentially — so it adapts over ~days
        without one anomalous reading throwing a slot off."""
        if not self.ent_load_power:
            return
        kw = self.get_float_state(self.ent_load_power, None)
        if kw is None:
            return
        slot_idx = str(now.hour * 2 + (1 if now.minute >= 30 else 0))
        observed_slot_kwh = kw * (5 / 60) * 6  # this tick covers ~5 min of a 30-min slot
        alpha = 0.05
        prev = self.load_profile.get(slot_idx)
        self.load_profile[slot_idx] = (observed_slot_kwh if prev is None
                                        else prev * (1 - alpha) + observed_slot_kwh * alpha)
        self._save_load_profile()

    def _savings_state_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "savings_state.json")

    def _load_savings_state(self):
        """Today-in-progress baseline accumulator — separate from
        savings_history.json (finalised past days) so a restart
        mid-day doesn't lose today's partial progress."""
        try:
            with open(self._savings_state_path()) as f:
                state = json.load(f)
            self.savings_day = state.get("day")
            self.baseline_soc = state.get("baseline_soc")
            self.baseline_cost_today = state.get("baseline_cost_today", 0.0)
            self.plan_accuracy_day = state.get("plan_accuracy_day")
            self.day_start_forecast = state.get("day_start_forecast", 0.0)
        except (OSError, ValueError):
            pass

    def _save_savings_state(self):
        try:
            with open(self._savings_state_path(), "w") as f:
                json.dump({"day": self.savings_day,
                           "baseline_soc": self.baseline_soc,
                           "baseline_cost_today": self.baseline_cost_today,
                           "plan_accuracy_day": self.plan_accuracy_day,
                           "day_start_forecast": self.day_start_forecast}, f)
        except OSError:
            pass

    def _savings_history_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "savings_history.json")

    def _load_savings_history(self):
        try:
            with open(self._savings_history_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_savings_history(self):
        try:
            with open(self._savings_history_path(), "w") as f:
                json.dump(self.savings_history, f)
        except OSError:
            pass

    def _read_live_kw(self, entities):
        """Sum of live power readings (kW) across one entity or a list
        — same "assume the entity's native unit is already kW" as the
        rest of this file's live reads (Sigenergy reports these that
        way); mirrors _update_load_profile's approach rather than
        introducing separate W/kW normalisation."""
        if isinstance(entities, str):
            entities = [entities]
        total, got_any = 0.0, False
        for e in (entities or []):
            v = self.get_float_state(e, None)
            if v is not None:
                total += abs(v)
                got_any = True
        return total if got_any else None

    def _roll_savings_day(self, now):
        today_iso = now.date().isoformat()
        if self.savings_day is None:
            self.savings_day = today_iso
            self.baseline_cost_today = 0.0
            self.baseline_soc = self.get_float_state(self.ent_soc, 50.0)
            return
        if today_iso == self.savings_day:
            return
        # Day rolled over — file yesterday's numbers into history using
        # whatever the real net-cost sensor last read before midnight,
        # then start today fresh (baseline SoC reseeded from the real
        # SoC so a shadow-simulation error can't compound forever).
        # Merge rather than replace — _track_plan_accuracy/_track_
        # profile_comparison may already have written a "forecast" or
        # "profile_comparison" key for this same date earlier today.
        self.savings_history.setdefault(self.savings_day, {}).update({
            "actual": round(self._last_actual_energy_cost, 4),
            "baseline": round(self.baseline_cost_today, 4)})
        self.savings_history = dict(list(self.savings_history.items())[-400:])
        self._save_savings_history()
        self.savings_day = today_iso
        self.baseline_cost_today = 0.0
        self.baseline_soc = self.get_float_state(self.ent_soc, 50.0)

    def _update_savings(self, now):
        """One tick of a shadow self-consumption-only battery, driven
        by the same real PV/load/rate readings as everything else —
        the gap between what that hypothetical would have cost and
        what was actually paid (from the real net-cost sensor) is
        what GridLock's active scheduling is worth today."""
        self._roll_savings_day(now)
        pv_kw = self._read_live_kw(self.ent_pv_power_entities)
        load_kw = self._read_live_kw(self.ent_load_power)
        if pv_kw is None or load_kw is None:
            return

        imp_rate = self.get_float_state(self.ent_import_rate, self.default_import)
        exp_rate = self.get_float_state(self.ent_export_rate, self.default_export)
        dt_h = 5 / 60
        eff, cap = self.efficiency, self.battery_kwh
        floor_kwh = self.floor_soc / 100.0 * cap
        max_c, max_d = self.charge_kw * dt_h, self.discharge_kw * dt_h

        batt = self.baseline_soc / 100.0 * cap
        pv, load = pv_kw * dt_h, load_kw * dt_h
        grid_in = grid_out = extra_cost = 0.0

        pv_to_load = min(pv, load)
        pv -= pv_to_load
        load -= pv_to_load

        room = max(0.0, min((cap - batt) / eff, max_c))
        pv_c = min(pv, room)
        batt += pv_c * eff
        grid_out += pv - pv_c
        if load > 0:
            avail = min(max_d, max(0.0, batt - floor_kwh))
            d = min(load / eff, avail)
            batt -= d
            load -= d * eff
            grid_in += load
            extra_cost += d * self.degradation

        self.baseline_soc = max(0.0, min(100.0, batt / cap * 100.0))
        self.baseline_cost_today += grid_in * imp_rate - grid_out * exp_rate + extra_cost
        self._save_savings_state()
        self._publish_savings(now)

    def _savings_totals(self, now):
        today = round(self.baseline_cost_today - self._last_actual_energy_cost, 2)
        week = month = all_time = today
        for date_str, d in self.savings_history.items():
            try:
                age_days = (now.date() - datetime.fromisoformat(date_str).date()).days
            except ValueError:
                continue
            net = d.get("baseline", 0.0) - d.get("actual", 0.0)
            all_time += net
            if age_days < 7:
                week += net
            if age_days < 30:
                month += net
        return today, round(week, 2), round(month, 2), round(all_time, 2)

    def _track_plan_accuracy(self, now, grid_cost):
        today_iso = now.date().isoformat()
        if self.plan_accuracy_day == today_iso:
            return
        if self.plan_accuracy_day is not None and self.plan_accuracy_day in self.savings_history:
            self.savings_history[self.plan_accuracy_day]["forecast"] = round(self.day_start_forecast, 4)
            self._save_savings_history()
        self.plan_accuracy_day = today_iso
        self.day_start_forecast = grid_cost
        self._track_profile_comparison(now)
        self._save_savings_state()

    def _track_profile_comparison(self, now):
        """Once daily: what each risk profile's own morning plan
        predicts for today, using the same real rates/PV/load. Not a
        real-outcome backtest — that would mean running the full
        optimiser continuously for all three profiles rather than just
        the active one, too expensive to do every 5 minutes — but a
        genuine same-morning comparison rather than a guess, and cheap
        since it only runs once a day."""
        real_degradation = self.degradation
        soc0 = self.get_float_state(self.ent_soc, 50.0)
        comparison = {}
        for name, deg in RISK_PROFILES.items():
            self.degradation = deg
            try:
                slots = self.build_slots(now)
                _, _, _, _, gc = self.optimise(slots, soc0)
                comparison[name] = round(gc, 2)
            except Exception as exc:  # noqa: BLE001 — one profile failing shouldn't break the tick
                self.log(f"Profile comparison failed for {name!r}: {exc!r}", level="WARNING")
            finally:
                self.degradation = real_degradation
        today_iso = now.date().isoformat()
        self.savings_history.setdefault(today_iso, {})["profile_comparison"] = comparison
        self._save_savings_history()

    def _publish_savings(self, now):
        today, week, month, all_time = self._savings_totals(now)
        # Last 28 finalised days' real spend, for the Forecast tab's
        # daily cost chart — today's still-in-progress figure isn't
        # included (it's a partial day, not comparable to full ones).
        history = sorted(
            ({"date": d, "cost": round(v.get("actual", 0.0), 2)}
             for d, v in self.savings_history.items()),
            key=lambda p: p["date"])[-28:]
        # Most recent finished day with both a morning forecast and a
        # real outcome on record — plain numbers side by side, not an
        # invented accuracy score.
        accuracy = None
        for d in sorted(self.savings_history.keys(), reverse=True):
            v = self.savings_history[d]
            if "forecast" in v and "actual" in v:
                accuracy = {"date": d, "forecast": round(v["forecast"], 2),
                           "actual": round(v["actual"], 2)}
                break
        # Profile comparison: each risk profile's own morning forecast,
        # day by day, plus a running total across every recorded day —
        # which one actually trends best isn't obvious from a single
        # day, this is what answers that over time.
        profile_days = sorted(
            ((d, v["profile_comparison"]) for d, v in self.savings_history.items()
             if "profile_comparison" in v),
            key=lambda p: p[0])
        profile_totals = {}
        for _, pc in profile_days:
            for name, val in pc.items():
                profile_totals[name] = profile_totals.get(name, 0.0) + val
        profile_totals = {k: round(v, 2) for k, v in profile_totals.items()}
        profile_history = [{"date": d, **pc} for d, pc in profile_days[-28:]]

        self.set_state("sensor.gridlock_savings", state=f"{today:.2f}",
                       attributes={"friendly_name": "GridLock Savings",
                                   "unit_of_measurement": "£",
                                   "icon": "mdi:piggy-bank",
                                   "today": today, "week": week,
                                   "month": month, "all_time": all_time,
                                   "daily_cost_history": history,
                                   "plan_accuracy": accuracy,
                                   "profile_comparison_history": profile_history,
                                   "profile_comparison_totals": profile_totals})

    def _cost_tracking_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "cost_tracking_state.json")

    def _load_cost_tracking_state(self):
        try:
            with open(self._cost_tracking_path()) as f:
                state = json.load(f)
            self.cost_tracking_day = state.get("day")
            self.tracked_import_cost_today = state.get("import_cost", 0.0)
            self.tracked_export_value_today = state.get("export_value", 0.0)
        except (OSError, ValueError):
            pass

    def _save_cost_tracking_state(self):
        try:
            with open(self._cost_tracking_path(), "w") as f:
                json.dump({"day": self.cost_tracking_day,
                           "import_cost": self.tracked_import_cost_today,
                           "export_value": self.tracked_export_value_today}, f)
        except OSError:
            pass

    def _roll_cost_day(self, now):
        today_iso = now.date().isoformat()
        if self.cost_tracking_day != today_iso:
            self.cost_tracking_day = today_iso
            self.tracked_import_cost_today = 0.0
            self.tracked_export_value_today = 0.0

    def _update_energy_cost_tracking(self, now):
        """GridLock's own running import/export total, tracked every
        tick from live grid power + the same importing/exporting
        boolean sensors the flow diagram trusts for direction (not a
        power sensor's sign convention, which varies across inverter
        integrations). An approximation — 5-minute samples assumed
        constant in between — not a replacement for real metered
        billing data, just a fallback for when that isn't available
        and a visible cross-check when it is."""
        self._roll_cost_day(now)
        grid_kw = self._read_live_kw(self.ent_grid_power)
        if grid_kw is None:
            return
        kwh = grid_kw * (5 / 60)
        if self.ent_exporting and self.get_state(self.ent_exporting) == "on":
            exp_rate = self.get_float_state(self.ent_export_rate, self.default_export)
            self.tracked_export_value_today += kwh * exp_rate
        elif self.ent_importing and self.get_state(self.ent_importing) == "on":
            imp_rate = self.get_float_state(self.ent_import_rate, self.default_import)
            self.tracked_import_cost_today += kwh * imp_rate
        self._save_cost_tracking_state()

    def _decision_log_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "decision_log.json")

    def _load_decision_log(self):
        try:
            with open(self._decision_log_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _load_addon_overrides(self):
        """Entity overrides set via the Supervisor add-on's
        Configuration tab (written by run to addon_overrides.json,
        next to this file). Empty/missing entirely for the HACS/manual
        AppDaemon-app install path, which has no such UI — falls back
        to {} there, same as if nothing were overridden."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "addon_overrides.json")
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _log_decision(self, state, reason):
        """Human-readable history of what GridLock actually did and why
        — a running log of state changes (not every 5-min tick, only
        when the decision actually changes), so someone can look back
        over hours/days and see the reasoning rather than just the
        current status. When nothing's changed for a while, still drops
        in a "still X" check-in every hour at most — otherwise a long
        quiet stretch (which is normal; most ticks change nothing) looks
        indistinguishable from the engine having silently stopped."""
        last = self.decision_log[-1] if self.decision_log else None
        now = self.get_now()
        if last:
            last_reason = (last["reason"][len("Still: "):]
                           if last["reason"].startswith("Still: ") else last["reason"])
            if last["state"] == state and last_reason == reason:
                if now - self._iso(last["ts"]) < timedelta(hours=1):
                    return
                reason = f"Still: {reason}"
        self.decision_log.append({"ts": now.isoformat(),
                                   "state": state, "reason": reason})
        self.decision_log = self.decision_log[-200:]
        try:
            with open(self._decision_log_path(), "w") as f:
                json.dump(self.decision_log, f)
        except OSError:
            pass
        self.set_state("sensor.gridlock_decision_log",
                       state=self.decision_log[-1]["ts"],
                       attributes={"friendly_name": "GridLock Decision Log",
                                   "icon": "mdi:script-text-outline",
                                   "entries": self.decision_log[-100:]})

    @staticmethod
    def _mpan_stem(base_entity, suffix):
        """Strip domain + a known suffix off an already-discovered
        entity_id, e.g. 'sensor.x_ACCT_MPAN_current_rate' -> 'x_ACCT_MPAN',
        to find sibling entities sharing the same meter."""
        if not base_entity or not base_entity.endswith(suffix):
            return None
        return base_entity.split(".", 1)[1][: -len(suffix)]

    def _find_sibling(self, stem, domain, suffixes):
        """Find a sibling entity sharing `stem` (same account/MPAN) in
        `domain`, trying each of `suffixes` in turn and preferring live
        matches — Octopus's entity naming has changed between versions
        (e.g. day-rate events sometimes gaining an "_export_" infix), so
        this searches rather than blindly constructing one exact name."""
        if not stem:
            return None
        flat = self._all_states_flat()
        matches = [eid for eid in flat
                   if eid.startswith(f"{domain}.") and stem in eid
                   and any(eid.endswith(suf) for suf in suffixes)]
        if not matches:
            return None
        live = [eid for eid in matches if self._is_live(flat.get(eid))]
        pool = live or matches
        if len(pool) > 1:
            self.log(f"Multiple sibling entities for stem={stem!r} "
                     f"suffixes={suffixes}: {pool} — set it explicitly "
                     "if the wrong one gets picked.", level="WARNING")
        return pool[0]

    def get_float_state(self, entity_id, default=0.0):
        if not entity_id:
            return default
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unknown", "unavailable") else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _iso(dt_str):
        if isinstance(dt_str, datetime):
            return dt_str
        return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))

    def _attr_list(self, entity, attr):
        if not entity or not self.entity_exists(entity):
            return []
        v = self.get_state(entity, attribute=attr)
        return v if isinstance(v, list) else []

    # ------------------------------------------------------------------
    # SAVING SESSIONS
    # ------------------------------------------------------------------
    def on_saving_event(self, entity, attribute, old, new, kwargs):
        self.check_and_join_sessions()

    def check_and_join_sessions(self):
        if self.get_state("input_boolean.gridlock_enable") == "off":
            return
        for ev in self._attr_list(self.ent_saving_events, "available_events"):
            code = ev.get("code")
            if code:
                self.log(f"Saving Session {code} found - auto-enrolling")
                self.call_service(
                    "octopus_energy/join_octoplus_saving_session_event",
                    target={"entity_id": self.ent_saving_events},
                    event_code=code)
                start = ev.get("start", "")
                end = ev.get("end", "")
                rate = ev.get("octopoints_per_kwh", "?")
                self._notify("GridLock: Saving Session joined",
                            f"Joined {start} – {end} at {rate} pts/kWh.")

    def active_saving_session(self, now):
        for ev in self._attr_list(self.ent_saving_events, "joined_events"):
            try:
                if self._iso(ev["start"]) <= now < self._iso(ev["end"]):
                    return ev
            except (KeyError, ValueError):
                continue
        return None

    def parse_ssen(self, data):
        """Extract postcode-matched faults from a getallfaults payload."""
        pc = self.ssen_postcode
        local = [f for f in data.get("faults", [])
                 if any(str(area).upper().startswith(pc)
                        for area in (f.get("affectedAreas") or []))]
        planned = [f for f in local if f.get("type") == "PSI"]
        return {"local": len(local), "planned": len(planned),
                "severe": data.get("severeWeather") is not None,
                "faults": [{"ref": f.get("reference"),
                            "type": f.get("type"),
                            "restore": f.get("estimatedRestorationTimeUtc")}
                           for f in local]}

    def _notify(self, title, message):
        self.call_service("persistent_notification/create",
                          title=title, message=message)
        if self.notify_service:
            try:
                self.call_service(self.notify_service.replace(".", "/", 1),
                                  title=title, message=message)
            except Exception as exc:  # noqa: BLE001 — a bad notify_service shouldn't break the tick
                self.log(f"notify_service call failed: {exc!r}", level="WARNING")

    def _thermal_derate_factor(self):
        """Scale factor (1.0 = full rate) applied to charge/discharge
        commands based on live inverter temperature. Heat in power
        electronics scales roughly with current², so a lower commanded
        rate genuinely reduces heat generation, not just theoretically
        — an extra safety margin on top of whatever thermal protection
        the inverter already has built in (unverified what that
        actually is, or at what temperature it kicks in). Same 60°C/
        75°C thresholds as the Battery health panel's amber/red — a
        reasoned guess, not a manufacturer-specified limit: full rate
        below 60°C, tapering linearly to 25% by 75°C, holding at 25%
        above that (never fully to zero — an idle inverter doing
        nothing at all isn't obviously safer than one running gently)."""
        temp = self.get_float_state(self.ent_inverter_temp, None)
        if temp is None or temp < 60:
            return 1.0
        if temp >= 75:
            return 0.25
        return 1.0 - (temp - 60) / 15 * 0.75

    def poll_ssen(self, kwargs):
        try:
            req = urllib.request.Request(
                self.ssen_url, headers={"User-Agent": "GridLock/2"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
        except Exception as exc:  # noqa: BLE001 — network is best-effort
            self.log(f"SSEN poll failed: {exc!r}", level="WARNING")
            return
        prev = self.ssen_state
        self.ssen_state = self.parse_ssen(data)
        self.set_state("sensor.gridlock_ssen_local_outages",
                       state=str(self.ssen_state["local"]),
                       attributes={"friendly_name": "SSEN Outages (local)",
                                   "icon": "mdi:transmission-tower-off",
                                   "planned": self.ssen_state["planned"],
                                   "network_severe_weather":
                                       self.ssen_state["severe"],
                                   "faults": self.ssen_state["faults"]})
        if self.ssen_state["planned"] and not prev["planned"]:
            self._notify("GridLock: SSEN planned power cut",
                        f"SSEN lists a planned interruption for "
                        f"{self.ssen_postcode}. Storm Watch will hold "
                        "the battery at 100%.")
        if bool(self.ssen_state["local"]) != bool(prev["local"]):
            self.tick({})  # react immediately on outage appear/clear

    def poll_carbon_intensity(self, kwargs):
        """National Grid ESO's public carbon intensity API — GB
        national average, gCO2/kWh, 30-min blocks matching GridLock's
        own slot size. https://carbonintensity.org.uk, no key/postcode
        needed."""
        try:
            now_iso = self.get_now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            req = urllib.request.Request(
                f"https://api.carbonintensity.org.uk/intensity/{now_iso}/fw24h",
                headers={"Accept": "application/json", "User-Agent": "GridLock/2"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)["data"]
        except Exception as exc:  # noqa: BLE001 — network is best-effort
            self.log(f"Carbon intensity poll failed: {exc!r}", level="WARNING")
            return
        curve = [{"x": self._iso(p["from"]).isoformat(),
                  "y": p["intensity"]["forecast"],
                  "index": p["intensity"]["index"]}
                 for p in data if p.get("intensity", {}).get("forecast") is not None]
        current = curve[0] if curve else None
        self.set_state("sensor.gridlock_carbon_intensity",
                       state=str(current["y"]) if current else "unknown",
                       attributes={"friendly_name": "GridLock Carbon Intensity",
                                   "unit_of_measurement": "gCO2/kWh",
                                   "icon": "mdi:molecule-co2",
                                   "index": current["index"] if current else None,
                                   "forecast_data": curve})

    def storm_active(self):
        """Returns a reason string if Storm Watch should be active, else None."""
        if self.get_state("input_boolean.gridlock_storm_watch") == "on":
            return "manual override"
        if self.ssen_state.get("local", 0) > 0:
            n = self.ssen_state["local"]
            return f"SSEN outage at {self.ssen_postcode} ({n} fault(s))"
        for ent, severity in self.storm_sources:
            if not ent or not self.entity_exists(ent):
                continue
            state = str(self.get_state(ent)).lower()
            if state not in ("on", "yes", "true"):
                continue
            if not severity:
                return f"{ent} active"
            attrs = self.get_state(ent, attribute="all") or {}
            blob = str(attrs.get("attributes", attrs)).lower()
            for sev in severity:
                if str(sev).lower() in blob:
                    headline = (attrs.get("attributes", attrs) or {}).get(
                        "event", "weather alert")
                    return f"{sev}: {headline}"
        return None

    # ------------------------------------------------------------------
    # SLOT MODEL
    # ------------------------------------------------------------------
    def _rate_windows(self, entities):
        wins = []
        for e in entities:
            for r in self._attr_list(e, "rates"):
                try:
                    wins.append((self._iso(r["start"]), self._iso(r["end"]),
                                 float(r["value_inc_vat"])))
                except (KeyError, ValueError, TypeError):
                    continue
        return wins

    @staticmethod
    def _rate_at(wins, t, default):
        for s, e, v in wins:
            if s <= t < e:
                return v
        return default

    def _dispatch_windows(self):
        """[(start, end, charge_kwh)] from planned_dispatches — Octopus
        reports charge_in_kwh as negative (energy flowing to the car),
        normalised to positive here."""
        wins = []
        for d in self._attr_list(self.ent_dispatch, "planned_dispatches"):
            try:
                kwh = abs(float(d.get("charge_in_kwh", 0.0)))
                wins.append((self._iso(d["start"]), self._iso(d["end"]), kwh))
            except (KeyError, ValueError, TypeError):
                continue
        return wins

    def ev_dispatch_totals(self):
        """(planned_kwh, completed_kwh) Octopus has told the charger to
        deliver — planned = still upcoming, completed = already done."""
        def total(attr):
            s = 0.0
            for d in self._attr_list(self.ent_dispatch, attr):
                try:
                    s += abs(float(d.get("charge_in_kwh", 0.0)))
                except (TypeError, ValueError):
                    continue
            return round(s, 2)
        return total("planned_dispatches"), total("completed_dispatches")

    def _pv_curve(self):
        """{slot_start_utc: kWh} from Solcast detailedForecast (kW avg/30min)."""
        curve = {}
        for e in self.ent_solcast:
            for p in self._attr_list(e, "detailedForecast"):
                try:
                    t = self._iso(p["period_start"])
                    curve[t] = float(p["pv_estimate"]) / 2.0
                except (KeyError, ValueError, TypeError):
                    continue
        return curve

    def publish_solar_forecast(self, now):
        """Solcast curve as its own sensor, for the web UI's Forecast
        page — not otherwise exposed anywhere outside the 24h plan."""
        curve = self._pv_curve()
        # today_kwh/tomorrow_kwh use the full curve, but the chart data
        # is capped to the plan's own 24h horizon — some Solcast
        # accounts return a week or more of detailedForecast, and
        # charting all of it let a handful of far-future days dwarf
        # the scale so every near-term bar rounded down to nothing.
        horizon_end = now + timedelta(minutes=SLOT_MIN * HORIZON_SLOTS)
        fc = sorted(({"x": t.isoformat(), "y": round(kwh, 3)}
                     for t, kwh in curve.items() if now <= t < horizon_end),
                    key=lambda p: p["x"])
        today_kwh = sum(kwh for t, kwh in curve.items() if t.date() == now.date())
        tomorrow_kwh = sum(kwh for t, kwh in curve.items()
                           if t.date() == (now + timedelta(days=1)).date())
        self.set_state("sensor.gridlock_solar_forecast",
                       state=f"{today_kwh:.2f}",
                       attributes={"friendly_name": "GridLock Solar Forecast",
                                   "unit_of_measurement": "kWh",
                                   "icon": "mdi:solar-power-variant",
                                   "forecast_data": fc,
                                   "today_kwh": round(today_kwh, 2),
                                   "tomorrow_kwh": round(tomorrow_kwh, 2)})

    def publish_storm_status(self):
        reason = self.storm_active()
        self.set_state("sensor.gridlock_storm_status",
                       state="Active" if reason else "Clear",
                       attributes={"friendly_name": "GridLock Storm Watch",
                                   "icon": ("mdi:weather-lightning" if reason
                                            else "mdi:weather-partly-cloudy"),
                                   "reason": reason or "No active alerts"})

    def _load_kwh(self, slot_start):
        slot_idx = str(slot_start.hour * 2 + (1 if slot_start.minute >= 30 else 0))
        learned = self.load_profile.get(slot_idx)
        if learned is not None:
            return learned
        if self.load_weights and len(self.load_weights) == 24:
            w = float(self.load_weights[slot_start.hour])
            total = sum(float(x) for x in self.load_weights)
            return self.daily_house_kwh * (w / total) / 2.0
        return self.daily_house_kwh / 48.0

    def build_slots(self, now):
        imp_w = self._rate_windows(self.ent_rates)
        exp_w = self._rate_windows(self.ent_export_rates)
        disp = self._dispatch_windows()
        pv = self._pv_curve()

        live_imp = self.get_float_state(self.ent_import_rate, self.default_import)
        live_exp = self.get_float_state(self.ent_export_rate, self.default_export)
        cheap_floor = min([v for _, _, v in imp_w], default=live_imp)

        base = now.replace(minute=(0 if now.minute < 30 else 30),
                           second=0, microsecond=0)
        slots = []
        for i in range(HORIZON_SLOTS):
            s = base + timedelta(minutes=SLOT_MIN * i)
            e = s + timedelta(minutes=SLOT_MIN)
            imp = self._rate_at(imp_w, s, live_imp if i == 0 else self.default_import)
            ev_win = next(((ds, de, kwh) for ds, de, kwh in disp if ds <= s < de), None)
            in_disp = ev_win is not None
            ev_slot_kwh = 0.0
            if in_disp:
                imp = min(imp, cheap_floor)  # IOG dispatch = off-peak price
                # charge_in_kwh is the total for the whole dispatch
                # window, which Octopus merges across contiguous slots
                # (e.g. one entry spanning 2.5h) — split evenly across
                # however many 30-min slots that window actually covers,
                # rather than showing the same window total on every one.
                ds, de, win_kwh = ev_win
                num_slots = max(1, round((de - ds).total_seconds() / (SLOT_MIN * 60)))
                ev_slot_kwh = win_kwh / num_slots
            slots.append({
                "start": s, "end": e,
                "imp": imp,
                "exp": self._rate_at(exp_w, s,
                                     live_exp if i == 0 else self.default_export),
                "pv": pv.get(s, 0.0),
                "load": self._load_kwh(s),
                "dispatch": in_disp,
                "ev_kwh": ev_slot_kwh,
                "charge": 0.0,   # grid->battery kWh (AC side)
                "export": 0.0,   # battery->grid kWh (battery side)
            })

        # For each slot, the index of the next slot (at or after it)
        # whose import rate has dropped to "cheap" — i.e. the next
        # off-peak window, as far as this horizon can see. Used by
        # simulate()'s self-consumption branch to pace battery use
        # across a peak stretch instead of draining it early and
        # importing at the full peak rate for whatever's left. Slots
        # where optimise() has already picked CHARGE or EXPORT aren't
        # affected — this only shapes the "nothing better to do than
        # serve load from the battery" fallback.
        # For each slot in a peak stretch, its forecasted unmet load
        # (load minus PV) plus every later slot's, up to the next cheap
        # slot — i.e. this slot's share of the whole stretch's total
        # need. Used to weight pacing by where the battery actually
        # helps, not just split evenly by how many slots are left: an
        # even split hands a small load its full ask and leaves a much
        # bigger load later in the same stretch with an identical,
        # now-inadequate ration.
        next_cheap_idx = None
        deficit_acc = 0.0
        for i in range(len(slots) - 1, -1, -1):
            if slots[i]["imp"] <= self.cheap_rate:
                next_cheap_idx = i
                deficit_acc = 0.0
            else:
                deficit_acc += max(0.0, slots[i]["load"] - slots[i]["pv"])
            slots[i]["next_cheap_idx"] = next_cheap_idx
            slots[i]["remaining_deficit"] = deficit_acc
        return slots

    # ------------------------------------------------------------------
    # SIMULATION + OPTIMISER
    # ------------------------------------------------------------------
    def simulate(self, slots, soc0, imp_override=None, exp_override=None, want_trace=False):
        """Per-slot energy balance. Returns (soc_trace, cost,
        violation_idx, cost_trace, grid_cost).

        `cost` is the all-in figure optimise()'s hill-climb searches
        on — it includes an assumed £/kWh degradation cost on every
        battery discharge (see battery_risk_profile), specifically so
        the search is discouraged from cycling the battery for
        wafer-thin arbitrage margins. `grid_cost` is real grid
        import/export £ only, with no degradation mixed in, for
        anything user-facing (the plan table, "Plan cost 24h"):
        self-consumption discharging your own battery to serve load
        doesn't touch a meter, so showing it as rising "cost" there
        conflated an internal decision-weighting assumption with money
        that actually left your account.

        cost_trace (only built when want_trace=True — this runs
        thousands of times inside optimise()'s hill-climb, so skipping
        it there avoids building a throwaway list on every candidate
        step) is [{"delta": <this slot's grid £>, "total": <running
        grid £ total through this slot>, "grid_in": <total kWh drawn
        from the grid this slot>, "charge_in": <of that, how much
        actually went into the battery>}, ...], for the plan table.
        During CHARGE, grid_in includes the concurrent load served
        directly from the grid alongside the battery top-up (no
        battery discharge happens in that branch) — charge_in isolates
        just the top-up amount, since grid_in alone conflates the two
        and makes the battery look like it's charging far slower than
        the numbers imply."""
        eff = self.efficiency
        cap = self.battery_kwh
        floor_kwh = self.floor_soc / 100.0 * cap
        max_c = self.charge_kw / 2.0
        max_d = self.discharge_kw / 2.0
        max_d_export = self.export_rate_kw / 2.0
        soc = soc0
        trace, cost, grid_cost, violation = [], 0.0, 0.0, None
        cost_trace = [] if want_trace else None

        for i, s in enumerate(slots):
            imp = imp_override[i] if imp_override else s["imp"]
            exp = exp_override[i] if exp_override else s["exp"]
            batt = soc / 100.0 * cap
            grid_in = grid_out = charge_in = 0.0
            pv, load = s["pv"], s["load"]

            # PV serves house load first in every mode
            pv_to_load = min(pv, load)
            pv -= pv_to_load
            load -= pv_to_load

            # Reserve-aware export cap: don't let a sale eat into the
            # charge self-consumption is counting on to reach the next
            # off-peak window without hitting the floor — only sell
            # genuine surplus beyond that need. No cap at all when
            # there's no off-peak in sight (nothing to reserve for) —
            # that's when a genuinely good rate should be sold flat-out,
            # same as it always has been.
            next_cheap = s.get("next_cheap_idx")
            export_cap = max_d_export
            if next_cheap is not None and next_cheap > i:
                future_deficit = s.get("remaining_deficit", 0.0) - max(0.0, s["load"] - s["pv"])
                export_cap = max(0.0, min(max_d_export, (batt - floor_kwh) - future_deficit / eff))

            if s["charge"] > 0:
                # Grid-first charging: no battery discharge this slot
                c = min(s["charge"], max_c, max(0.0, (cap - batt) / eff))
                batt += c * eff
                grid_in += c
                charge_in = c
                room = max(0.0, min((cap - batt) / eff, max_c - c))
                pv_c = min(pv, room)
                batt += pv_c * eff
                grid_out += pv - pv_c
                grid_in += load
            elif s["export"] > 0 and batt > floor_kwh and export_cap > 0:
                # Genuine battery discharge for export — PV surplus
                # sold alongside it. If the battery's already at the
                # floor (this slot's "export" was only ever a planned
                # target, not something actually achievable), there's
                # nothing to discharge — fall through to the self-
                # consumption branch below instead of still selling
                # the PV: an empty battery with free PV to absorb
                # should charge from it, not sell it cheap, regardless
                # of what this slot's own rate happens to be. Same
                # fallthrough when export_cap is the thing that's zero —
                # selling here would eat into the reserve for reaching
                # the next off-peak window, so this slot behaves like an
                # empty battery for export purposes even though there's
                # genuinely charge in there.
                d = min(s["export"], export_cap, batt - floor_kwh)
                batt -= d
                ac = d * eff
                serve = min(ac, load)
                load -= serve
                grid_out += (ac - serve) + pv
                grid_in += load
                cost += d * self.degradation
            else:
                # Self consumption: PV surplus charges, deficit discharges
                room = max(0.0, min((cap - batt) / eff, max_c))
                pv_c = min(pv, room)
                batt += pv_c * eff
                grid_out += pv - pv_c
                if load > 0:
                    headroom = max(0.0, batt - floor_kwh)
                    if not self.conserve_battery:
                        # User override: just drain whatever's there,
                        # slot by slot, regardless of whether a later
                        # slot would get more value out of it.
                        avail = min(max_d, headroom)
                    # No export candidate beat the cheap-import gate for
                    # this slot (that branch already runs flat-out — see
                    # above), so this is genuinely a "meh, just keep the
                    # lights on" slot. If a known off-peak window is
                    # still ahead, ration what's left across the
                    # remaining stretch rather than draining fast now
                    # and importing at full peak rate later — weighted by
                    # each slot's own forecasted share of the stretch's
                    # total need (not just an even split by slot count),
                    # so a big load doesn't get throttled to the same
                    # ration as a tiny one earlier in the same stretch.
                    # Recomputed fresh every slot from the actual battery
                    # level, so it self-corrects if PV covers some slots
                    # along the way instead of committing to a stale plan.
                    elif next_cheap is not None and next_cheap > i:
                        total_deficit = s.get("remaining_deficit", 0.0)
                        this_deficit = max(0.0, s["load"] - s["pv"])
                        if total_deficit > 0:
                            avail = min(max_d, headroom * (this_deficit / total_deficit))
                        else:
                            avail = min(max_d, headroom / (next_cheap - i))
                    elif imp <= self.cheap_rate:
                        # Already sitting in a cheap/off-peak slot right
                        # now — spending stored charge here saves nothing
                        # over importing fresh (it's about as cheap as
                        # importing gets), and costs a real round-trip
                        # efficiency loss for zero benefit. Leave the
                        # battery alone and import instead, so whatever's
                        # stored stays available for the next expensive
                        # stretch.
                        avail = 0.0
                    else:
                        avail = min(max_d, headroom)
                    d = min(load / eff, avail)
                    batt -= d
                    load -= d * eff
                    grid_in += load
                    cost += d * self.degradation

            soc = max(0.0, min(100.0, batt / cap * 100.0))
            if soc < self.floor_soc - 0.5 and violation is None:
                violation = i
            slot_grid_cost = grid_in * imp - grid_out * exp
            cost += slot_grid_cost
            grid_cost += slot_grid_cost
            trace.append(round(soc, 1))
            if want_trace:
                cost_trace.append({"delta": round(slot_grid_cost, 4),
                                    "total": round(grid_cost, 4),
                                    "grid_in": round(grid_in, 3),
                                    "charge_in": round(charge_in, 3)})
        return trace, round(cost, 4), violation, cost_trace, round(grid_cost, 4)

    def optimise(self, slots, soc0):
        """Cost-greedy allocation: keep any charge/export step that lowers
        total 24h cost without breaching the SoC floor. Runs a coarse
        0.5kWh pass first (fast), then a finer 0.05kWh pass to mop up
        whatever the coarse step is too blunt to profitably close - e.g.
        a fraction-of-a-kWh shortfall right at the edge of the horizon,
        where a whole 0.5kWh step would cost more than the tiny gap it's
        covering is actually worth, so the coarse pass leaves it alone."""
        max_c = self.charge_kw / 2.0
        max_d_export = self.export_rate_kw / 2.0
        _, best, base_v, _, _ = self.simulate(slots, soc0)

        def refine(step, guard_budget):
            nonlocal best
            guard, improved = 0, True
            while improved and guard < guard_budget:
                improved = False
                for i in sorted(range(len(slots)), key=lambda i: slots[i]["imp"]):
                    s = slots[i]
                    # Hard rule, not just a cost-math discouragement: never
                    # grid-charge outside a genuinely cheap/off-peak slot.
                    # Candidates are visited cheapest-first, so once one
                    # fails this the rest (all pricier) do too - Storm
                    # Watch is the only thing allowed to charge regardless
                    # of rate, and it bypasses this planner entirely via
                    # its own apply() call in _tick_inner.
                    if s["imp"] > self.cheap_rate:
                        break
                    while s["charge"] < max_c and guard < guard_budget:
                        guard += 1
                        s["charge"] += step
                        _, c, v, _, _ = self.simulate(slots, soc0)
                        if (v is None or v == base_v) and c < best - 1e-6:
                            best, improved = c, True
                        else:
                            s["charge"] -= step
                            break
                for i in sorted(range(len(slots)),
                                key=lambda i: slots[i]["exp"], reverse=True):
                    s = slots[i]
                    if s["charge"] > 0:
                        continue
                    while s["export"] < max_d_export and guard < guard_budget:
                        guard += 1
                        s["export"] += step
                        _, c, v, _, _ = self.simulate(slots, soc0)
                        if (v is None or v == base_v) and c < best - 1e-6:
                            best, improved = c, True
                        else:
                            s["export"] -= step
                            break

        refine(0.5, 3000)
        refine(0.05, 600)

        # Drop any EXPORT block too small to be worth bothering with -
        # a lone slot selling a fraction of a percent of capacity isn't
        # worth the SoC it eats into, even if it technically shaved a
        # fraction of a penny off total cost. Only whole contiguous
        # blocks that clear min_export_pct of capacity survive; the
        # rest fall back to self-consumption.
        min_export_kwh = self.min_export_pct / 100.0 * self.battery_kwh
        i = 0
        while i < len(slots):
            if slots[i]["export"] > 0:
                j = i
                block_kwh = 0.0
                while j < len(slots) and slots[j]["export"] > 0:
                    block_kwh += slots[j]["export"]
                    j += 1
                if block_kwh < min_export_kwh:
                    for k in range(i, j):
                        slots[k]["export"] = 0.0
                i = j
            else:
                i += 1

        trace, cost, _, cost_trace, grid_cost = self.simulate(slots, soc0, want_trace=True)
        return slots, trace, cost, cost_trace, grid_cost

    # ------------------------------------------------------------------
    # OUTPUT
    # ------------------------------------------------------------------
    @staticmethod
    def _action(s):
        if s["charge"] > 0:
            return "CHARGE"
        if s["export"] > 0:
            return "EXPORT"
        return "ECO"

    @staticmethod
    def _fmt_hours(h):
        if h < 0.05:
            return "now"
        return f"in {h:.0f}h" if abs(h - round(h)) < 0.05 else f"in {h:.1f}h"

    def _plan_summary(self, slots, trace, soc0, now):
        """One-sentence digest of the plan already computed above — every
        figure here is read straight off `slots`/`trace`/`next_cheap_idx`,
        not an invented explanation of why the optimiser chose anything
        (it doesn't record that anywhere, so there's nothing honest to
        say beyond what these numbers already show)."""
        n = len(slots)
        if n == 0:
            return ""
        actions = [self._action(s) for s in slots]
        parts = []

        dominant = max(set(actions), key=actions.count)
        label = {"ECO": "self-consumption", "CHARGE": "grid charging",
                  "EXPORT": "export"}[dominant]
        ev_note = ", pausing if your EV starts charging" if (dominant == "ECO" and self.ent_ev) else ""
        parts.append(f"Running mainly on {label}{ev_note}")

        export_idxs = [i for i, a in enumerate(actions) if a == "EXPORT"]
        if export_idxs:
            best_i = max(export_idxs, key=lambda i: slots[i]["exp"])
            start = best_i
            while start > 0 and actions[start - 1] == "EXPORT":
                start -= 1
            end = best_i
            while end + 1 < n and actions[end + 1] == "EXPORT":
                end += 1
            kwh_sold = sum(slots[i]["export"] for i in range(start, end + 1))
            pct_batt = (kwh_sold / self.battery_kwh * 100) if self.battery_kwh else 0
            parts.append(f"export looks good {self._fmt_hours(start * 0.5)} "
                         f"({slots[best_i]['exp'] * 100:.0f}p) — sells "
                         f"~{pct_batt:.0f}% of battery capacity then")

        next_cheap = slots[0].get("next_cheap_idx")
        if next_cheap is not None and next_cheap > 0:
            parts.append(f"import drops to off-peak {self._fmt_hours(next_cheap * 0.5)}")
        elif next_cheap == 0:
            parts.append("already in the cheap import window")

        if next_cheap is not None:
            c_start = next_cheap
            while c_start < n and actions[c_start] != "CHARGE":
                c_start += 1
            if c_start < n:
                c_end = c_start
                while c_end + 1 < n and actions[c_end + 1] == "CHARGE":
                    c_end += 1
                soc_before = trace[c_start - 1] if c_start > 0 else soc0
                delta = max(0.0, trace[c_end] - soc_before)
                if delta > 0.5:
                    tomorrow = now.date() + timedelta(days=1)
                    tomorrow_pv = sum(s["pv"] for s in slots if s["start"].date() == tomorrow)
                    solar_note = (f" — tomorrow's forecast ({tomorrow_pv:.0f}kWh solar) covers the rest"
                                  if tomorrow_pv >= self.daily_house_kwh else "")
                    parts.append(f"only a {delta:.0f}% top-up planned then{solar_note}")

        return ". ".join(parts) + "."

    def publish_plan(self, slots, trace, cost_trace, grid_cost, soc0, now, live_label=None):
        """live_label overrides row 0's displayed action — the optimiser
        plan is theoretical (doesn't model EV/Storm/Session events), so
        when a live override (e.g. "EV Protection") is actually being
        applied for right now, the table should say so instead of
        showing what the battery-only plan would otherwise have done."""
        summary = self._plan_summary(slots, trace, soc0, now)
        fc = [{"x": s["start"].isoformat(), "y": trace[i]}
              for i, s in enumerate(slots)]
        learned = [{"x": f"{i // 2:02d}:{'30' if i % 2 else '00'}", "y": round(kwh, 3)}
                   for i, kwh in sorted(((int(k), v) for k, v in self.load_profile.items()),
                                        key=lambda p: p[0])]

        # Objective rate ranking (1 = best), for the CSV export — not a
        # claim about why the optimiser chose a slot (it doesn't record
        # that, only "did this reduce total cost"), just a verifiable
        # fact: where this slot's rate sits among the other 47.
        imp_rank = {idx: r + 1 for r, idx in
                    enumerate(sorted(range(len(slots)), key=lambda i: slots[i]["imp"]))}
        exp_rank = {idx: r + 1 for r, idx in
                    enumerate(sorted(range(len(slots)), key=lambda i: -slots[i]["exp"]))}

        rows = []
        plan_table = []
        for i, s in enumerate(slots):
            if i == 0 and live_label:
                act = live_label
                ll = live_label.lower()
                if "hold" in ll or "protection" in ll:
                    colour = "#fbbf24"     # amber — battery held, not actively charging/exporting
                elif "charg" in ll:
                    colour = "#22c55e"     # green
                elif "export" in ll or "session" in ll:
                    colour = "#38bdf8"     # cyan
                else:
                    colour = "#a78bfa"     # violet — other live override (e.g. Storm Watch)
            else:
                act = self._action(s)
                colour = {"CHARGE": "#22c55e", "EXPORT": "#38bdf8",
                          "ECO": "#9ca3af"}[act]
                # Forecast-level version of the same live bypass check
                # apply() does — if this slot's predicted SoC is at/near
                # the floor with no PV forecast, the actual hardware
                # command when this slot arrives will be the "Unknown"
                # bypass state, not "Maximum Self Consumption". Shown
                # here too, not just on the live status line, so the
                # whole stretch it applies to is visible rather than
                # just whatever slot happens to be "now".
                if act == "ECO" and trace[i] <= self.floor_soc + 2.0 and s["pv"] <= 0.01:
                    act = "ECO (Bypass)"
            ev_cell = (f"<span style='color:#38bdf8'>⚡ {s['ev_kwh']:.2f}</span>"
                       if s["dispatch"] else "—")
            delta_p = cost_trace[i]["delta"] * 100
            delta_colour = "#22c55e" if delta_p <= 0 else "#fbbf24"
            delta_sign = "+" if delta_p > 0 else ""
            # Whole row gets a faint tint of the action's colour — a
            # quicker "what's happening in this slot" scan than reading
            # the Action column text alone, especially scrolling fast
            # through the whole horizon's rows.
            grid_kwh = cost_trace[i]["grid_in"]
            charge_kwh = cost_trace[i]["charge_in"]
            rows.append(
                f"<tr style='background:{colour}1a'>"
                f"<td>{s['start'].astimezone().strftime('%a %H:%M')}</td>"
                f"<td>{s['imp']*100:.1f}p</td><td>{s['exp']*100:.1f}p</td>"
                f"<td>{s['pv']:.2f}</td><td>{s['load']:.2f}</td>"
                f"<td>{grid_kwh:.2f}</td>"
                f"<td>{charge_kwh:.2f}</td>"
                f"<td style='color:{colour};font-weight:600'>{act}</td>"
                f"<td>{ev_cell}</td>"
                f"<td>{trace[i]:.0f}%</td>"
                f"<td style='color:{delta_colour}'>{delta_sign}{delta_p:.1f}p</td>"
                f"<td>£{cost_trace[i]['total']:.2f}</td></tr>")
            # Array-of-arrays, not array-of-objects — field names would
            # otherwise repeat once per row and eat most of HA's ~16KB
            # attribute-size limit for no reason.
            plan_table.append([
                s["start"].astimezone().strftime("%a %H:%M"),
                round(s["imp"] * 100, 2), round(s["exp"] * 100, 2),
                round(s["pv"], 3), round(s["load"], 3), grid_kwh, charge_kwh, act,
                round(s["ev_kwh"], 3) if s["dispatch"] else None,
                trace[i], round(delta_p, 2), cost_trace[i]["total"],
                imp_rank[i], exp_rank[i]])
        html = ("<table class='gridlock-plan'><tr><th>Slot</th><th>Import</th>"
                "<th>Export</th><th>PV kWh</th><th>Load kWh</th>"
                "<th>Grid kWh</th><th>Charge kWh</th><th>Action</th>"
                "<th>EV kWh</th><th>SoC</th><th>Grid £</th><th>Total £</th></tr>"
                + "".join(rows) + "</table>")
        # grid_kwh is what the Grid £ column actually charges for -
        # the gap between load_kwh and grid_kwh is what PV/battery
        # covered for free, right there in the table instead of
        # something you have to take on faith. charge_kwh splits it
        # further during CHARGE specifically: grid_kwh there is the
        # battery top-up AND the concurrent load combined (CHARGE never
        # discharges the battery for load in the same slot) - without
        # this column a big Grid figure next to a small SoC move looks
        # like the battery is charging far slower than it actually is,
        # when most of that draw was just the house load passing
        # straight through.
        plan_table_cols = ["slot", "import_p", "export_p", "pv_kwh", "load_kwh",
                           "grid_kwh", "charge_kwh", "action", "ev_kwh", "soc_pct",
                           "cost_delta_p", "total_gbp", "import_rank", "export_rank"]
        self.set_state("sensor.gridlock_soc_forecast", state=str(trace[0]),
                       attributes={"friendly_name": "GridLock SoC Forecast",
                                   "unit_of_measurement": "%",
                                   "forecast_data": fc,
                                   "plan_cost_24h": grid_cost,
                                   "plan_summary": summary,
                                   "learned_load_profile": learned,
                                   "plan_table": {"columns": plan_table_cols,
                                                  "rows": plan_table,
                                                  "total_slots": len(slots)}})
        return html

    def publish_compare(self, slots, soc0, live_cost):
        rows = [("Current (live rates)", live_cost)]
        for t in self.compare_tariffs:
            imp, exp = [], []
            for s in slots:
                r = float(t.get("import_default", self.default_import))
                for w in t.get("import", []):
                    st = dtime.fromisoformat(w["start"])
                    en = dtime.fromisoformat(w["end"])
                    lt = s["start"].astimezone().time()
                    inside = (st <= lt < en) if st < en else (lt >= st or lt < en)
                    if inside:
                        r = float(w["rate"])
                        break
                imp.append(r)
                exp.append(float(t.get("export", 0.0)))
            # Re-optimise a copy of the raw slots under this tariff
            cp = [dict(s, charge=0.0, export=0.0, imp=imp[i], exp=exp[i])
                  for i, s in enumerate(slots)]
            cp, _, _, _, c = self.optimise(cp, soc0)
            c += float(t.get("standing", 0.0))
            rows.append((t.get("name", "tariff"), c))

        rows.sort(key=lambda r: r[1])
        best = rows[0][1]
        html_rows = "".join(
            f"<tr><td>{n}</td><td>£{c:.2f}</td>"
            f"<td>{'—' if c == best else f'+£{c-best:.2f}'}</td></tr>"
            for n, c in rows)
        html = ("<table class='gridlock-plan'><tr><th>Tariff</th>"
                "<th>24h cost</th><th>vs best</th></tr>" + html_rows +
                "</table>")
        self.set_state("sensor.gridlock_tariff_compare", state=rows[0][0],
                       attributes={"friendly_name": "GridLock Tariff Compare",
                                   "compare_html": html,
                                   "results": [{"name": n, "cost": c}
                                               for n, c in rows]})

    def update_daily_financials(self):
        now = self.get_now()
        self._update_energy_cost_tracking(now)
        # Real Octopus billing data when the sensor exists and has a
        # valid reading; get_float_state's default silently takes over
        # with GridLock's own tracked total otherwise (missing entity,
        # or "unknown"/"unavailable" state) — always published
        # separately too, rather than the substitution being invisible.
        imp = self.get_float_state(self.ent_daily_import_cost, self.tracked_import_cost_today)
        exp = self.get_float_state(self.ent_daily_export_value, self.tracked_export_value_today)
        stand = self.get_float_state(self.ent_daily_standing_charge)
        net = round(imp + stand - exp, 2)
        # Energy-only (no standing charge — identical either way, so it
        # cancels out of a savings comparison) real cost, for
        # _update_savings to compare its shadow baseline against.
        self._last_actual_energy_cost = imp - exp
        self.set_state("sensor.gridlock_calculated_net_cost_today",
                       state=f"{net:.2f}",
                       attributes={"friendly_name": "GridLock Net Cost Today",
                                   "unit_of_measurement": "£",
                                   "icon": "mdi:currency-gbp",
                                   "import_cost_today": imp,
                                   "export_credit_today": exp,
                                   "standing_charge_today": stand,
                                   "import_cost_calculated_today": round(self.tracked_import_cost_today, 2),
                                   "export_value_calculated_today": round(self.tracked_export_value_today, 2)})

        planned_kwh, completed_kwh = self.ev_dispatch_totals()
        self.set_state("sensor.gridlock_ev_dispatch_kwh",
                       state=f"{planned_kwh:.2f}",
                       attributes={"friendly_name": "GridLock EV Dispatch kWh",
                                   "unit_of_measurement": "kWh",
                                   "icon": "mdi:ev-station",
                                   "planned_kwh": planned_kwh,
                                   "completed_kwh": completed_kwh})

    # ------------------------------------------------------------------
    # MAIN LOOP
    # ------------------------------------------------------------------
    def on_trigger(self, entity, attribute, old, new, kwargs):
        self.tick({})

    def tick(self, kwargs):
        try:
            self._tick_inner(kwargs)
            self.set_state("sensor.gridlock_heartbeat",
                           state=self.get_now().isoformat(),
                           attributes={"friendly_name": "GridLock Heartbeat",
                                       "device_class": "timestamp"})
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            self.log(f"GridLock tick failed: {exc!r} — entering safe mode",
                     level="ERROR")
            try:
                self.apply(self.mode_eco, self.discharge_kw, self.charge_kw,
                           "Fault (safe mode)",
                           f"Engine error, running self-consumption: {exc}",
                           "")
            except Exception as exc2:  # noqa: BLE001
                self.log(f"Safe-mode apply also failed: {exc2!r}",
                         level="ERROR")

    def _tick_inner(self, kwargs):
        self.update_daily_financials()

        if self.get_state("input_boolean.gridlock_enable") == "off":
            self.set_state("sensor.gridlock_status", state="Disabled")
            return

        now = self.get_now()
        soc0 = self.get_float_state(self.ent_soc, 50.0)
        self._update_load_profile(now)
        self._update_savings(now)
        self.publish_solar_forecast(now)
        self.publish_storm_status()

        slots = self.build_slots(now)
        slots, trace, _, cost_trace, grid_cost = self.optimise(slots, soc0)
        self._track_plan_accuracy(now, grid_cost)

        cur = slots[0]
        action = self._action(cur)
        ev_active = bool(self.ent_ev) and self.get_state(self.ent_ev) == "on"
        session = self.active_saving_session(now)
        storm = self.storm_active()

        if bool(storm) != self._prev_storm_active:
            if storm:
                self._notify("GridLock: Storm Watch active", storm)
            else:
                self._notify("GridLock: Storm Watch cleared",
                            "Back to normal cost-optimised planning.")
        self._prev_storm_active = bool(storm)

        ev_protection_now = ev_active and action != "CHARGE"
        if ev_protection_now and not self._prev_ev_protection:
            self._notify("GridLock: EV Protection engaged",
                        "Battery discharge clamped to 0 while your EV "
                        "is charging concurrently.")
        self._prev_ev_protection = ev_protection_now

        # The optimiser's plan doesn't model EV/Storm/Session events, so
        # for "now" specifically (row 0), show what will actually be
        # applied below rather than the theoretical battery-only action.
        if storm:
            live_label = ("Storm Watch — Charging"
                           if soc0 < self.storm_target_soc - 1
                           else "Storm Watch — Holding")
        elif session and soc0 > self.floor_soc + 5:
            live_label = "Saving Session Export"
        elif ev_active:
            live_label = "Charging (EV concurrent)" if action == "CHARGE" else "EV Protection"
        else:
            live_label = None

        plan_html = self.publish_plan(slots, trace, cost_trace, grid_cost, soc0, now, live_label)
        self.publish_compare(self.build_slots(now), soc0, grid_cost)
        self.plan = slots

        target = next((trace[i] for i, s in enumerate(slots)
                       if self._action(s) == "CHARGE" and
                       (i + 1 == len(slots) or
                        self._action(slots[i + 1]) != "CHARGE")),
                      max(self.floor_soc, trace[0]))
        self.set_state("sensor.gridlock_target_soc", state=str(int(target)),
                       attributes={"friendly_name": "GridLock Target SoC",
                                   "unit_of_measurement": "%"})

        # --- Storm Watch overrides everything: charge & hold ---
        if storm:
            disch = 0.0 if ev_active else self.discharge_kw
            chg = self.ev_concurrent_charge_kw if ev_active else self.charge_kw
            if soc0 < self.storm_target_soc - 1:
                self.apply(self.mode_charge, disch, chg,
                           "Storm Watch — Charging",
                           f"Weather alert ({storm}): charging to "
                           f"{self.storm_target_soc:.0f}% regardless of rates",
                           plan_html)
            else:
                self.apply(self.mode_eco, disch, chg,
                           "Storm Watch — Holding",
                           f"Weather alert ({storm}): holding charge, "
                           "exports suspended", plan_html)
            return

        # --- Saving session: force export ---
        if session and soc0 > self.floor_soc + 5:
            self.apply(self.mode_discharge, self.discharge_kw,
                       self.charge_kw, "Saving Session Export",
                       f"Exporting for session {session.get('code', '')}",
                       plan_html)
            return

        # --- EV active: never let battery feed the car, but DO grid-charge
        #     in cheap/dispatch slots at a reduced rate ---
        if ev_active:
            if action == "CHARGE":
                self.apply(self.mode_charge, 0.0,
                           self.ev_concurrent_charge_kw,
                           "Charging (EV concurrent)",
                           f"Cheap slot + EV: charging at "
                           f"{self.ev_concurrent_charge_kw}kW, discharge 0",
                           plan_html)
            else:
                self.apply(self.mode_eco, 0.0, self.charge_kw,
                           "EV Protection",
                           "EV charging: battery discharge clamped to 0",
                           plan_html)
            return

        if action == "CHARGE":
            self.apply(self.mode_charge, self.discharge_kw, self.charge_kw,
                       "Command Charging",
                       f"Planned charge slot @ {cur['imp']*100:.1f}p"
                       f"{' (dispatch)' if cur['dispatch'] else ''}", plan_html)
        elif action == "EXPORT":
            self.apply(self.mode_discharge, self.discharge_kw, self.charge_kw,
                       "Export",
                       f"Planned export @ {cur['exp']*100:.1f}p", plan_html)
        else:
            self.apply(self.mode_eco, self.discharge_kw, self.charge_kw,
                       "Self Consumption", "Planned ECO slot", plan_html)

    def apply(self, mode, disch_kw, charge_kw, state, reason, plan_html):
        derate = self._thermal_derate_factor()
        if derate < 1.0:
            disch_kw = round(disch_kw * derate, 2)
            charge_kw = round(charge_kw * derate, 2)
            reason = f"{reason} (thermal derate to {derate * 100:.0f}%)"
        if mode == self.mode_eco:
            live_soc = self.get_float_state(self.ent_soc, 50.0)
            pv_now = bool(self.ent_pv_generating) and \
                self.get_state(self.ent_pv_generating) == "on"
            if live_soc <= self.floor_soc + 2.0 and not pv_now:
                # "Maximum Self Consumption" still has the inverter
                # actively hunting for battery power that isn't there
                # once it's at the floor. "Unknown" is Sigenergy's own
                # documented bypass state — it just passes load straight
                # through to the grid instead, which is what we want
                # here regardless of which ECO call site this came from.
                # But not while PV is actively generating: an empty
                # battery with free solar arriving should still charge
                # from it via normal self-consumption, not sit bypassed.
                mode = "Unknown"
                state = f"{state} — Bypass"
                reason = f"{reason} (battery at floor — bypass mode)"
        self._log_decision(state, reason)
        if self.get_state(self.ent_mode) != mode:
            self.call_service("select/select_option",
                              target={"entity_id": self.ent_mode}, option=mode)
        if self.get_float_state(self.ent_disch_limit, -1) != disch_kw:
            self.call_service("number/set_value",
                              target={"entity_id": self.ent_disch_limit},
                              value=disch_kw)
        if self.get_float_state(self.ent_charge_limit, -1) != charge_kw:
            self.call_service("number/set_value",
                              target={"entity_id": self.ent_charge_limit},
                              value=charge_kw)
        # Hardware-level backstop matching floor_soc — without this,
        # the floor only ever existed in this app's own planning, with
        # nothing stopping the real battery discharging past it if the
        # tick loop ever hung mid-command.
        if self.ent_discharge_cutoff and self.get_float_state(self.ent_discharge_cutoff, -1) != self.floor_soc:
            self.call_service("number/set_value",
                              target={"entity_id": self.ent_discharge_cutoff},
                              value=self.floor_soc)
        self.set_state("sensor.gridlock_status", state=state,
                       attributes={"friendly_name": "GridLock Status",
                                   "icon": "mdi:brain",
                                   "action": reason, "reason": reason,
                                   "plan_html": plan_html,
                                   # published so the Ingress web UI (and
                                   # anything else) can show/confirm the
                                   # (possibly auto-discovered) source
                                   # entities actually being used
                                   "soc_entity": self.ent_soc,
                                   "import_rate_entity": self.ent_import_rate,
                                   "export_rate_entity": self.ent_export_rate,
                                   "ev_entity": self.ent_ev,
                                   "dispatch_entity": self.ent_dispatch,
                                   "saving_events_entity": self.ent_saving_events,
                                   "daily_import_cost_entity": self.ent_daily_import_cost,
                                   "daily_export_value_entity": self.ent_daily_export_value,
                                   "daily_standing_charge_entity": self.ent_daily_standing_charge,
                                   # power-flow diagram entities
                                   "pv_power_entities": self.ent_pv_power_entities,
                                   "grid_power_entity": self.ent_grid_power,
                                   "battery_power_entity": self.ent_battery_power,
                                   "load_power_entity": self.ent_load_power,
                                   "pv_generating_entity": self.ent_pv_generating,
                                   "importing_entity": self.ent_importing,
                                   "exporting_entity": self.ent_exporting,
                                   "battery_charging_entity": self.ent_battery_charging,
                                   "battery_discharging_entity": self.ent_battery_discharging,
                                   "ev_power_entity": self.ent_ev_power,
                                   "inverter_temp_entity": self.ent_inverter_temp,
                                   "battery_temp_entity": self.ent_battery_temp,
                                   "battery_soh_entity": self.ent_battery_soh,
                                   "discharge_cutoff_entity": self.ent_discharge_cutoff,
                                   "battery_risk_profile": self.battery_risk_profile,
                                   "battery_degradation_cost": self.degradation,
                                   "thermal_derate": derate,
                                   "storm_watch_entities": [e for e, _ in self.storm_sources if e] or None,
                                   "ssen_postcode": self.ssen_postcode or None})

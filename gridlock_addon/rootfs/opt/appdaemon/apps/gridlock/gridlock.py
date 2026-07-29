import json
import os
import re
import urllib.request

VERSION = "2.12.0"

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta, time as dtime

SLOT_MIN = 30
HORIZON_SLOTS = 48  # 24h


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

        # Parameters
        self.battery_kwh = float(a.get("battery_capacity_kwh", 10.0))
        self.daily_house_kwh = float(a.get("typical_daily_house_kwh", 12.0))
        self.load_weights = a.get("load_hourly_weights")  # optional list[24]
        self.efficiency = float(a.get("inverter_efficiency", 0.90))
        self.degradation = float(a.get("battery_degradation_cost", 0.03))
        self.floor_soc = float(a.get("floor_soc", 20.0))
        self.charge_kw = float(a.get("charge_rate_kw", 10.0))
        self.discharge_kw = float(a.get("discharge_rate_kw", 10.0))
        self.ev_concurrent_charge_kw = float(a.get("ev_concurrent_charge_kw", 5.0))
        self.cheap_rate = float(a.get("cheap_rate_threshold", 0.10))
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
        raw = a.get("storm_watch_entity")
        self.storm_sources = []
        for item in (raw if isinstance(raw, list) else [raw] if raw else []):
            if isinstance(item, dict):
                self.storm_sources.append(
                    (item.get("entity"), item.get("severity", default_sev)))
            else:
                self.storm_sources.append((item, default_sev))
        self.storm_target_soc = float(a.get("storm_watch_target_soc", 100.0))

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

        # Daily financials — also matched off the import rate entity's
        # stem; daily_export_value_entity has no fixed Octopus naming
        # pattern (varies by inverter integration) so stays explicit-only.
        self.ent_daily_import_cost = a.get("daily_import_cost_entity") or self._find_sibling(
            import_stem, "sensor", ["_current_accumulative_cost"])
        self.ent_daily_export_value = a.get("daily_export_value_entity")
        self.ent_daily_standing_charge = a.get("daily_standing_charge_entity") or self._find_sibling(
            import_stem, "sensor", ["_current_standing_charge"])

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

        # Self-updater ([REDACTED]-style): pulls gridlock.py from a GitHub
        # repo. Private repos need a fine-grained PAT with contents:read.
        self.update_repo = a.get("update_repo")          # e.g. "you/gridlock"
        self.update_token = a.get("update_token")
        self.update_branch = a.get("update_branch", "main")
        self.update_path = a.get("update_path", "apps/gridlock/gridlock.py")
        self.auto_update = bool(a.get("auto_update", False))
        if self.update_repo:
            self.run_every(self.check_update, "now", 6 * 3600)

        if self.ssen_postcode:
            self.run_every(self.poll_ssen, "now", 300)

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
        current status."""
        last = self.decision_log[-1] if self.decision_log else None
        if last and last["state"] == state and last["reason"] == reason:
            return
        self.decision_log.append({"ts": self.get_now().isoformat(),
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

    def active_saving_session(self, now):
        for ev in self._attr_list(self.ent_saving_events, "joined_events"):
            try:
                if self._iso(ev["start"]) <= now < self._iso(ev["end"]):
                    return ev
            except (KeyError, ValueError):
                continue
        return None

    @staticmethod
    def extract_version(source):
        m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', source,
                      re.MULTILINE)
        return m.group(1) if m else None

    def fetch_remote_source(self):
        url = (f"https://api.github.com/repos/{self.update_repo}/contents/"
               f"{self.update_path}?ref={self.update_branch}")
        headers = {"Accept": "application/vnd.github.raw+json",
                   "User-Agent": "GridLock/2"}
        if self.update_token:
            headers["Authorization"] = f"Bearer {self.update_token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8")

    def check_update(self, kwargs):
        try:
            remote = self.fetch_remote_source()
            latest = self.extract_version(remote)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Update check failed: {exc!r}", level="WARNING")
            return
        if not latest:
            self.log("Update check: no VERSION found in remote file",
                     level="WARNING")
            return
        self.set_state("sensor.gridlock_version", state=VERSION, attributes={
            "friendly_name": "GridLock Version", "icon": "mdi:tag",
            "latest": latest, "update_available": latest != VERSION,
            "repo": self.update_repo})
        if latest == VERSION:
            return
        self.log(f"Update available: {VERSION} -> {latest}")
        if not self.auto_update:
            self.call_service(
                "persistent_notification/create",
                title="GridLock update available",
                message=(f"{VERSION} -> {latest} in {self.update_repo}. "
                         "Set auto_update: true or pull manually."))
            return
        try:
            compile(remote, "gridlock.py", "exec")  # sanity: valid Python
        except SyntaxError as exc:
            self.log(f"Refusing update — remote file has syntax error: {exc}",
                     level="ERROR")
            return
        target = os.path.abspath(__file__)
        with open(target + ".bak", "w") as fh:
            fh.write(open(target).read())
        with open(target, "w") as fh:
            fh.write(remote)
        self.log(f"Updated to {latest}; AppDaemon will reload the app. "
                 f"Previous version saved as gridlock.py.bak")

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
            self.call_service(
                "persistent_notification/create",
                title="SSEN planned power cut",
                message=(f"SSEN lists a planned interruption for "
                         f"{self.ssen_postcode}. Storm Watch will hold "
                         "the battery at 100%."))
        if bool(self.ssen_state["local"]) != bool(prev["local"]):
            self.tick({})  # react immediately on outage appear/clear

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
        fc = sorted(({"x": t.isoformat(), "y": round(kwh, 3)}
                     for t, kwh in curve.items()), key=lambda p: p["x"])
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
        return slots

    # ------------------------------------------------------------------
    # SIMULATION + OPTIMISER
    # ------------------------------------------------------------------
    def simulate(self, slots, soc0, imp_override=None, exp_override=None):
        """Per-slot energy balance. Returns (soc_trace, cost, violation_idx)."""
        eff = self.efficiency
        cap = self.battery_kwh
        floor_kwh = self.floor_soc / 100.0 * cap
        max_c = self.charge_kw / 2.0
        max_d = self.discharge_kw / 2.0
        soc = soc0
        trace, cost, violation = [], 0.0, None

        for i, s in enumerate(slots):
            imp = imp_override[i] if imp_override else s["imp"]
            exp = exp_override[i] if exp_override else s["exp"]
            batt = soc / 100.0 * cap
            grid_in = grid_out = 0.0
            pv, load = s["pv"], s["load"]

            # PV serves house load first in every mode
            pv_to_load = min(pv, load)
            pv -= pv_to_load
            load -= pv_to_load

            if s["charge"] > 0:
                # Grid-first charging: no battery discharge this slot
                c = min(s["charge"], max_c, max(0.0, (cap - batt) / eff))
                batt += c * eff
                grid_in += c
                room = max(0.0, min((cap - batt) / eff, max_c - c))
                pv_c = min(pv, room)
                batt += pv_c * eff
                grid_out += pv - pv_c
                grid_in += load
            elif s["export"] > 0:
                d = min(s["export"], max_d, max(0.0, batt - floor_kwh))
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
                    avail = min(max_d, max(0.0, batt - floor_kwh))
                    d = min(load / eff, avail)
                    batt -= d
                    load -= d * eff
                    grid_in += load
                    cost += d * self.degradation

            soc = max(0.0, min(100.0, batt / cap * 100.0))
            if soc < self.floor_soc - 0.5 and violation is None:
                violation = i
            cost += grid_in * imp - grid_out * exp
            trace.append(round(soc, 1))
        return trace, round(cost, 4), violation

    def optimise(self, slots, soc0):
        """Cost-greedy allocation: keep any 0.5 kWh charge/export step that
        lowers total 24h cost without breaching the SoC floor."""
        max_c = self.charge_kw / 2.0
        max_d = self.discharge_kw / 2.0
        step = 0.5
        _, best, base_v = self.simulate(slots, soc0)
        guard, improved = 0, True
        while improved and guard < 3000:
            improved = False
            for i in sorted(range(len(slots)), key=lambda i: slots[i]["imp"]):
                s = slots[i]
                while s["charge"] < max_c and guard < 3000:
                    guard += 1
                    s["charge"] += step
                    _, c, v = self.simulate(slots, soc0)
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
                while s["export"] < max_d and guard < 3000:
                    guard += 1
                    s["export"] += step
                    _, c, v = self.simulate(slots, soc0)
                    if (v is None or v == base_v) and c < best - 1e-6:
                        best, improved = c, True
                    else:
                        s["export"] -= step
                        break
        trace, cost, _ = self.simulate(slots, soc0)
        return slots, trace, cost

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

    def publish_plan(self, slots, trace, cost, soc0, live_label=None):
        """live_label overrides row 0's displayed action — the optimiser
        plan is theoretical (doesn't model EV/Storm/Session events), so
        when a live override (e.g. "EV Protection") is actually being
        applied for right now, the table should say so instead of
        showing what the battery-only plan would otherwise have done."""
        fc = [{"x": s["start"].isoformat(), "y": trace[i]}
              for i, s in enumerate(slots)]
        self.set_state("sensor.gridlock_soc_forecast", state=str(trace[0]),
                       attributes={"friendly_name": "GridLock SoC Forecast",
                                   "unit_of_measurement": "%",
                                   "forecast_data": fc,
                                   "plan_cost_24h": cost})

        rows = []
        for i, s in enumerate(slots):
            if i == 0 and live_label:
                act, colour = live_label, "#a78bfa"
            else:
                act = self._action(s)
                colour = {"CHARGE": "#22c55e", "EXPORT": "#38bdf8",
                          "ECO": "#9ca3af"}[act]
            ev_cell = (f"<span style='color:#38bdf8'>⚡ {s['ev_kwh']:.2f}</span>"
                       if s["dispatch"] else "—")
            rows.append(
                f"<tr><td>{s['start'].astimezone().strftime('%a %H:%M')}</td>"
                f"<td>{s['imp']*100:.1f}p</td><td>{s['exp']*100:.1f}p</td>"
                f"<td>{s['pv']:.2f}</td><td>{s['load']:.2f}</td>"
                f"<td style='color:{colour};font-weight:600'>{act}</td>"
                f"<td>{ev_cell}</td>"
                f"<td>{trace[i]:.0f}%</td></tr>")
        html = ("<table class='gridlock-plan'><tr><th>Slot</th><th>Import</th>"
                "<th>Export</th><th>PV kWh</th><th>Load kWh</th><th>Action</th>"
                "<th>EV kWh</th><th>SoC</th></tr>" + "".join(rows) + "</table>")
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
            cp, _, c = self.optimise(cp, soc0)
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
        imp = self.get_float_state(self.ent_daily_import_cost)
        exp = self.get_float_state(self.ent_daily_export_value)
        stand = self.get_float_state(self.ent_daily_standing_charge)
        net = round(imp + stand - exp, 2)
        self.set_state("sensor.gridlock_calculated_net_cost_today",
                       state=f"{net:.2f}",
                       attributes={"friendly_name": "GridLock Net Cost Today",
                                   "unit_of_measurement": "£",
                                   "icon": "mdi:currency-gbp",
                                   "import_cost_today": imp,
                                   "export_credit_today": exp,
                                   "standing_charge_today": stand})

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
        self.publish_solar_forecast(now)
        self.publish_storm_status()

        slots = self.build_slots(now)
        slots, trace, cost = self.optimise(slots, soc0)

        cur = slots[0]
        action = self._action(cur)
        ev_active = bool(self.ent_ev) and self.get_state(self.ent_ev) == "on"
        session = self.active_saving_session(now)
        storm = self.storm_active()

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

        plan_html = self.publish_plan(slots, trace, cost, soc0, live_label)
        self.publish_compare(self.build_slots(now), soc0, cost)
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
                                   "storm_watch_entities": [e for e, _ in self.storm_sources if e] or None,
                                   "ssen_postcode": self.ssen_postcode or None})

import json
import math
import os
import re
import shutil
import urllib.request
from dataclasses import replace

# The Supervisor add-on's config.yaml version is the single source of
# truth (set by `run` via GL_VERSION) — falls back to "dev" for the
# HACS/manual install path, which has no add-on manifest to read.
VERSION = os.environ.get("GL_VERSION") or "dev"

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta, timezone, time as dtime

from core.config import (Mode as OptMode, SiteConfig, RISK_PROFILES,
                          EXPORT_DEGRADATION_OVERRIDES, SLOT_MIN, HORIZON_SLOTS)
from core import slots as core_slots
from core import optimizer as core_optimizer
from core import failsafe as core_failsafe
from core.registry import HASensorRegistry
from core.inverter import SigenergyAdapter
from core.tariff import OctopusTariffProvider
from core.forecast import SolcastForecastProvider, LearnedLoadForecastProvider
from core import thermal as core_thermal
from core import diagnostics as core_diagnostics
from core import warranty as core_warranty

STATE_FILES = ("load_profile.json", "savings_state.json", "savings_history.json",
               "cost_tracking_state.json", "decision_log.json",
               "circuit_state.json", "circuit_history.json")

# publish_plan()'s per-slot row shape — a single source of truth for both
# the row length it builds and the "columns" name list it publishes
# alongside sensor.gridlock_soc_forecast's plan_table attribute, so the
# two can never drift out of sync with each other.
PLAN_TABLE_COLS = ["slot", "import_p", "export_p", "pv_kwh", "load_kwh",
                   "grid_kwh", "charge_kwh", "battery_kwh", "action", "ev_kwh",
                   "dispatch", "saving_session", "power_up_session", "session_reward_p",
                   "session_baseline_kwh", "session_export_baseline_kwh",
                   "soc_pct", "cost_delta_p", "total_gbp",
                   "import_rank", "export_rank"]

# The thermal model steps at a finer resolution than the battery plan's own
# 30-min slots (core/thermal.py's docstring/tests: a fast zone like a hot
# water tank can reach its target well within a single 30-min slot, and
# simulating at that coarse a resolution overshoots wildly since nothing
# gets a chance to react mid-slot). Simulated points are then downsampled
# to one per battery-plan slot for publishing, keeping payload size
# comparable to the SoC/solar forecasts rather than 576 raw points.
THERMAL_STEP_MIN = 5
THERMAL_HORIZON_STEPS = SLOT_MIN * HORIZON_SLOTS // THERMAL_STEP_MIN

# Confirmed in production (2026-07-30): a value equal to exactly 0/0.0
# nested inside a set_state() attributes payload — a dict key or a list
# element — goes missing by the time it comes back out of Home
# Assistant, whether read via Developer Tools' own state view or via
# the plain /api/states REST endpoint webui.py uses. Every single
# "corrupted" plan_table cell and every missing Solcast forecast point
# at night was, without exception, a value that should have been
# exactly zero for that slot — confirmed by cross-referencing against
# the same slot's still-intact plan_html string, built from the same
# numbers a few lines earlier in the same function. This predates any
# of the 3.1.x changes (the very first bug report already showed it,
# before the LP rewrite's own separate NaN issue was ever found or
# fixed) — it's somewhere in the AppDaemon<->HA state pipeline, not in
# gridlock.py's own data. Rather than track down exactly which layer
# does it, nudge every zero by a display-invisible epsilon at the one
# choke point all of this app's published attributes pass through
# (GridLock.set_state below) — a value that is never exactly zero
# can't be silently dropped by whatever's doing this.
_ZERO_EPS = 1e-9


def _never_zero_deep(obj):
    if isinstance(obj, dict):
        return {k: _never_zero_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_never_zero_deep(v) for v in obj]
    if isinstance(obj, bool):
        return obj  # bool is a subclass of int in Python -- never touch these
    if isinstance(obj, (int, float)) and obj == 0:
        return _ZERO_EPS
    return obj


class GridLock(hass.Hass):

    def set_state(self, entity_id, *args, **kwargs):
        """Every sensor this app publishes goes through here — see
        _never_zero_deep's docstring above for why. Intercepting the one
        shared call site catches this for every current and future
        attribute this app ever publishes, rather than needing every
        individual set_state() call site to remember to guard against it."""
        attrs = kwargs.get("attributes")
        if attrs is not None:
            kwargs["attributes"] = _never_zero_deep(attrs)
        return super().set_state(entity_id, *args, **kwargs)

    # ------------------------------------------------------------------
    # INIT
    # ------------------------------------------------------------------
    def initialize(self):
        self.log(f"=== GRIDLOCK {VERSION} PLANNING ENGINE STARTING ===")

        # Multi-site safety: every persisted file below is namespaced by
        # this AppDaemon app instance's own config key (e.g. "gridlock",
        # or "gridlock_cabin" for a second site block in the same
        # apps.yaml) — two site instances sharing this gridlock.py can no
        # longer stomp on each other's learned load profile / savings
        # history / decision log. An existing single-site install's
        # un-namespaced files get copied across once on first start under
        # this version, so upgrading doesn't reset anything already learned.
        self._site_slug = re.sub(r"[^A-Za-z0-9_.-]", "_", getattr(self, "name", None) or "default")
        self._migrate_legacy_state_files()

        self.registry = HASensorRegistry(self)

        a = self.args
        # Entity overrides from the Supervisor add-on's Configuration
        # tab (written by run to addon_overrides.json) — stays a single
        # flat file (the add-on's Configuration tab is a fixed HA UI
        # form, not something that can be dynamically re-keyed per
        # site), so this only ever applies to one "primary" site even
        # if apps.yaml defines more than one gridlock block.
        self.overrides = self._load_addon_overrides()

        # Hardware
        self.ent_mode = a["sigen_mode"]
        self.ent_disch_limit = a["sigen_discharge_limit"]
        self.ent_charge_limit = a["sigen_charge_limit"]
        self.ent_soc = a["sigen_soc"]
        # Same device as ent_mode (Sigenergy's own "grid connection
        # status" sensor — reads e.g. "Off Grid (Auto)" when the inverter
        # has islanded) — derived the same stem-matching way import/
        # export siblings are, rather than a second hardcoded device slug.
        # Optional: only used to force safe self-consumption during a
        # genuine outage, degrades to "never off-grid" if not found.
        self.ent_grid_connection_status = (
            a.get("grid_connection_status_entity")
            or self.overrides.get("grid_connection_status_entity_override")
            or self.registry.find_sibling(
                self.registry.mpan_stem(self.ent_mode, "_remote_ems_control_mode") or "",
                "sensor", ["_grid_connection_status"]))

        # Inputs
        self.ent_ev = (a.get("ev_charging") or self.overrides.get("ev_charging_override")
                       or self.registry.find_hypervolt_charging())

        # Octopus entities embed your account/MPAN in the entity_id
        # itself, so instead of requiring them in config, discover them
        # from the entity registry by naming pattern (BottlecapDave's
        # Octopus Energy integration convention). Explicit config (or a
        # secrets.yaml !secret ref) still wins if you set one.
        self.ent_dispatch = (a.get("octopus_dispatch")
                             or self.overrides.get("octopus_dispatch_override")
                             or self.registry.find(
            prefix="binary_sensor.octopus_energy_", suffix="_intelligent_dispatching"))
        self.ent_import_rate = (a.get("import_rate")
                                or self.overrides.get("import_rate_override")
                                or self.registry.find(
            prefix="sensor.octopus_energy_electricity_", suffix="_current_rate", avoid="export"))
        self.ent_export_rate = (a.get("export_rate")
                                or self.overrides.get("export_rate_override")
                                or self.registry.find(
            prefix="sensor.octopus_energy_electricity_", suffix="_export_current_rate"))
        # Octopus renamed "Saving Sessions" to "Power Down" sessions
        # (HomeAssistant-OctopusEnergy ADR 0004) — old _saving_session_
        # events entities are kept until Jan 2027, then removed, so this
        # tries the new naming first (what a current install actually
        # exposes) and falls back to the old one for as long as it's
        # still there, rather than only recognising the name that's
        # already scheduled for removal.
        self.ent_saving_events = (a.get("octopus_saving_events")
                                  or self.overrides.get("octopus_saving_events_override")
                                  or self.registry.find(
            prefix="event.octopus_energy_", suffix="_octoplus_power_down_events")
                                  or self.registry.find(
            prefix="event.octopus_energy_", suffix="_octoplus_saving_session_events"))

        # Free Electricity Sessions, renamed "Power Up" (same ADR as
        # above) — a genuinely separate Octopus programme from Power
        # Down, not another name for the same thing: it rewards
        # importing MORE than a predicted baseline (credited at your
        # own unit rate for the excess), not less, and — confirmed
        # against the integration's services.yaml — has no join
        # service at all. You're automatically included in every
        # announced session once enrolled, so there's nothing to
        # auto-join here, only sessions to read and plan around.
        self.ent_power_up_events = (a.get("power_up_events")
                                    or self.overrides.get("power_up_events_override")
                                    or self.registry.find(
            prefix="event.octopus_energy_", suffix="_octoplus_power_up_events"))

        import_stem = self.registry.mpan_stem(self.ent_import_rate, "_current_rate")
        export_stem = self.registry.mpan_stem(self.ent_export_rate, "_export_current_rate")

        # Both baseline sensors are disabled by default in HA (per the
        # integration's own docs) — the user needs to enable them for
        # session-reward modelling to have anything to read; discovery
        # degrades gracefully to "no reward modelling" if neither is
        # found or enabled, same as any other optional sensor.
        # Two separate baseline sensors exist depending on which programme
        # the account is actually on — confirmed via the integration's own
        # source (octoplus/power_down_baseline.py vs
        # octoplus/saving_session_baseline.py, same attribute shape, just a
        # different unique_id suffix) — mirrors the same two-programme
        # fallback already used for ent_saving_events above, just applied
        # to the baseline sensor too. Previously only the Power Down suffix
        # was tried, so a classic-Saving-Sessions account could never find
        # its baseline no matter what was enabled in HA.
        self.ent_power_down_baseline = (a.get("power_down_baseline_entity")
                                        or self.overrides.get("power_down_baseline_entity_override")
                                        or self.registry.find_sibling(
            import_stem, "sensor",
            ["_octoplus_power_down_baseline", "_octoplus_saving_session_baseline"]))
        self.ent_power_up_baseline = (a.get("power_up_baseline_entity")
                                      or self.overrides.get("power_up_baseline_entity_override")
                                      or self.registry.find_sibling(
            import_stem, "sensor", ["_octoplus_power_up_baseline"]))
        # Confirmed against a real account with export: the integration
        # instantiates a baseline sensor per METER Octopus reports, not
        # just the import one — if you export, there's a separate
        # baseline predicting your EXPORT for the same session, meaning
        # Power Down likely also rewards exporting MORE than this
        # baseline (on top of importing less), not just the import
        # side. Same graceful degrade if not found/enabled.
        self.ent_power_down_export_baseline = (
            a.get("power_down_export_baseline_entity")
            or self.overrides.get("power_down_export_baseline_entity_override")
            or self.registry.find_sibling(
                export_stem, "sensor",
                ["_export_octoplus_power_down_baseline",
                 "_export_octoplus_saving_session_baseline"]))
        self.ent_power_up_export_baseline = (
            a.get("power_up_export_baseline_entity")
            or self.overrides.get("power_up_export_baseline_entity_override")
            or self.registry.find_sibling(
                export_stem, "sensor", ["_export_octoplus_power_up_baseline"]))
        self.ent_rates = [e for e in [
            a.get("import_rates_previous") or self.registry.find_sibling(
                import_stem, "event", ["_previous_day_rates"]),
            a.get("import_rates_today") or self.registry.find_sibling(
                import_stem, "event", ["_current_day_rates"]),
            a.get("import_rates_tomorrow") or self.registry.find_sibling(
                import_stem, "event", ["_next_day_rates"]),
        ] if e]
        self.ent_export_rates = [e for e in [
            a.get("export_rates_today") or self.registry.find_sibling(
                export_stem, "event", ["_export_current_day_rates", "_current_day_rates"]),
            a.get("export_rates_tomorrow") or self.registry.find_sibling(
                export_stem, "event", ["_export_next_day_rates", "_next_day_rates"]),
        ] if e]

        # Solcast detailed curves
        self.ent_solcast = [e for e in [
            a.get("solcast_detail_today",
                  "sensor.solcast_pv_forecast_forecast_today"),
            a.get("solcast_detail_tomorrow",
                  "sensor.solcast_pv_forecast_forecast_tomorrow")] if e]

        self.ent_load_power = (a.get("load_power_entity")
                               or self.overrides.get("load_power_entity_override")
                               or self.registry.find_load_entity())
        self.decision_log = self._load_json("decision_log.json", [])
        # {code: end_iso} for every Saving Session code GridLock has ever
        # submitted a join for — the entity's own joined_events attribute
        # is NOT a reliable enough guard on its own (see
        # check_and_join_sessions' own comment), so this is GridLock's own
        # persisted memory, checked first.
        self.joined_session_codes = self._load_json("saving_session_state.json", {})

        self._last_actual_energy_cost = 0.0
        self.savings_day = None
        self.baseline_soc = None
        self.baseline_cost_today = 0.0
        self.savings_history = self._load_json("savings_history.json", {})
        self.plan_accuracy_day = None
        self.day_start_forecast = 0.0
        self.solar_deficit_day = None
        self._load_savings_state()

        self.circuit_day = None
        self.circuit_today_kwh = {}
        self.circuit_history = self._load_json("circuit_history.json", {})
        self._load_circuit_state()

        self.ent_pv_power_entities = a.get("pv_power_entities") or self.registry.find_pv_power()
        self.ent_grid_power = (a.get("grid_power_entity")
                               or self.overrides.get("grid_power_entity_override")
                               or self.registry.find_power("grid"))
        self.ent_battery_power = (a.get("battery_power_entity")
                                  or self.overrides.get("battery_power_entity_override")
                                  or self.registry.find_sibling(
            self.registry.mpan_stem(self.ent_soc, "_state_of_charge"), "sensor", ["_power"])
                                  or self.registry.find_power("battery"))
        self.ent_pv_generating = self.registry.find_binary("pv_generating")
        self.ent_importing = self.registry.find_binary("importing_from_grid")
        self.ent_exporting = self.registry.find_binary("exporting_to_grid")
        self.ent_battery_charging = self.registry.find_binary("battery_charging")
        self.ent_battery_discharging = self.registry.find_binary("battery_discharging")
        self.ent_ev_power = (a.get("ev_power_entity")
                             or self.overrides.get("ev_power_entity_override")
                             or self.registry.find(prefix="sensor.", contains="hypervolt_ev_power")
                             or self.registry.find_hypervolt_ev_power())
        self.ent_inverter_temp = (a.get("inverter_temp_entity")
                                  or self.overrides.get("inverter_temp_entity_override")
                                  or self.registry.find_temp("pcs", exclude=["cell", "battery"])
                                  or self.registry.find_temp("inverter", exclude=["cell", "battery"]))
        self.ent_battery_temp = (a.get("battery_temp_entity")
                                 or self.overrides.get("battery_temp_entity_override")
                                 or self.registry.find_temp("cell"))
        self.ent_battery_soh = (a.get("battery_soh_entity")
                                or self.overrides.get("battery_soh_entity_override")
                                or self.registry.find_soh())
        self.ent_discharge_cutoff = (a.get("discharge_cutoff_entity")
                                     or self.overrides.get("discharge_cutoff_entity_override")
                                     or self.registry.find_discharge_cutoff())

        # Parameters
        auto_capacity = self.registry.find_capacity_kwh()
        self.battery_kwh = float(a.get("battery_capacity_kwh") or auto_capacity or 10.0)
        self.daily_house_kwh = float(a.get("typical_daily_house_kwh", 12.0))
        self.load_weights = a.get("load_hourly_weights")  # optional list[24]
        self.efficiency = float(a.get("inverter_efficiency", 0.90))

        # ent_mode_override is GridLock's own helper (auto-created below,
        # same pattern as input_boolean.gridlock_enable) — lets the web
        # UI's mode segmented control switch strategy live, without an
        # AppDaemon restart, by re-resolving it every tick rather than
        # only once here at startup. "auto" (its default) defers entirely
        # to apps.yaml's battery_risk_profile, so an install that never
        # touches the web UI's control behaves exactly as before.
        self.ent_mode_override = "input_select.gridlock_mode_override"
        self._resolve_mode()
        self.floor_soc = float(a.get("floor_soc", 20.0))
        self.charge_kw = float(a.get("charge_rate_kw", 10.0))
        self.discharge_kw = float(a.get("discharge_rate_kw", 10.0))

        # Hardware-declared max, read straight off the number.* entity's
        # own min/max/step attributes — a misconfigured apps.yaml rate
        # can't be commanded above what the inverter itself says it
        # actually supports.
        for label, ent in (("charge_rate_kw", self.ent_charge_limit),
                           ("discharge_rate_kw", self.ent_disch_limit)):
            hw_max = self.registry.hardware_max_kw(ent)
            configured = self.charge_kw if label == "charge_rate_kw" else self.discharge_kw
            if hw_max and configured > hw_max:
                self.log(f"{label} ({configured}) exceeds {ent}'s own declared max "
                         f"({hw_max}) — clamping to it.", level="WARNING")
                if label == "charge_rate_kw":
                    self.charge_kw = hw_max
                else:
                    self.discharge_kw = hw_max

        self.ev_concurrent_charge_kw = float(a.get("ev_concurrent_charge_kw", 5.0))
        self.cheap_rate = float(a.get("cheap_rate_threshold", 0.10))
        self.min_export_pct = float(a.get("min_export_pct", 5.0))
        # Superseded by the LP optimiser, which paces battery use
        # optimally across the whole horizon on its own (see
        # core/optimizer.py) — kept parseable so an existing apps.yaml
        # doesn't error, but it no longer changes behaviour.
        self.conserve_battery = bool(a.get("conserve_battery_for_peak", False))
        if "conserve_battery_for_peak" in a:
            self.log("conserve_battery_for_peak is no longer used — the LP optimiser "
                     "always paces battery use optimally across the whole horizon, "
                     "which supersedes this toggle.", level="INFO")
        self.export_rate_kw = float(a.get("export_rate_kw", self.discharge_kw))
        self.default_import = float(a.get("default_import_rate", 0.2839))
        self.default_export = float(a.get("default_export_rate", 0.15))
        self.export_margin = float(a.get("export_margin", 0.02))
        self.target_daily_net_cost = (float(a["target_daily_net_cost"])
                                      if a.get("target_daily_net_cost") is not None else None)

        self.cfg = SiteConfig(
            site_id=self._site_slug, battery_kwh=self.battery_kwh,
            daily_house_kwh=self.daily_house_kwh, load_hourly_weights=self.load_weights,
            efficiency=self.efficiency, floor_soc=self.floor_soc, charge_kw=self.charge_kw,
            discharge_kw=self.discharge_kw, export_rate_kw=self.export_rate_kw,
            ev_concurrent_charge_kw=self.ev_concurrent_charge_kw, cheap_rate=self.cheap_rate,
            min_export_pct=self.min_export_pct, conserve_battery=self.conserve_battery,
            default_import=self.default_import, default_export=self.default_export,
            export_margin=self.export_margin, mode=self.mode, degradation=self.degradation,
            export_degradation=self.export_degradation,
            target_daily_net_cost=self.target_daily_net_cost,
            storm_target_soc=float(a.get("storm_watch_target_soc", 100.0)),
            reserve_margin_pct=float(a.get("reserve_margin_pct", 0.15)),
            horizon_slots=HORIZON_SLOTS, slot_min=SLOT_MIN)

        # Mode strings (Sigenergy EMS) + the adapter that actually talks
        # to the inverter (dedup/write logic lives in core/dedup.py,
        # wrapped by SigenergyAdapter).
        self.mode_charge = a.get("mode_charge", SigenergyAdapter.MODE_CHARGE)
        self.mode_discharge = a.get("mode_discharge", SigenergyAdapter.MODE_DISCHARGE)
        self.mode_eco = a.get("mode_eco", SigenergyAdapter.MODE_ECO)
        self.inverter_adapter = SigenergyAdapter(
            self.ent_mode, self.ent_disch_limit, self.ent_charge_limit,
            self.ent_discharge_cutoff, mode_charge=self.mode_charge,
            mode_discharge=self.mode_discharge, mode_eco=self.mode_eco)

        self.tariff_provider = OctopusTariffProvider(
            self, self.ent_rates, self.ent_export_rates, self.ent_dispatch)
        self.forecast_provider = SolcastForecastProvider(self, self.ent_solcast)
        self.load_provider = LearnedLoadForecastProvider(
            self, self._state_path("load_profile.json"),
            load_power_entity=self.ent_load_power, ev_entity=self.ent_ev,
            ev_power_entity=self.ent_ev_power, daily_house_kwh=self.daily_house_kwh,
            load_hourly_weights=self.load_weights)

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
        self.storm_target_soc = self.cfg.storm_target_soc
        # Only used when the current storm trigger carries no estimated
        # restoration time of its own (a weather alert, or the manual
        # override) — an SSEN fault's own estimatedRestorationTimeUtc is
        # always preferred when one's available (see
        # _estimated_outage_hours), since it's real data rather than a guess.
        self.storm_fallback_hours = float(a.get("storm_fallback_hours", 10.0))

        # Main fuse (Amps) load management — a pure live safety override,
        # not part of the LP optimiser at all: the optimizer has no
        # visibility into the EV/hot tub/heat pump loads that could
        # combine with battery charging to exceed the site's own main
        # fuse rating, so this reacts to the live combined site-import
        # reading instead (self.ent_grid_power — the CT clamp at the grid
        # connection point on a standard hybrid-inverter install, already
        # net of everything: house, EV, battery, etc). Defaults ON at
        # 100A — this add-on is specifically built for UK Octopus/
        # Sigenergy households, and single-phase UK domestic supplies are
        # standardised at 100A (the exception being 3-phase properties,
        # which should set their own real per-phase rating, or
        # main_fuse_amps: false to disable entirely).
        main_fuse_amps = a.get("main_fuse_amps", 100.0)
        self.load_mgmt_enabled = bool(main_fuse_amps)
        if self.load_mgmt_enabled:
            self.mains_voltage = float(a.get("mains_voltage", 240.0))
            self.main_fuse_amps = float(main_fuse_amps)
            max_import_kw = self.main_fuse_amps * self.mains_voltage / 1000.0
            self.load_mgmt_warn_kw = max_import_kw * float(a.get("load_mgmt_warn_pct", 0.80))
            self.load_mgmt_critical_kw = max_import_kw * float(a.get("load_mgmt_critical_pct", 0.90))
        self._prev_load_mgmt_state = None  # None / "warn" / "critical"

        # GridWarm — the heat pump thermal model + off-peak plan (see
        # core/thermal.py's docstring for the model itself). Prediction
        # is always advisory only; a zone only ever gets written to if
        # it explicitly sets control_entity (see _control_thermal_zone).
        # A named sub-block with its own "active" flag, not a bare
        # top-level list key — a stray indentation mistake under a bare
        # key silently produces an empty/wrong config with no error
        # (confirmed the hard way on a real install), whereas a wrong
        # "active" value at least fails loudly/visibly rather than just
        # doing nothing. Each zone entry is fully independent (a room,
        # or a hot water tank) — no brand-naming heuristic makes sense
        # for a specific named thermostat/heat-pump-controller, so these
        # are taken as direct config rather than auto-discovered.
        # Defaults to active whenever the block/zones exist, so just
        # listing zones is enough — "active: false" is purely an
        # explicit pause switch that doesn't require deleting the whole
        # block.
        gridwarm_cfg = a.get("gridwarm") or {}
        self.thermal_zones = ([self._build_thermal_zone(z) for z in gridwarm_cfg.get("zones", [])]
                              if gridwarm_cfg.get("active", True) else [])
        self._thermal_forecast_warned = set()
        self._thermal_control_state = {}
        # heat_loss_degrees learning -- refines the config's own starting
        # figure against real cooling periods (heating off) over time,
        # same EMA-blend pattern as the load forecast's learned profile.
        # Restored from disk and re-applied to each zone's params
        # immediately, so a restart doesn't silently reset progress back
        # to the config default until the next learning tick happens to
        # fire.
        self.thermal_learning_state = self._load_json("thermal_learning_state.json", {})
        for zone in self.thermal_zones:
            saved = self.thermal_learning_state.get(zone["name"], {})
            if saved.get("heat_loss_degrees") is not None:
                zone["params"].heat_loss_degrees = saved["heat_loss_degrees"]
            if saved.get("heat_loss_watts") is not None:
                zone["params"].heat_loss_watts = saved["heat_loss_watts"]
        for zone in self.thermal_zones:
            if not zone["control_entity"]:
                continue
            pause_helper = self._thermal_pause_helper(zone["name"])
            if not self.entity_exists(pause_helper):
                self.log(f"Create {pause_helper} as a real HA helper (Settings > Devices > "
                         f"Helpers) to be able to pause GridWarm's control of '{zone['name']}' "
                         "from the UI. Falling back to virtual entity (defaults on); the "
                         "toggle won't be controllable until you do.", level="WARNING")
                self.set_state(pause_helper, state="on")

        # GridWarm heat pump diagnostics — off unless configured. Two ways
        # to select entities: an explicit "entities:" list, or a simpler
        # "entity_prefix:" (e.g. "loft_heatpump_controller") that pulls in
        # every entity across every domain containing that substring —
        # most controller integrations (ESPHome, Modbus bridges, etc.)
        # give every entity on a device the same name prefix, so this
        # covers "give me everything for this device" without hand-typing
        # each entity_id (and re-editing apps.yaml every time the
        # controller's firmware exposes a new one). Both forms combine if
        # both are set. Re-resolved on every poll (not just at startup)
        # so a newly-appeared entity on the device shows up on its own.
        #
        # Two independent tracking mechanisms once resolved: a live
        # call_service event listener (catches an external command the
        # instant it happens, with full fidelity — domain, service, the
        # actual value set — since reconstructing that after the fact
        # from plain history loses exactly that detail), and a periodic
        # history pull for the plain state-change timeline (self-
        # reported values, not commands).
        diag_cfg = gridwarm_cfg.get("diagnostics", {}) or {}
        self.gridwarm_diagnostic_static_entities = list(dict.fromkeys(diag_cfg.get("entities", []) or []))
        self.gridwarm_diagnostic_prefix = diag_cfg.get("entity_prefix")
        self.heatpump_events = self._load_json("heatpump_events.json", [])
        self._refresh_diagnostic_entities()
        if self.gridwarm_diagnostic_static_entities or self.gridwarm_diagnostic_prefix:
            self.log(f"GridWarm diagnostics: watching {len(self.gridwarm_diagnostic_entities)} "
                     f"entities: {self.gridwarm_diagnostic_entities}")
            self.listen_event(self._on_heatpump_service_call, "call_service")

        self.notify_service = a.get("notify_service")
        self._prev_storm_active = False
        self._prev_ev_protection = False
        self._prev_off_grid = False
        self._prev_storm_reserve_sufficient = False

        # Failsafe / deadman switch — HA-link and Solcast-link liveness,
        # tracked every tick; see core/failsafe.py for the >15-minute
        # continuously-unavailable threshold this checks against.
        self._ha_last_live = None
        self._solcast_last_live = None

        # SSEN Power Track — engine polls the open API directly
        self.ssen_postcode = str(
            a.get("ssen_postcode") or self.overrides.get("ssen_postcode_override") or ""
        ).upper().strip()
        self.ssen_url = a.get(
            "ssen_api_url",
            "https://external.distribution.prd.ssen.co.uk"
            "/opendataportal-prd/v4/api/getallfaults")
        self.ssen_state = {"local": 0, "planned": 0, "severe": False, "faults": []}

        self.compare_tariffs = a.get("compare_tariffs", [])

        # Octopus Agile comparison — engine polls the open API directly
        # (public, no auth needed), same "poll periodically, cache the
        # result" pattern as SSEN Power Track above. Off unless
        # agile_region is explicitly set: Agile rates are region-specific
        # (14 DNO regions, letters A-P) and this add-on is shared across
        # installs — silently defaulting to one region would be actively
        # wrong for anyone outside it, not a harmless guess.
        self.agile_region = a.get("agile_region")
        self.agile_rates = {}
        self.agile_standing_gbp = 0.0

        # Component warranty tracking — off unless configured. Each entry
        # in warranties: is independent; most (Energy Controller, Sigen
        # Gateway, a heat pump, ...) are a plain calendar countdown, but
        # Sigenergy's own SigenStor *battery* warranty (confirmed from
        # published EU documentation, not a UK-specific source — see
        # DOCS.md) is throughput-based, not a cycle count: covered for
        # warranty_years years OR until throughput_cap_mwh total energy
        # throughput is reached, whichever comes first -- set
        # throughput_cap_mwh on an entry to opt it into that tracking.
        # No native lifetime/"total"-class battery charge or discharge
        # sensor exists on this integration (confirmed against a real
        # entity dump — PV production and total load consumption both
        # have one, battery charge/discharge only has the daily-
        # resetting kind), so GridLock builds its own running lifetime
        # total the same way every other daily-to-lifetime rollover in
        # this app already works — see _update_warranty_tracking.
        self.warranties_cfg = a.get("warranties") or []
        # Only one physical battery/meter in practice, so any single
        # entry's override (if more than one sets throughput_cap_mwh,
        # which would be unusual) is enough — the first one found wins.
        throughput_entry = next((w for w in self.warranties_cfg if "throughput_cap_mwh" in w), {})
        self.ent_daily_battery_charge = (throughput_entry.get("daily_charge_entity")
                                         or self.registry.find(domain="sensor",
                                                                contains="daily_battery_charge_energy"))
        self.ent_daily_battery_discharge = (throughput_entry.get("daily_discharge_entity")
                                            or self.registry.find(domain="sensor",
                                                                   contains="daily_battery_discharge_energy"))
        self.warranty_state = self._load_json("warranty_state.json", {})
        self._warranty_date_warned = set()
        for w in self.warranties_cfg:
            if "throughput_cap_mwh" in w and not (self.ent_daily_battery_charge
                                                   and self.ent_daily_battery_discharge):
                self.log(f"warranties entry {w.get('name', '?')!r} has throughput_cap_mwh set "
                         "but the daily battery charge/discharge sensors couldn't be discovered "
                         "— set daily_charge_entity/daily_discharge_entity explicitly on it.",
                         level="WARNING")

        self.ent_daily_import_cost = a.get("daily_import_cost_entity") or self.registry.find_sibling(
            import_stem, "sensor", ["_current_accumulative_cost"])
        self.ent_daily_export_value = (a.get("daily_export_value_entity")
                                       or self.overrides.get("daily_export_value_entity_override")
                                       or self.registry.find_sibling(
            export_stem, "sensor", ["_current_accumulative_cost"]))
        self.ent_daily_standing_charge = a.get("daily_standing_charge_entity") or self.registry.find_sibling(
            import_stem, "sensor", ["_current_standing_charge"])

        self.cost_tracking_day = None
        self.tracked_import_cost_today = 0.0
        self.tracked_export_value_today = 0.0
        self.tracked_import_kwh_today = 0.0
        self.tracked_offpeak_kwh_today = 0.0
        self.tracked_onpeak_kwh_today = 0.0
        self.tracked_offpeak_cost_today = 0.0
        self.tracked_onpeak_cost_today = 0.0
        self._load_cost_tracking_state()

        self.plan = []
        self.plan_built_at = None

        if not self.entity_exists("input_boolean.gridlock_enable"):
            self.log("Create input_boolean.gridlock_enable as a real HA helper "
                     "(Settings > Devices > Helpers). Falling back to virtual "
                     "entity; the UI toggle will NOT work until you do.",
                     level="WARNING")
            self.set_state("input_boolean.gridlock_enable", state="on")
        if not self.entity_exists(self.ent_mode_override):
            self.set_state(self.ent_mode_override, state="auto",
                           attributes={"friendly_name": "GridLock Mode Override",
                                       "options": ["auto", "eco", "balanced", "max_profit"]})
        # GridWarm's own master safety switch — a global gate above every
        # per-zone control_entity/pause helper, not a replacement for
        # them. Defaults to read_only regardless of what's configured in
        # apps.yaml, so active control never starts just because a zone
        # happens to have control_entity set — it has to be switched to
        # active here too, explicitly, from the UI.
        self.ent_gridwarm_mode = "input_select.gridlock_gridwarm_mode"
        if not self.entity_exists(self.ent_gridwarm_mode):
            self.set_state(self.ent_gridwarm_mode, state="read_only",
                           attributes={"friendly_name": "GridLock GridWarm Control Mode",
                                       "options": ["read_only", "active"]})

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
        # Real-time bypass guardrail: if the inverter's own mode entity
        # changes for any reason outside our own tick (manual override,
        # a fault reverting it to Unknown/bypass mid on-peak slot), re-plan
        # immediately instead of waiting up to 5 minutes for the next
        # scheduled tick — apply()'s existing write-dedup then re-asserts
        # the correct mode straight away if it's actually wrong.
        if self.entity_exists(self.ent_mode):
            self.listen_state(self.on_trigger, self.ent_mode)
        # Web UI's segmented mode control — re-plan immediately on change
        # rather than waiting for the next scheduled tick, so clicking
        # ECO/BALANCED/MAX PROFIT feels instant.
        self.listen_state(self.on_trigger, self.ent_mode_override)
        if self.entity_exists("input_boolean.gridlock_storm_watch"):
            self.listen_state(self.on_trigger, "input_boolean.gridlock_storm_watch")
        for ent, _ in self.storm_sources:
            if ent and self.entity_exists(ent):
                self.listen_state(self.on_trigger, ent)
            elif ent:
                self.log(f"Storm Watch entity '{ent}' not found", level="WARNING")
        if self.ent_saving_events and self.entity_exists(self.ent_saving_events):
            self.listen_state(self.on_saving_event, self.ent_saving_events)
            self.check_and_join_sessions()
        elif self.ent_saving_events:
            self.log(f"Saving Sessions entity '{self.ent_saving_events}' "
                     "not found", level="WARNING")

        if self.ssen_postcode:
            self.run_every(self.poll_ssen, "now", 300)

        if self.agile_region:
            self.run_every(self.poll_agile_rates, "now", 3600)

        if self.gridwarm_diagnostic_static_entities or self.gridwarm_diagnostic_prefix:
            self.run_every(self.poll_heatpump_diagnostics, "now", 1800)

        self.run_every(self.poll_carbon_intensity, "now", 1800)
        self.run_every(self.tick, "now", 300)

    # ------------------------------------------------------------------
    # PERSISTENCE (namespaced per site — see initialize()'s docstring note)
    # ------------------------------------------------------------------
    def _state_path(self, filename):
        d = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(d, f"{self._site_slug}__{filename}")

    def _migrate_legacy_state_files(self):
        """An existing single-site install upgrading past this version has
        its state under the old un-namespaced filenames — copy each across
        once (never delete the original) so upgrading doesn't silently
        reset a learned load profile or savings history."""
        d = os.path.dirname(os.path.abspath(__file__))
        for fname in STATE_FILES:
            namespaced = os.path.join(d, f"{self._site_slug}__{fname}")
            legacy = os.path.join(d, fname)
            if not os.path.exists(namespaced) and os.path.exists(legacy):
                try:
                    shutil.copy(legacy, namespaced)
                except OSError:
                    pass

    def _load_json(self, filename, default):
        try:
            with open(self._state_path(filename)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save_json(self, filename, data):
        try:
            with open(self._state_path(filename), "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def _load_addon_overrides(self):
        """Entity overrides from the Supervisor add-on's Configuration
        tab — deliberately NOT namespaced (see initialize()'s note: the
        add-on's Configuration tab is a single fixed HA form, so this
        only ever covers one "primary" site). Empty for the HACS/manual
        install path, which has no such UI."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "addon_overrides.json")
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------
    def get_float_state(self, entity_id, default=0.0):
        if not entity_id:
            return default
        try:
            v = self.get_state(entity_id)
            if v in (None, "unknown", "unavailable"):
                return default
            f = float(v)
            # float() happily parses the literal strings "nan"/"inf" —
            # a state that's ever exactly that (a broken template
            # sensor, an upstream integration erroring) would otherwise
            # poison every downstream calculation that reads it (found
            # via GridWarm's cost figures coming back "£NaN" — the true
            # source was one of these, not GridWarm's own arithmetic).
            return f if math.isfinite(f) else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _json_safe(value):
        """Guards a plan_table cell against None/NaN/inf before it ever
        reaches HA's state attributes — a value that goes missing partway
        through serialisation (observed: a stray NaN correlating with a
        shortened, misaligned row reaching the web UI) is worse than a
        wrong-looking but present 0, since a missing element shifts every
        column after it rather than just being wrong in its own cell."""
        if isinstance(value, float) and not math.isfinite(value):
            return 0.0
        if value is None:
            return 0.0
        return value

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

    def _resolve_mode(self):
        """Live mode resolution: ent_mode_override (settable from the web
        UI's segmented control) wins whenever it's set to something other
        than "auto"; apps.yaml's battery_risk_profile is the fallback.
        Called once at startup and again on every tick — cheap, and it's
        what lets a GUI click change strategy without restarting
        AppDaemon. Only self.mode/self.degradation/self.cfg are affected;
        the LP itself (core/optimizer.py) has no idea where its mode
        argument came from."""
        a = self.args
        override = (self.get_state(self.ent_mode_override)
                    if self.entity_exists(self.ent_mode_override) else None)
        raw = str(override if override and override != "auto"
                  else a.get("battery_risk_profile", "balanced")).lower()
        try:
            mode = OptMode(raw)
        except ValueError:
            self.log(f"Unknown mode {raw!r} (from "
                     f"{'the web UI override' if override and override != 'auto' else 'battery_risk_profile'})"
                     " — falling back to 'balanced'.", level="WARNING")
            raw = "balanced"
            mode = OptMode.BALANCED
        self.battery_risk_profile = raw
        self.mode = mode
        self.degradation = float(a.get("battery_degradation_cost", RISK_PROFILES[mode]))
        self.export_degradation = float(a.get(
            "export_degradation_cost", EXPORT_DEGRADATION_OVERRIDES.get(mode, self.degradation)))
        if hasattr(self, "cfg"):
            self.cfg = replace(self.cfg, mode=mode, degradation=self.degradation,
                               export_degradation=self.export_degradation)

    def _read_live_kw(self, entities):
        if isinstance(entities, str):
            entities = [entities]
        total, got_any = 0.0, False
        for e in (entities or []):
            v = self.get_float_state(e, None)
            if v is not None:
                total += abs(v)
                got_any = True
        return total if got_any else None

    def _log_decision(self, state, reason):
        """Human-readable history of what GridLock actually did and why —
        only logged on genuine change (not every 5-min tick), with a
        "Still: X" check-in at most once an hour so a long quiet stretch
        doesn't look indistinguishable from the engine having stopped."""
        last = self.decision_log[-1] if self.decision_log else None
        now = self.get_now()
        if last:
            last_reason = (last["reason"][len("Still: "):]
                           if last["reason"].startswith("Still: ") else last["reason"])
            if last["state"] == state and last_reason == reason:
                if now - self._iso(last["ts"]) < timedelta(hours=1):
                    return
                reason = f"Still: {reason}"
        self.decision_log.append({"ts": now.isoformat(), "state": state, "reason": reason})
        self.decision_log = self.decision_log[-200:]
        self._save_json("decision_log.json", self.decision_log)
        self.set_state("sensor.gridlock_decision_log",
                       state=self.decision_log[-1]["ts"],
                       attributes={"friendly_name": "GridLock Decision Log",
                                   "icon": "mdi:script-text-outline",
                                   "entries": self.decision_log[-100:]})

    # ------------------------------------------------------------------
    # SAVINGS TRACKING
    # ------------------------------------------------------------------
    def _load_savings_state(self):
        state = self._load_json("savings_state.json", {})
        self.savings_day = state.get("day")
        self.baseline_soc = state.get("baseline_soc")
        self.baseline_cost_today = state.get("baseline_cost_today", 0.0)
        self.plan_accuracy_day = state.get("plan_accuracy_day")
        self.day_start_forecast = state.get("day_start_forecast", 0.0)
        self.solar_deficit_day = state.get("solar_deficit_day")

    def _save_savings_state(self):
        self._save_json("savings_state.json", {
            "day": self.savings_day, "baseline_soc": self.baseline_soc,
            "baseline_cost_today": self.baseline_cost_today,
            "plan_accuracy_day": self.plan_accuracy_day,
            "day_start_forecast": self.day_start_forecast,
            "solar_deficit_day": self.solar_deficit_day})

    def _roll_savings_day(self, now):
        today_iso = now.date().isoformat()
        if self.savings_day is None:
            self.savings_day = today_iso
            self.baseline_cost_today = 0.0
            self.baseline_soc = self.get_float_state(self.ent_soc, 50.0)
            return
        if today_iso == self.savings_day:
            return
        self.savings_history.setdefault(self.savings_day, {}).update({
            "actual": round(self._last_actual_energy_cost, 4),
            "baseline": round(self.baseline_cost_today, 4)})
        self.savings_history = dict(list(self.savings_history.items())[-400:])
        self._save_json("savings_history.json", self.savings_history)
        self.savings_day = today_iso
        self.baseline_cost_today = 0.0
        self.baseline_soc = self.get_float_state(self.ent_soc, 50.0)

    def _update_savings(self, now):
        """One tick of a shadow self-consumption-only battery, driven by
        the same real PV/load/rate readings as everything else — the gap
        between what that hypothetical would have cost and what was
        actually paid is what GridLock's active scheduling is worth today."""
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

    def _track_plan_accuracy(self, now, grid_cost, slots, soc0):
        today_iso = now.date().isoformat()
        if self.plan_accuracy_day == today_iso:
            return
        if self.plan_accuracy_day is not None and self.plan_accuracy_day in self.savings_history:
            self.savings_history[self.plan_accuracy_day]["forecast"] = round(self.day_start_forecast, 4)
            self._save_json("savings_history.json", self.savings_history)
        self.plan_accuracy_day = today_iso
        self.day_start_forecast = grid_cost
        self._track_profile_comparison(now, slots, soc0)
        self._save_savings_state()

    def _track_profile_comparison(self, now, slots, soc0):
        """Once daily: what each mode's own morning plan predicts for
        today, using the same real rates/PV/load slots already built for
        the live plan (mode/degradation don't affect slot construction,
        only the optimiser, so there's no need to rebuild them per
        profile). Not a real-outcome backtest — that would mean running
        the optimiser continuously for all three modes rather than just
        the active one — but a genuine same-morning comparison, cheap
        since it only runs once a day."""
        comparison = {}
        for opt_mode in OptMode:
            # export_degradation must be set explicitly here too, not
            # just degradation — replace() only overrides what's named,
            # so without this every comparison profile would silently
            # inherit whichever mode is *currently* live's export
            # threshold instead of its own (e.g. comparing "eco" while
            # actually running "balanced" would compare eco's self-
            # consumption cost against balanced's export bar, not
            # eco's own).
            temp_cfg = replace(self.cfg, mode=opt_mode, degradation=RISK_PROFILES[opt_mode],
                               export_degradation=EXPORT_DEGRADATION_OVERRIDES.get(
                                   opt_mode, RISK_PROFILES[opt_mode]))
            try:
                result = core_optimizer.solve(slots, soc0, temp_cfg, today_date=now.date())
                if result.infeasible:
                    raise ValueError("LP reported infeasible")
                comparison[opt_mode.value] = round(result.grid_cost, 2)
            except Exception as exc:  # noqa: BLE001 — one mode failing shouldn't break the tick
                self.log(f"Profile comparison failed for {opt_mode.value!r}: {exc!r}", level="WARNING")
        today_iso = now.date().isoformat()
        self.savings_history.setdefault(today_iso, {})["profile_comparison"] = comparison
        self._save_json("savings_history.json", self.savings_history)

    # Reserve shortfall (a genuine, unavoidable-even-with-optimal-play gap
    # — see optimizer.py's reserve constraint) below which a solar-deficit
    # notification isn't worth firing: guards against a floating-point
    # residual from the solver reading as a "real" shortfall.
    SOLAR_DEFICIT_MIN_KWH = 0.05

    def _check_solar_deficit(self, now, slots, cost_trace):
        """Once daily, in the evening (giving time to act that night):
        does tomorrow's plan show a genuine reserve shortfall — i.e.
        even with an optimal charge/discharge schedule against the real
        solar forecast, does the LP itself still fall short? That's the
        actual answer to "is tomorrow's solar not enough", not a
        hand-rolled comparison against a fixed daily load figure, which
        would false-positive on a battery that's deliberately low
        because abundant solar is coming to refill it (the normal,
        correct pattern on a good day, and a real concern raised about
        this feature — verified this can't happen against a scenario
        shaped exactly like that: test_low_starting_soc_shows_zero_
        shortfall_when_solar_will_refill_it). Advisory only — GridLock
        has no way to request or influence when Octopus actually grants
        smart-charging dispatch, only to flag that plugging in improves
        the odds of getting some."""
        if now.hour < 18:
            return
        today_iso = now.date().isoformat()
        if self.solar_deficit_day == today_iso:
            return
        self.solar_deficit_day = today_iso
        self._save_savings_state()

        # This is advisory only — a bug here must never be able to fall
        # back the whole tick to safe self-consumption (tick()'s own
        # top-level try/except would do exactly that for anything
        # raised this far up the call chain), same reasoning as
        # _saving_session_plan_note's own try/except.
        try:
            tomorrow = now.date() + timedelta(days=1)
            tomorrow_shortfalls = [
                c["reserve_shortfall_kwh"] for s, c in zip(slots, cost_trace)
                if s["start"].date() == tomorrow]
            if not tomorrow_shortfalls:
                return  # horizon doesn't reach tomorrow (shouldn't happen at 48h, but don't guess)
            max_shortfall = max(tomorrow_shortfalls)
            if max_shortfall <= self.SOLAR_DEFICIT_MIN_KWH:
                return
            self._notify(
                "GridLock: Possible solar shortfall tomorrow",
                f"Tomorrow's plan shows a reserve shortfall of up to {max_shortfall:.1f} kWh "
                "even with optimal charging — solar alone may not cover your battery's needs. "
                "Plugging in the EV improves your odds of extra smart-charging dispatch during "
                "the day, not just the overnight window.")
        except Exception as exc:  # noqa: BLE001 — advisory notification only
            self.log(f"Solar deficit check failed: {exc!r}", level="WARNING")

    def _publish_savings(self, now):
        today, week, month, all_time = self._savings_totals(now)
        history = sorted(
            ({"date": d, "cost": round(v.get("actual", 0.0), 2)}
             for d, v in self.savings_history.items()),
            key=lambda p: p["date"])[-28:]
        # baseline - actual, per day — the same subtraction _savings_totals
        # already does for the running total, just kept per-day instead of
        # summed, for a KPI sparkline showing the trend rather than one number.
        saved_history = sorted(
            ({"date": d, "saved": round(v["baseline"] - v["actual"], 2)}
             for d, v in self.savings_history.items()
             if "baseline" in v and "actual" in v),
            key=lambda p: p["date"])[-28:]
        accuracy = None
        for d in sorted(self.savings_history.keys(), reverse=True):
            v = self.savings_history[d]
            if "forecast" in v and "actual" in v:
                accuracy = {"date": d, "forecast": round(v["forecast"], 2),
                           "actual": round(v["actual"], 2)}
                break
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

        # Bill reconciliation: does GridLock's own live-tracked estimate
        # agree with the real bill entity? Only days where both are
        # present — bill_import is explicitly None (not skipped via a
        # missing key) whenever the entity itself was unavailable at
        # rollover, so this correctly excludes those rather than treating
        # a missing bill as "agrees perfectly".
        bill_recon_history = sorted(
            ({"date": d, "bill_total": round(v["bill_import"] - (v.get("bill_export") or 0.0), 2),
              "estimate_total": round(v["estimate_import"] - v.get("estimate_export", 0.0), 2)}
             for d, v in self.savings_history.items()
             if v.get("bill_import") is not None and "estimate_import" in v),
            key=lambda p: p["date"])[-28:]

        # Month-to-date total — every fully-rolled day this calendar month
        # plus today's still-in-progress figures (both sides read live,
        # not waiting for tonight's rollover), so "this month" doesn't lag
        # a day behind on the 1st tick of a new day.
        month_bill_total = month_estimate_total = 0.0
        have_month_data = False
        for d, v in self.savings_history.items():
            try:
                dt = datetime.fromisoformat(d).date()
            except ValueError:
                continue
            if (dt.year == now.year and dt.month == now.month
                    and v.get("bill_import") is not None and "estimate_import" in v):
                month_bill_total += v["bill_import"] - (v.get("bill_export") or 0.0)
                month_estimate_total += v["estimate_import"] - v.get("estimate_export", 0.0)
                have_month_data = True
        today_bill = self.get_float_state(self.ent_daily_import_cost, None)
        if today_bill is not None:
            today_bill_export = self.get_float_state(self.ent_daily_export_value, 0.0)
            month_bill_total += today_bill - today_bill_export
            month_estimate_total += self.tracked_import_cost_today - self.tracked_export_value_today
            have_month_data = True
        bill_month_to_date = ({"bill_total": round(month_bill_total, 2),
                                "estimate_total": round(month_estimate_total, 2)}
                               if have_month_data else None)

        # Breakdown of the most recent day that has one — by tagged
        # circuit (core.forecast/circuit_history.json, cross-referenced by
        # date) when any existed that day, else the plain off-peak/on-peak
        # split every day already has. Circuit names are resolved against
        # CURRENT entity state (best-effort — a circuit renamed or removed
        # since that day just falls back to its entity_id).
        bill_breakdown = None
        for d in sorted(self.savings_history.keys(), reverse=True):
            v = self.savings_history[d]
            if "offpeak_import_kwh" not in v:
                continue
            total_kwh = v["offpeak_import_kwh"] + v["onpeak_import_kwh"]
            circuits_that_day = self.circuit_history.get(d, {})
            if circuits_that_day and total_kwh > 0:
                # No per-circuit time-of-use data exists (circuit_history
                # only has each circuit's daily kWh total, not when it was
                # drawn) — a single blended £/kWh rate for the whole day
                # (that day's real total cost / real total kWh, both
                # already tracked exactly) is the closest available
                # estimate, not an exact figure. Labelled "~" in the UI
                # rather than presented as precise.
                day_cost = v["estimate_import"]
                blended_rate = day_cost / total_kwh
                by_circuit = []
                for eid, kwh in circuits_that_day.items():
                    attrs = (self.get_state(eid, attribute="all") or {}).get("attributes", {}) \
                        if self.entity_exists(eid) else {}
                    by_circuit.append({"entity_id": eid, "name": attrs.get("friendly_name", eid),
                                        "kwh": round(kwh, 3), "pct": round(kwh / total_kwh * 100, 1),
                                        "cost": round(kwh * blended_rate, 3)})
                other_kwh = max(0.0, total_kwh - sum(c["kwh"] for c in by_circuit))
                by_circuit.append({"entity_id": None, "name": "Other", "kwh": round(other_kwh, 3),
                                    "pct": round(other_kwh / total_kwh * 100, 1),
                                    "cost": round(other_kwh * blended_rate, 3)})
                bill_breakdown = {"date": d, "by_circuit": by_circuit, "cost_is_estimated": True}
            else:
                bill_breakdown = {"date": d, "offpeak_kwh": round(v["offpeak_import_kwh"], 3),
                                   "onpeak_kwh": round(v["onpeak_import_kwh"], 3),
                                   "offpeak_cost": round(v.get("offpeak_import_cost", 0.0), 3),
                                   "onpeak_cost": round(v.get("onpeak_import_cost", 0.0), 3)}
            break

        self.set_state("sensor.gridlock_savings", state=f"{today:.2f}",
                       attributes={"friendly_name": "GridLock Savings",
                                   "unit_of_measurement": "£",
                                   "icon": "mdi:piggy-bank",
                                   "today": today, "week": week,
                                   "month": month, "all_time": all_time,
                                   "daily_cost_history": history,
                                   "daily_savings_history": saved_history,
                                   "plan_accuracy": accuracy,
                                   "profile_comparison_history": profile_history,
                                   "profile_comparison_totals": profile_totals,
                                   "bill_reconciliation_history": bill_recon_history,
                                   "bill_breakdown": bill_breakdown,
                                   "bill_month_to_date": bill_month_to_date})

    # ------------------------------------------------------------------
    # COST TRACKING
    # ------------------------------------------------------------------
    def _load_cost_tracking_state(self):
        state = self._load_json("cost_tracking_state.json", {})
        self.cost_tracking_day = state.get("day")
        self.tracked_import_cost_today = state.get("import_cost", 0.0)
        self.tracked_export_value_today = state.get("export_value", 0.0)
        self.tracked_import_kwh_today = state.get("import_kwh", 0.0)
        self.tracked_offpeak_kwh_today = state.get("offpeak_kwh", 0.0)
        self.tracked_onpeak_kwh_today = state.get("onpeak_kwh", 0.0)
        self.tracked_offpeak_cost_today = state.get("offpeak_cost", 0.0)
        self.tracked_onpeak_cost_today = state.get("onpeak_cost", 0.0)

    def _save_cost_tracking_state(self):
        self._save_json("cost_tracking_state.json", {
            "day": self.cost_tracking_day,
            "import_cost": self.tracked_import_cost_today,
            "export_value": self.tracked_export_value_today,
            "import_kwh": self.tracked_import_kwh_today,
            "offpeak_kwh": self.tracked_offpeak_kwh_today,
            "onpeak_kwh": self.tracked_onpeak_kwh_today,
            "offpeak_cost": self.tracked_offpeak_cost_today,
            "onpeak_cost": self.tracked_onpeak_cost_today})

    def _roll_cost_day(self, now):
        today_iso = now.date().isoformat()
        if self.cost_tracking_day is None:
            self.cost_tracking_day = today_iso
            return
        if today_iso == self.cost_tracking_day:
            return
        # Bill reconciliation: freeze GridLock's own estimate alongside the
        # real bill entity (BottlecapDave's Octopus integration — actual
        # billed cost, not a guess) for the day that just ended, onto the
        # same shared per-date dict savings_history.json already uses for
        # several other independently-tracked daily facts (baseline/
        # actual, forecast, profile_comparison) — same "many writers, one
        # per-day record" precedent, not a new parallel history file.
        # None (not 0.0) whenever the bill entity itself is unavailable,
        # so a later reconciliation view can tell "no data" apart from
        # "genuinely zero cost that day".
        bill_import = self.get_float_state(self.ent_daily_import_cost, None)
        bill_export = self.get_float_state(self.ent_daily_export_value, None)
        self.savings_history.setdefault(self.cost_tracking_day, {}).update({
            "bill_import": bill_import, "bill_export": bill_export,
            "estimate_import": round(self.tracked_import_cost_today, 4),
            "estimate_export": round(self.tracked_export_value_today, 4),
            "offpeak_import_kwh": round(self.tracked_offpeak_kwh_today, 3),
            "onpeak_import_kwh": round(self.tracked_onpeak_kwh_today, 3),
            "offpeak_import_cost": round(self.tracked_offpeak_cost_today, 4),
            "onpeak_import_cost": round(self.tracked_onpeak_cost_today, 4)})
        self.savings_history = dict(list(self.savings_history.items())[-400:])
        self._save_json("savings_history.json", self.savings_history)
        self.cost_tracking_day = today_iso
        self.tracked_import_cost_today = 0.0
        self.tracked_export_value_today = 0.0
        self.tracked_import_kwh_today = 0.0
        self.tracked_offpeak_kwh_today = 0.0
        self.tracked_onpeak_kwh_today = 0.0
        self.tracked_offpeak_cost_today = 0.0
        self.tracked_onpeak_cost_today = 0.0

    def _update_energy_cost_tracking(self, now):
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
            self.tracked_import_kwh_today += kwh
            # Same cheap_rate threshold already used for the off-peak
            # Bypass classification in publish_plan — not a new concept.
            if imp_rate <= self.cheap_rate:
                self.tracked_offpeak_kwh_today += kwh
                self.tracked_offpeak_cost_today += kwh * imp_rate
            else:
                self.tracked_onpeak_kwh_today += kwh
                self.tracked_onpeak_cost_today += kwh * imp_rate
        self._save_cost_tracking_state()

    def _all_warranties(self):
        """apps.yaml's warranties: list, plus anything added straight
        from the dashboard's own "Add component" form (stored in
        warranty_entries.json by the web UI itself, no apps.yaml edit
        or add-on restart needed) -- re-read fresh every call so a
        dashboard addition shows up on the next tick, not just after a
        restart. A dashboard entry sharing a name with an apps.yaml one
        is dropped in favour of the apps.yaml version, on the theory
        that anyone hand-editing apps.yaml for this meant it deliberately."""
        config_entries = [dict(w, source="config") for w in self.warranties_cfg]
        config_names = {w.get("name") for w in self.warranties_cfg}
        dashboard_entries = [dict(w, source="dashboard")
                             for w in self._load_json("warranty_entries.json", [])
                             if w.get("name") not in config_names]
        return config_entries + dashboard_entries

    def _update_warranty_tracking(self, now):
        """Rolls the Sigen inverter's own daily battery charge/discharge
        counters into a persisted lifetime total, at day rollover --
        shared across every warranties: entry that opts into throughput
        tracking (throughput_cap_mwh set); everything else in the list
        is a plain calendar countdown needing no tracking at all.

        Reads the two daily sensors on EVERY tick (not just at the tick
        that notices the day has changed) and keeps whatever was last
        seen -- reading them fresh exactly at the rollover tick risks
        the sensors having already reset to the new day's ~0 by then
        (GridLock's own tick runs every 5 minutes, not exactly at
        midnight), silently losing that day's real total. The rollover
        uses whatever was captured on the last tick still within the
        old day instead."""
        if not self._all_warranties():
            return
        t = self.warranty_state.setdefault("_throughput", {})
        today_iso = now.date().isoformat()
        if t.get("day") is None:
            t["day"] = today_iso
        elif today_iso != t["day"]:
            last_charge = t.get("last_charge_reading")
            last_discharge = t.get("last_discharge_reading")
            if last_charge is not None:
                t["lifetime_charge_kwh"] = t.get("lifetime_charge_kwh", 0.0) + last_charge
            if last_discharge is not None:
                t["lifetime_discharge_kwh"] = t.get("lifetime_discharge_kwh", 0.0) + last_discharge
            t["day"] = today_iso
        charge_now = self.get_float_state(self.ent_daily_battery_charge, None)
        discharge_now = self.get_float_state(self.ent_daily_battery_discharge, None)
        if charge_now is not None:
            t["last_charge_reading"] = charge_now
        if discharge_now is not None:
            t["last_discharge_reading"] = discharge_now
        self._publish_warranties(now)
        self._save_json("warranty_state.json", self.warranty_state)

    def _publish_warranties(self, now):
        t = self.warranty_state.get("_throughput", {})
        # today's in-progress total isn't captured into the lifetime
        # figure until rollover (above) -- add whatever's been seen so
        # far today so the dashboard reads as "right now", not "as of
        # yesterday" for most of every day.
        lifetime_charge_kwh = t.get("lifetime_charge_kwh", 0.0) + (t.get("last_charge_reading") or 0.0)
        lifetime_discharge_kwh = t.get("lifetime_discharge_kwh", 0.0) + (t.get("last_discharge_reading") or 0.0)
        items = []
        for w in self._all_warranties():
            warranty_years = float(w.get("warranty_years", 10.0))
            install_date_str = w.get("install_date")
            years_remaining = None
            warranty_end_date = None
            if install_date_str:
                install_date = core_warranty.parse_install_date(install_date_str)
                if install_date is not None:
                    years_remaining = core_warranty.warranty_years_remaining(
                        install_date, warranty_years, now.date())
                    warranty_end_date = (install_date + timedelta(
                        days=warranty_years * 365.25)).isoformat()
                elif w.get("name", "?") not in self._warranty_date_warned:
                    self.log(f"warranties entry {w.get('name', '?')!r} has an install_date "
                             f"{install_date_str!r} that doesn't parse (use DD-MM-YYYY or "
                             "YYYY-MM-DD) — years_remaining will be unavailable for it.",
                             level="WARNING")
                    self._warranty_date_warned.add(w.get("name", "?"))
            entry = {"name": w.get("name", "Component"),
                     "source": w.get("source", "config"),
                     "install_date": install_date_str,
                     "warranty_years": warranty_years,
                     "warranty_end_date": warranty_end_date,
                     "years_remaining": round(years_remaining, 2) if years_remaining is not None else None}
            if "throughput_cap_mwh" in w:
                throughput_cap_mwh = float(w["throughput_cap_mwh"])
                pct_used = core_warranty.throughput_pct_used(lifetime_discharge_kwh, throughput_cap_mwh)
                cycles = core_warranty.equivalent_full_cycles(lifetime_discharge_kwh, self.battery_kwh)
                entry.update({
                    "lifetime_charge_kwh": round(lifetime_charge_kwh, 2),
                    "lifetime_discharge_kwh": round(lifetime_discharge_kwh, 2),
                    "throughput_used_mwh": round(
                        core_warranty.throughput_used_mwh(lifetime_discharge_kwh), 3),
                    "throughput_cap_mwh": throughput_cap_mwh,
                    "throughput_pct_used": round(pct_used, 1) if pct_used is not None else None,
                    "equivalent_full_cycles": round(cycles, 1) if cycles is not None else None,
                    "capacity_retention_pct": w.get("capacity_retention_pct")})
            items.append(entry)
        self.set_state("sensor.gridlock_warranties", state=str(len(items)),
                       attributes={"friendly_name": "GridLock Warranties",
                                   "icon": "mdi:shield-check-outline",
                                   "items": items})

    # ------------------------------------------------------------------
    # SAVING SESSIONS
    # ------------------------------------------------------------------
    def on_saving_event(self, entity, attribute, old, new, kwargs):
        self.check_and_join_sessions()

    def check_and_join_sessions(self):
        if self.get_state("input_boolean.gridlock_enable") == "off":
            return
        # Which join service exists depends on which entity naming was
        # actually discovered (gridlock.py's ent_saving_events discovery
        # tries the new "power_down" suffix first, falling back to the
        # old "saving_session" one) — picking the service by what was
        # actually found is more reliable than guessing/trying one and
        # catching a failure, since a missing-service call doesn't
        # necessarily raise a catchable Python exception through
        # AppDaemon's call_service.
        join_service = ("octopus_energy/join_octoplus_saving_session_event"
                        if self.ent_saving_events
                        and "_octoplus_saving_session_events" in self.ent_saving_events
                        else "octopus_energy/join_octoplus_power_down_session_event")
        # joined_events turned out not to be a reliable enough guard on
        # its own: checking against it (v3.14.0) still let the same batch
        # get joined+notified twice in practice, because the integration
        # can take longer to reflect a fresh join back into joined_events
        # than it takes for ent_saving_events to fire another state-
        # changed event (a second on_saving_event callback landing before
        # the first join has round-tripped). joined_session_codes is
        # GridLock's OWN persisted memory of what it has already
        # submitted — checked and updated synchronously in this same
        # call, so a second callback arriving microseconds later sees it
        # immediately, with no dependency on the integration's own
        # refresh timing at all.
        now = self.get_now()
        pruned = {}
        for code, end in self.joined_session_codes.items():
            try:
                if self._iso(end) > now - timedelta(days=1):
                    pruned[code] = end
            except (ValueError, TypeError):
                continue
        self.joined_session_codes = pruned
        already = (set(self.joined_session_codes) |
                  {ev.get("code") for ev in
                   self._attr_list(self.ent_saving_events, "joined_events")})
        changed = False
        for ev in self._attr_list(self.ent_saving_events, "available_events"):
            code = ev.get("code")
            if not code or code in already:
                continue
            already.add(code)
            self.log(f"Saving Session {code} found - auto-enrolling")
            self.call_service(
                join_service,
                target={"entity_id": self.ent_saving_events},
                event_code=code)
            start = ev.get("start", "")
            end = ev.get("end", "")
            rate = ev.get("octopoints_per_kwh", "?")
            plan_note = self._saving_session_plan_note(start, end)
            self._notify("GridLock: Saving Session joined",
                        f"Joined {start} – {end} at {rate} pts/kWh.{plan_note}")
            self._log_decision("Saving Session joined",
                              f"Joined {start} – {end} at {rate} pts/kWh")
            self.joined_session_codes[code] = end
            changed = True
        if changed:
            self._save_json("saving_session_state.json", self.joined_session_codes)

    def _saving_session_plan_note(self, start, end):
        """Best-effort: solve a fresh plan and report how much battery
        it currently expects to use across this specific session's own
        window, so the join notification says something concrete
        rather than just the raw times/rate. This runs off an HA event
        (available_events updating), not the regular tick, so anything
        going wrong here must never affect the join itself (already
        submitted by the caller above) — only the notification text."""
        try:
            window_start, window_end = self._iso(start), self._iso(end)
            now = self.get_now()
            soc0 = self.get_float_state(self.ent_soc, 50.0)
            slots = self.build_slots(now)
            result = self._solve_plan(slots, soc0, now)
            if result.infeasible:
                return ""
            battery_kwh = sum(
                c["battery_kwh"] for i, c in enumerate(result.cost_trace)
                if window_start <= slots[i]["start"] < window_end)
            if battery_kwh <= 0:
                return ""
            pct = battery_kwh / self.battery_kwh * 100.0
            return f" Plan currently expects to discharge ~{pct:.0f}% of the battery during this window."
        except Exception as exc:  # noqa: BLE001 -- notification content only
            self.log(f"Saving Session plan note failed: {exc!r}", level="WARNING")
            return ""

    def active_saving_session(self, now):
        for ev in self._attr_list(self.ent_saving_events, "joined_events"):
            try:
                if self._iso(ev["start"]) <= now < self._iso(ev["end"]):
                    return ev
            except (KeyError, ValueError):
                continue
        return None

    # ------------------------------------------------------------------
    # SSEN / CARBON / STORM
    # ------------------------------------------------------------------
    def parse_ssen(self, data):
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
        commands based on live inverter temperature — heat in power
        electronics scales roughly with current², so a lower commanded
        rate genuinely reduces heat generation. Full rate below 60°C,
        tapering linearly to 25% by 75°C, holding at 25% above that."""
        temp = self.get_float_state(self.ent_inverter_temp, None)
        if temp is None or temp < 60:
            return 1.0
        if temp >= 75:
            return 0.25
        return 1.0 - (temp - 60) / 15 * 0.75

    def poll_ssen(self, kwargs):
        try:
            req = urllib.request.Request(
                self.ssen_url, headers={"User-Agent": "GridLock/3"})
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
                                   "network_severe_weather": self.ssen_state["severe"],
                                   "faults": self.ssen_state["faults"]})
        if self.ssen_state["planned"] and not prev["planned"]:
            self._notify("GridLock: SSEN planned power cut",
                        f"SSEN lists a planned interruption for "
                        f"{self.ssen_postcode}. Storm Watch will hold "
                        "the battery at 100%.")
        if bool(self.ssen_state["local"]) != bool(prev["local"]):
            self.tick({})

    def _agile_slot_key(self, dt):
        dt = dt.astimezone(timezone.utc)
        minute = 30 if dt.minute >= 30 else 0
        return dt.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _agile_rate_for(self, dt):
        return self.agile_rates.get(self._agile_slot_key(dt))

    def poll_agile_rates(self, kwargs):
        """Octopus's public Agile API — no auth needed, but genuinely
        two real facts that can't be hardcoded: which product code is
        Agile right now (Octopus renews it every 6-12 months, e.g.
        AGILE-24-10-01 -> a new one), and the region-specific tariff
        code built from it. Both looked up live every poll rather than
        assumed, same "don't guess at something checkable" discipline as
        everywhere else real money is on the line in this codebase."""
        try:
            req = urllib.request.Request(
                "https://api.octopus.energy/v1/products/?is_variable=true&brand=OCTOPUS_ENERGY",
                headers={"User-Agent": "GridLock/3"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                products = json.load(resp).get("results", [])
            now = self.get_now()
            agile = next(
                (p for p in products
                 if "Agile Octopus" in (p.get("full_name") or "")
                 and self._iso(p["available_from"]) <= now
                 and (not p.get("available_to") or self._iso(p["available_to"]) > now)),
                None)
            if not agile:
                self.log("Agile poll: no currently-active Agile product found", level="WARNING")
                return
            code = agile["code"]
            tariff_code = f"E-1R-{code}-{self.agile_region}"
            period_from = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
            period_to = (now + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
            base = f"https://api.octopus.energy/v1/products/{code}/electricity-tariffs/{tariff_code}"
            rates_req = urllib.request.Request(
                f"{base}/standard-unit-rates/?period_from={period_from}&period_to={period_to}",
                headers={"User-Agent": "GridLock/3"})
            with urllib.request.urlopen(rates_req, timeout=15) as resp:
                rates = json.load(resp).get("results", [])
            standing_req = urllib.request.Request(
                f"{base}/standing-charges/?period_from={period_from}&period_to={period_to}",
                headers={"User-Agent": "GridLock/3"})
            with urllib.request.urlopen(standing_req, timeout=15) as resp:
                standing = json.load(resp).get("results", [])
        except Exception as exc:  # noqa: BLE001 — network is best-effort
            self.log(f"Agile rate poll failed: {exc!r}", level="WARNING")
            return
        self.agile_rates = {
            self._agile_slot_key(self._iso(r["valid_from"])): r["value_inc_vat"] / 100.0
            for r in rates if r.get("valid_from") and r.get("value_inc_vat") is not None
        }
        if standing and standing[0].get("value_inc_vat") is not None:
            self.agile_standing_gbp = standing[0]["value_inc_vat"] / 100.0

    def poll_carbon_intensity(self, kwargs):
        try:
            now_iso = self.get_now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            req = urllib.request.Request(
                f"https://api.carbonintensity.org.uk/intensity/{now_iso}/fw24h",
                headers={"Accept": "application/json", "User-Agent": "GridLock/3"})
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

    def grid_connection_off(self):
        """Sigenergy's own "grid connection status" sensor reads e.g. "Off
        Grid (Auto)" once the inverter has actually islanded — a direct,
        authoritative signal, unlike Storm Watch's SSEN-outage-based
        inference, which only predicts a risk rather than confirming the
        site is genuinely disconnected right now. Substring match rather
        than an exact string, since "(Auto)" vs "(Manual)" variants exist
        and neither changes what GridLock should do about it."""
        if not self.ent_grid_connection_status:
            return None
        state = self.get_state(self.ent_grid_connection_status)
        return state if state and "off grid" in str(state).lower() else None

    def _estimated_outage_hours(self, now):
        """SSEN's own estimated restoration time for the current local
        outage(s), if any — real data, not a guess — taking the latest
        among multiple concurrent faults so a second, longer-running fault
        isn't silently ignored. Falls back to storm_fallback_hours when
        there's no such estimate at all (a weather-alert or manual-override
        trigger neither carry one) or it can't be parsed."""
        times = []
        for f in self.ssen_state.get("faults", []):
            r = f.get("restore")
            if not r:
                continue
            try:
                times.append(self._iso(r))
            except (ValueError, TypeError):
                continue
        if not times:
            return self.storm_fallback_hours
        hours = (max(times) - now).total_seconds() / 3600.0
        return hours if hours > 0 else self.storm_fallback_hours

    def _expected_load_kwh(self, now, hours):
        n_slots = max(1, round(hours * 2))  # half-hour slots
        return sum(self.load_provider.load_kwh(now + timedelta(minutes=30 * i))
                   for i in range(n_slots))

    def storm_active(self):
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
    # SLOT MODEL / OPTIMISER
    # ------------------------------------------------------------------
    def _power_down_points_for_session(self, start, end):
        """octopoints_per_kwh for whichever joined Power Down session
        the baseline sensor's own start/end matches — Power Down is
        opt-in, so this has to be cross-referenced against the joined
        list rather than read off the baseline sensor itself (which
        doesn't carry a points figure)."""
        for ev in self._attr_list(self.ent_saving_events, "joined_events"):
            try:
                if self._iso(ev["start"]) == start and self._iso(ev["end"]) == end:
                    return ev.get("octopoints_per_kwh")
            except (KeyError, ValueError, TypeError):
                continue
        return None

    def _octoplus_session_windows(self, baseline_entity, points_lookup=None):
        """Turn a Power Down/Power Up baseline sensor's own per-half-hour
        `baselines` array into (start, end, baseline_kwh, points_per_kwh)
        windows the optimiser can read directly — a genuine predicted
        curve for whichever session the sensor currently reflects (the
        "current or next" one), computed by the integration itself from
        real historic consumption, available ahead of the session
        actually starting. Both baseline sensors are disabled by default
        in HA — if the user hasn't enabled it, this just returns an
        empty list and reward modelling for that programme is silently
        skipped, same as if it was never discovered at all. points_lookup
        is only relevant for Power Down (Power Up has no points field —
        it's credited directly in £, see its discovery comment above)."""
        if not baseline_entity or not self.entity_exists(baseline_entity):
            return []
        state_obj = self.get_state(baseline_entity, attribute="all") or {}
        attrs = state_obj.get("attributes", {}) or {}
        if attrs.get("is_incomplete_calculation"):
            # Not enough matching historic half-hourly data yet (e.g. a
            # new meter) — the integration's own docs flag this as
            # unreliable, so don't feed it into planning.
            return []
        baselines = attrs.get("baselines")
        if not isinstance(baselines, list):
            return []
        points_per_kwh = None
        session_start, session_end = attrs.get("start"), attrs.get("end")
        if points_lookup and session_start and session_end:
            try:
                points_per_kwh = points_lookup(self._iso(session_start), self._iso(session_end))
            except (TypeError, ValueError):
                points_per_kwh = None
        windows = []
        for period in baselines:
            try:
                windows.append((self._iso(period["start"]), self._iso(period["end"]),
                                float(period["baseline"]), points_per_kwh))
            except (KeyError, ValueError, TypeError):
                continue
        return windows

    def build_slots(self, now):
        live_imp = self.get_float_state(self.ent_import_rate, self.default_import)
        live_exp = self.get_float_state(self.ent_export_rate, self.default_export)
        return core_slots.build_slots(
            now,
            import_windows=self.tariff_provider.import_windows(),
            export_windows=self.tariff_provider.export_windows(),
            dispatch_windows=self.tariff_provider.dispatch_windows(),
            pv_curve=self.forecast_provider.pv_curve(),
            load_kwh_fn=self.load_provider.load_kwh,
            cheap_rate=self.cheap_rate,
            live_import_rate=live_imp, live_export_rate=live_exp,
            default_import_rate=self.default_import, default_export_rate=self.default_export,
            power_down_windows=self._octoplus_session_windows(
                self.ent_power_down_baseline, self._power_down_points_for_session),
            power_up_windows=self._octoplus_session_windows(self.ent_power_up_baseline),
            power_down_export_windows=self._octoplus_session_windows(
                self.ent_power_down_export_baseline, self._power_down_points_for_session),
            power_up_export_windows=self._octoplus_session_windows(self.ent_power_up_export_baseline),
            horizon_slots=self.cfg.horizon_slots, slot_min=self.cfg.slot_min)

    def _solve_plan(self, slots, soc0, now):
        return core_optimizer.solve(slots, soc0, self.cfg, today_date=now.date())

    # ------------------------------------------------------------------
    # OUTPUT
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt_hours(h):
        if h < 0.05:
            return "now"
        return f"in {h:.0f}h" if abs(h - round(h)) < 0.05 else f"in {h:.1f}h"

    def _plan_summary(self, slots, trace, soc0, now):
        n = len(slots)
        if n == 0:
            return ""
        actions = [core_optimizer.action(s) for s in slots]
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
        summary = self._plan_summary(slots, trace, soc0, now)
        fc = [{"x": s["start"].isoformat(), "y": trace[i]}
              for i, s in enumerate(slots)]
        learned = self.load_provider.learned_series()

        imp_rank = {idx: r + 1 for r, idx in
                    enumerate(sorted(range(len(slots)), key=lambda i: slots[i]["imp"]))}
        exp_rank = {idx: r + 1 for r, idx in
                    enumerate(sorted(range(len(slots)), key=lambda i: -slots[i]["exp"]))}

        # Joined Saving Session windows, computed once rather than per
        # slot — each slot just checks its own start against these.
        saving_windows = []
        for ev in self._attr_list(self.ent_saving_events, "joined_events"):
            try:
                saving_windows.append((self._iso(ev["start"]), self._iso(ev["end"])))
            except (KeyError, ValueError, TypeError):
                continue

        rows = []
        plan_table = []
        for i, s in enumerate(slots):
            in_saving_session = any(ws <= s["start"] < we for ws, we in saving_windows)
            if i == 0 and live_label:
                act = live_label
                ll = live_label.lower()
                if "hold" in ll or "protection" in ll:
                    colour = "#fbbf24"
                elif "charg" in ll:
                    colour = "#22c55e"
                elif "export" in ll or "session" in ll:
                    colour = "#38bdf8"
                else:
                    colour = "#a78bfa"
            else:
                act = core_optimizer.action(s)
                colour = {"CHARGE": "#22c55e", "EXPORT": "#38bdf8",
                          "ECO": "#9ca3af"}[act]
                # An "ECO" slot where grid served the load directly and the
                # battery didn't move at all (no charge, no self-consumption,
                # no export) is worth its own label rather than blending
                # into "self-consumption" — but which label depends on WHY
                # the battery sat idle:
                if (act == "ECO" and cost_trace[i]["grid_in"] > core_optimizer.EPS
                        and cost_trace[i]["battery_kwh"] <= core_optimizer.EPS):
                    if s["imp"] <= self.cheap_rate:
                        # Deliberate, and cost-optimal: optimizer.py forces
                        # batt_to_load==0 in every cheap/off-peak slot on
                        # purpose — importing fresh at this same cheap rate
                        # is strictly cheaper than cycling stored charge to
                        # avoid paying it, so the battery is held back by
                        # design, not because it ran out. Neutral label, not
                        # a warning — this is the plan working as intended.
                        act = "Bypass"
                        colour = "#60a5fa"
                    elif trace[i] <= self.floor_soc + 2.0 and s["pv"] <= 0.01:
                        # Genuine problem: normal/peak pricing, but the
                        # battery had nothing left to give and grid had to
                        # step in. A slot that fully covers load from the
                        # battery with zero grid import is the reserve
                        # mechanism working as intended (drain it down
                        # before the next cheap slot recharges it), not a
                        # failure — this only fires when grid was actually
                        # needed.
                        act = "Forced Bypass"
                        colour = "#fb923c"
                elif act == "EXPORT" and in_saving_session:
                    # Forecast rows only ever showed plain "EXPORT" here —
                    # the saving_cell 💰 icon below already knew this slot
                    # sits inside a joined Saving Session, but the Action
                    # pill itself didn't say so unless it happened to be
                    # the live (i==0) slot, which gets this same label via
                    # _tick_inner's own live_label. Match that naming here
                    # too, instead of only the current slot getting credit.
                    act = "Saving Session Export"
            ev_cell = (f"<span style='color:#38bdf8'>⚡ {s['ev_kwh']:.2f}</span>"
                       if s["dispatch"] else "—")
            saving_cell = "<span style='color:#facc15'>💰</span>" if in_saving_session else "—"
            # Power Up (Free Electricity) doesn't need its own window
            # list the way Saving Sessions does — no opt-in, so every
            # slot the baseline sensor covers already came straight off
            # build_slots() as power_up_baseline_kwh, no separate
            # joined/available distinction to check against.
            in_power_up_session = s.get("power_up_baseline_kwh") is not None
            power_up_cell = "<span style='color:#4ade80'>⚡🆓</span>" if in_power_up_session else "—"
            session_reward_p = cost_trace[i].get("session_reward_gbp", 0.0) * 100
            # Whichever programme applies to this slot (mutually
            # exclusive per slot) — shown alongside grid_kwh so a small
            # reward is self-explanatory: if the baseline itself is
            # small for this half-hour (this user's own historic
            # consumption pattern, not a guess), there's only ever a
            # small amount of "reduction" to credit, no matter how
            # little is actually imported.
            session_baseline_kwh = s.get("power_down_baseline_kwh")
            if session_baseline_kwh is None:
                session_baseline_kwh = s.get("power_up_baseline_kwh")
            if session_baseline_kwh is None:
                session_baseline_kwh = 0.0
            # A Power Down slot can have BOTH an import baseline (above)
            # AND its own export baseline active at the same time — two
            # separate sensors, two separate predicted curves for the
            # same joined session (see core/optimizer.py's export-excess
            # reward term). Reported as its own column rather than
            # folded into session_baseline_kwh, so the dashboard can
            # show both instead of silently picking one.
            session_export_baseline_kwh = s.get("power_down_export_baseline_kwh")
            if session_export_baseline_kwh is None:
                session_export_baseline_kwh = s.get("power_up_export_baseline_kwh")
            if session_export_baseline_kwh is None:
                session_export_baseline_kwh = 0.0
            delta_p = cost_trace[i]["delta"] * 100
            delta_colour = "#22c55e" if delta_p <= 0 else "#fbbf24"
            delta_sign = "+" if delta_p > 0 else ""
            grid_kwh = cost_trace[i]["grid_in"]
            charge_kwh = cost_trace[i]["charge_in"]
            # Battery-side kWh discharged THIS slot (self-consumption +
            # export combined), taken directly from the LP's own
            # variables for slot i — deliberately not reconstructed from
            # a SoC delta against the row above/below, which is exactly
            # the misreading that made a correct decision (the best-
            # priced slot selling the most) look backwards in practice.
            battery_kwh = cost_trace[i]["battery_kwh"]
            rows.append(
                f"<tr style='background:{colour}1a'>"
                f"<td>{s['start'].astimezone().strftime('%a %H:%M')}</td>"
                f"<td>{s['imp']*100:.1f}p</td><td>{s['exp']*100:.1f}p</td>"
                f"<td>{s['pv']:.2f}</td><td>{s['load']:.2f}</td>"
                f"<td>{grid_kwh:.2f}</td>"
                f"<td>{charge_kwh:.2f}</td>"
                f"<td>{battery_kwh:.2f}</td>"
                f"<td style='color:{colour};font-weight:600'>{act}</td>"
                f"<td>{ev_cell}</td>"
                f"<td>{saving_cell}</td>"
                f"<td>{power_up_cell}</td>"
                f"<td>{trace[i]:.0f}%</td>"
                f"<td style='color:{delta_colour}'>{delta_sign}{delta_p:.1f}p</td>"
                f"<td>£{cost_trace[i]['total']:.2f}</td></tr>")
            plan_row = [
                s["start"].astimezone().strftime("%a %H:%M"),
                round(s["imp"] * 100, 2), round(s["exp"] * 100, 2),
                round(s["pv"], 3), round(s["load"], 3), grid_kwh, charge_kwh,
                battery_kwh, act,
                # Always a number, never None — a genuinely missing value
                # in a row this size is exactly what shifted every column
                # after it into the wrong slot in practice (see
                # PLAN_TABLE_COLS' length check below); "was this a
                # dispatch slot" now has its own explicit column instead.
                round(s["ev_kwh"], 3) if s["dispatch"] else 0.0,
                1 if s["dispatch"] else 0,
                1 if in_saving_session else 0,
                1 if in_power_up_session else 0,
                round(session_reward_p, 2),
                round(session_baseline_kwh, 3),
                round(session_export_baseline_kwh, 3),
                trace[i], round(delta_p, 2), cost_trace[i]["total"],
                imp_rank[i], exp_rank[i]]
            plan_row = [self._json_safe(v) for v in plan_row]
            if len(plan_row) != len(PLAN_TABLE_COLS):
                self.log(f"plan_table row has {len(plan_row)} values, expected "
                         f"{len(PLAN_TABLE_COLS)} — dropping this slot rather than "
                         "publish a misaligned row.", level="ERROR")
                continue
            plan_table.append(plan_row)
        html = ("<table class='gridlock-plan'><tr><th>Slot</th><th>Import</th>"
                "<th>Export</th><th>PV kWh</th><th>Load kWh</th>"
                "<th>Grid kWh</th><th>Charge kWh</th><th>Battery kWh</th><th>Action</th>"
                "<th>EV kWh</th><th>Saving session</th><th>Power Up</th><th>SoC</th><th>Grid £</th><th>Total £</th></tr>"
                + "".join(rows) + "</table>")
        self.set_state("sensor.gridlock_soc_forecast", state=str(trace[0]),
                       attributes={"friendly_name": "GridLock SoC Forecast",
                                   "unit_of_measurement": "%",
                                   "forecast_data": fc,
                                   "plan_cost_24h": grid_cost,
                                   "plan_summary": summary,
                                   "learned_load_profile": learned,
                                   "plan_table": {"columns": PLAN_TABLE_COLS,
                                                  "rows": plan_table,
                                                  "total_slots": len(slots)}})
        return html

    def publish_compare(self, slots, soc0, live_cost, now):
        # is_live distinguishes "we actually queried this tariff's real
        # rates" (Current, Agile) from "a fixed rate + time windows typed
        # into apps.yaml as an approximation of this tariff's published
        # structure" (every static compare_tariffs entry) -- shown on the
        # dashboard so a close-but-not-identical number between "Current"
        # and a static row for the SAME product you're actually on reads
        # as "estimate vs your real dispatch", not as a discrepancy/bug.
        rows = [("Current (live rates)", live_cost, True)]
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
            cp = [dict(s, charge=0.0, export=0.0, imp=imp[i], exp=exp[i])
                  for i, s in enumerate(slots)]
            result = core_optimizer.solve(cp, soc0, self.cfg, today_date=now.date())
            if result.infeasible:
                self.log(f"Tariff comparison for {t.get('name', 'tariff')!r} "
                         "reported infeasible — skipping it this tick.", level="WARNING")
                continue
            c = result.grid_cost + float(t.get("standing", 0.0))
            rows.append((t.get("name", "tariff"), c, False))

        # Agile import only (per the user's own ask — export stays as
        # whatever's actually configured, not also swapped to Agile's own
        # export tariff): real, live half-hourly rates from Octopus's
        # public API, not a flat+windows approximation like the static
        # tariffs above — Agile's whole point is that it doesn't have a
        # fixed daily pattern.
        #
        # Agile only ever publishes ~1 day ahead (tomorrow's rates land
        # around 4pm today), so requiring the FULL 48h comparison horizon
        # to be covered (the first version of this) meant the comparison
        # silently failed almost all day, every day — confirmed live.
        # Compare over whatever prefix of the horizon actually has
        # published rates instead, labelled with how many hours that
        # covers, rather than pretending it's the same 48h window as
        # every other row.
        if self.agile_region and self.agile_rates:
            covered = 0
            for s in slots:
                if self._agile_rate_for(s["start"]) is None:
                    break
                covered += 1
            if covered >= 4:  # at least 2h — anything shorter isn't a useful comparison
                agile_slots = slots[:covered]
                agile_imp = [self._agile_rate_for(s["start"]) for s in agile_slots]
                cp = [dict(s, charge=0.0, export=0.0, imp=agile_imp[i], exp=s["exp"])
                      for i, s in enumerate(agile_slots)]
                result = core_optimizer.solve(cp, soc0, self.cfg, today_date=now.date())
                if result.infeasible:
                    self.log("Agile tariff comparison reported infeasible — "
                             "skipping it this tick.", level="WARNING")
                else:
                    hours = covered * SLOT_MIN / 60.0
                    standing = self.agile_standing_gbp * hours / 24.0
                    rows.append((f"Octopus Agile (import, next {hours:.0f}h)",
                                 result.grid_cost + standing, True))
            else:
                self.log("Agile comparison skipped — no published rate data "
                         "yet for the upcoming slots.", level="DEBUG")

        rows.sort(key=lambda r: r[1])
        best = rows[0][1]
        html_rows = "".join(
            f"<tr><td>{n}</td><td>£{c:.2f}</td>"
            f"<td>{'—' if c == best else f'+£{c-best:.2f}'}</td></tr>"
            for n, c, _ in rows)
        html = ("<table class='gridlock-plan'><tr><th>Tariff</th>"
                "<th>24h cost</th><th>vs best</th></tr>" + html_rows +
                "</table>")
        self.set_state("sensor.gridlock_tariff_compare", state=rows[0][0],
                       attributes={"friendly_name": "GridLock Tariff Compare",
                                   "compare_html": html,
                                   "results": [{"name": n, "cost": c, "is_live": is_live}
                                               for n, c, is_live in rows]})

    def publish_solar_forecast(self, now):
        curve = self.forecast_provider.pv_curve()
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

    # ------------------------------------------------------------------
    # HEAT PUMP THERMAL MODEL (GridWarm — see core/thermal.py's
    # docstring for the model itself). Prediction and the anticipation
    # curve are always advisory only. Active control of a zone's own
    # heating switch (control_entity) is a separate, explicit opt-in per
    # zone — off unless configured, gated by a per-zone pause helper,
    # and bounded by a hard safety floor + a cap on continuous off-time
    # (see core.thermal.decide_dhw_command). This is the one thing in
    # GridLock that writes to a heating device at all.
    # ------------------------------------------------------------------
    def _build_thermal_zone(self, z):
        """Turn one gridwarm.zones apps.yaml entry into its ThermalParams
        plus the entity IDs needed to read it each tick. Thermal mass can
        be given directly, or derived from heat_volume (a room — air plus
        furniture/fabric, an approximation) or tank_litres (a tank —
        exact water physics, no fudge factor needed) — whichever the
        entry provides."""
        if "thermal_mass_wh_per_c" in z:
            mass = float(z["thermal_mass_wh_per_c"])
        elif "tank_litres" in z:
            mass = core_thermal.thermal_mass_from_litres(float(z["tank_litres"]))
        else:
            mass = core_thermal.thermal_mass_from_volume(float(z.get("heat_volume", 30.0)))
        params = core_thermal.ThermalParams(
            heat_loss_degrees=float(z.get("heat_loss_degrees", 0.0)),
            heat_loss_watts=float(z.get("heat_loss_watts", 0.0)),
            heat_gain_static=float(z.get("heat_gain_static", 0.0)),
            heat_max_power=float(z.get("heat_max_power", 0.0)),
            heat_min_power=float(z.get("heat_min_power", 0.0)),
            heat_share=float(z.get("heat_share", 1.0)),
            heating_cop=float(z.get("heating_cop", 3.0)),
            thermal_mass_wh_per_c=mass,
            hysteresis=float(z.get("hysteresis", 0.5)),
            hysteresis_off=float(z.get("hysteresis_off", 0.1)),
            boost_threshold_degrees=float(z.get("boost_threshold_degrees", 2.0)))
        return {"name": z.get("name", "Zone"),
                "internal_temp_entity": z.get("internal_temp_entity"),
                "internal_temp_attribute": z.get("internal_temp_attribute"),
                "target_temp_entity": z.get("target_temp_entity"),
                "target_temp_attribute": z.get("target_temp_attribute"),
                "external_temp_entity": z.get("external_temp_entity"),
                "external_temp_constant": z.get("external_temp_constant"),
                "weather_entity": z.get("weather_entity"),
                "heating_entity": z.get("heating_entity"),
                "cop_entity": z.get("cop_entity"),
                "anticipation_lookahead_steps": int(
                    float(z.get("anticipation_lookahead_hours", 3.0)) * 60 / THERMAL_STEP_MIN),
                # active: false keeps the zone predicted and shown on the
                # dashboard as normal, just with its anticipation forced
                # off -- it tracks whatever the thermostat's own target
                # is, no forecast-driven adjustment either way. For a
                # room you specifically want to run as cool as you can
                # get away with, rather than nudged up ahead of a cold
                # snap like the others.
                "anticipation_sensitivity": (float(z.get("anticipation_sensitivity", 0.3))
                                             if z.get("active", True) else 0.0),
                "anticipation_max_adjust": (float(z.get("anticipation_max_adjust", 2.0))
                                            if z.get("active", True) else 0.0),
                # Active control -- opt-in, off unless control_entity is
                # explicitly set. Everything else about this zone (the
                # prediction, the anticipation curve) stays advisory-only
                # regardless; this is the one specific switch GridWarm is
                # allowed to actually command.
                "control_entity": z.get("control_entity"),
                "control_safety_min_temp": float(z.get("control_safety_min_temp", 45.0)),
                "control_max_off_hours": float(z.get("control_max_off_hours", 6.0)),
                # Usable-hot-water estimate — tank zones only (tank_litres
                # is what makes it a tank rather than a room in the first
                # place, see the mass branch above).
                "tank_litres": float(z["tank_litres"]) if "tank_litres" in z else None,
                "shower_temp_c": float(z.get("shower_temp_c", 40.0)),
                "cold_mains_temp_c": float(z.get("cold_mains_temp_c", 10.0)),
                "litres_per_shower": float(z.get("litres_per_shower", 40.0)),
                # The apps.yaml starting figures, kept separate from
                # params.heat_loss_degrees/heat_loss_watts (which drift
                # as they learn) so the dashboard can show "learned vs
                # where it started".
                "config_heat_loss_degrees": params.heat_loss_degrees,
                "config_heat_loss_watts": params.heat_loss_watts,
                "params": params}

    def _thermal_internal_temp(self, zone):
        """A climate.* entity's own state is its HVAC mode ("heat"/"off"),
        not a temperature — its live reading is the current_temperature
        attribute instead. Auto-detect that by domain so a room zone
        pointed at a climate entity (the common case) works with no
        extra config; internal_temp_attribute can still override it for
        anything that doesn't follow that convention."""
        ent = zone["internal_temp_entity"]
        if not ent:
            return None
        attr = zone["internal_temp_attribute"] or (
            "current_temperature" if ent.startswith("climate.") else None)
        if attr:
            v = self.get_state(ent, attribute=attr)
            try:
                return float(v) if v not in (None, "unknown", "unavailable") else None
            except (ValueError, TypeError):
                return None
        return self.get_float_state(ent, None)

    def _thermal_target_temp(self, zone):
        ent = zone["target_temp_entity"]
        if not ent:
            return None
        attr = zone["target_temp_attribute"]
        if attr:
            v = self.get_state(ent, attribute=attr)
            try:
                return float(v) if v not in (None, "unknown", "unavailable") else None
            except (ValueError, TypeError):
                return None
        return self.get_float_state(ent, None)

    def _thermal_external_temp(self, zone):
        if zone["external_temp_entity"]:
            v = self.get_float_state(zone["external_temp_entity"], None)
            if v is not None:
                return v
        return float(zone["external_temp_constant"]) if zone["external_temp_constant"] is not None else 18.0

    def _thermal_weather_curve(self, weather_entity, now, n_steps, step_minutes, fallback_temp):
        """Best-effort external-temperature forecast for a room zone (a
        tank uses a constant ambient instead — see
        _thermal_external_temp — so never calls this at all). Home
        Assistant moved multi-point weather forecasts from a plain
        attribute to a service call in recent versions; try the older
        attribute first (still exposed by some integrations), then the
        service call, and fall back to holding the current reading flat
        across the whole horizon rather than failing the tick if neither
        is available — a flat approximation is still more useful than no
        forecast at all for an advisory-only feature."""
        points = []
        if weather_entity and self.entity_exists(weather_entity):
            raw = self.get_state(weather_entity, attribute="forecast")
            if isinstance(raw, list) and raw:
                points = raw
            elif weather_entity not in self._thermal_forecast_warned:
                # Only tried once per entity, ever (not once per tick) --
                # confirmed live that a weather integration not supporting
                # this service call still costs a real round trip before
                # the error comes back, and every tick calling it again
                # forever was slow enough to back up this app's single
                # worker thread (see poll_heatpump_diagnostics for the
                # same lesson learned the hard way on a bigger scale).
                try:
                    resp = self.call_service("weather/get_forecasts", entity_id=weather_entity,
                                             type="hourly", return_response=True) or {}
                    points = (resp.get(weather_entity, {}) or {}).get("forecast", []) or []
                except Exception as exc:  # noqa: BLE001 — best-effort only
                    self.log(f"Couldn't get a weather forecast from {weather_entity} for "
                             f"the thermal model ({exc!r}) — holding the current external "
                             "temperature flat across the horizon instead. Not retrying this "
                             "entity again this run.", level="WARNING")
                    self._thermal_forecast_warned.add(weather_entity)
        parsed = []
        for p in points:
            try:
                t = self._iso(p.get("datetime") or p.get("period_start"))
                parsed.append((t, float(p["temperature"])))
            except (KeyError, ValueError, TypeError):
                continue
        parsed.sort(key=lambda p: p[0])

        curve, idx = [], 0
        for i in range(n_steps):
            t = now + timedelta(minutes=step_minutes * i)
            while idx + 1 < len(parsed) and parsed[idx + 1][0] <= t:
                idx += 1
            curve.append(parsed[idx][1] if parsed else fallback_temp)
        return curve

    def _thermal_rate_curve(self, slots):
        """One import rate per 5-min thermal step, aligned to the battery
        plan's own 30-min slots (self.build_slots) — used only to price
        whatever the model predicts it'll draw, for a £ figure alongside
        the kWh one. GridWarm's plan itself doesn't react to price at
        all (see anticipatory_target_curve) — this is purely for
        reporting what today's plan is expected to cost."""
        step_ratio = SLOT_MIN // THERMAL_STEP_MIN
        rate_curve = []
        for s in slots:
            # A single non-finite rate (a bad upstream reading slipping
            # through) would otherwise poison every cost figure it's
            # summed into, not just this one slot -- NaN + anything is
            # NaN.
            imp = s["imp"] if math.isfinite(s["imp"]) else self.default_import
            rate_curve.extend([imp] * step_ratio)
        if len(rate_curve) < THERMAL_HORIZON_STEPS:
            pad_n = THERMAL_HORIZON_STEPS - len(rate_curve)
            rate_curve.extend([rate_curve[-1] if rate_curve else self.default_import] * pad_n)
        return rate_curve[:THERMAL_HORIZON_STEPS]

    def _update_thermal_learning(self, zone, now, internal_temp, external_temp, heating_on):
        """Refines zone['params'].heat_loss_degrees AND heat_loss_watts
        against real cooling periods (heating off at both ends of the
        interval since the last tick) -- the same "time an actual
        cooldown" method core/thermal.py's own docstring describes as
        how the original figures were derived, run continuously against
        real data instead of once by hand.

        Every usable observation (one per tick, at most) is appended to
        a rolling buffer rather than immediately solved and applied --
        with enough of them (spanning a real spread of internal/external
        differences, not just similar nights), a proper line fit
        separates the two loss terms properly instead of solving one
        equation per point with the other term assumed fixed, which
        would silently absorb any error in the "fixed" term into the one
        being solved for. Below that, falls back to the simpler single-
        point method (heat_loss_watts held at its current value) so
        SOME refinement happens during early data collection rather
        than nothing at all.

        Either way, the actual params are only ever nudged via the same
        gradual EMA blend as the load forecast's learned profile -- a
        single fit (even a good one) can't swing them on its own."""
        name = zone["name"]
        state = self.thermal_learning_state.setdefault(name, {})
        prior_ts, prior_temp = state.get("ts"), state.get("temp")
        prior_ext, prior_heating_on = state.get("ext"), state.get("heating_on")

        new_obs = None
        if prior_ts is not None and not prior_heating_on and not heating_on:
            try:
                elapsed_hours = (now - self._iso(prior_ts)).total_seconds() / 3600.0
            except (ValueError, TypeError):  # noqa: BLE001 — a bad timestamp just skips this one
                elapsed_hours = None
            # Roughly one real tick apart (5 min), with slack either way
            # -- too short and noise dominates the maths, too long and
            # something else (a missed tick, a restart) likely happened
            # in between that this simple two-point method can't see.
            if elapsed_hours is not None and 0.05 <= elapsed_hours <= 0.3:
                observed_loss_c_per_hr = (prior_temp - internal_temp) / elapsed_hours
                diff = (prior_temp + internal_temp) / 2.0 - (prior_ext + external_temp) / 2.0
                if abs(diff) >= 1.0:
                    new_obs = {"diff": diff, "rate": observed_loss_c_per_hr}

        state["temp"], state["ext"] = internal_temp, external_temp
        state["heating_on"], state["ts"] = heating_on, now.isoformat()

        observations = state.setdefault("observations", [])
        if new_obs is not None:
            observations.append(new_obs)
            # Caps how far back the fit looks -- keeps it responsive to
            # a slow real change (a season, a dirtier filter) instead of
            # every observation ever recorded weighing in forever, and
            # keeps the persisted file bounded.
            state["observations"] = observations[-500:]

        fitted = core_thermal.fit_heat_loss_params(
            state["observations"], zone["params"].thermal_mass_wh_per_c)
        if fitted is not None:
            degrees, watts = fitted
            # Sanity clamp -- a real building's figures are small
            # positive numbers; a fit wildly outside this range means
            # noisy/sparse data dominating, not a real physical answer
            # worth blending in.
            if 0.0 < degrees < 0.5 and 0.0 <= watts < 5000.0:
                zone["params"].heat_loss_degrees = core_thermal.blend_learned_value(
                    zone["params"].heat_loss_degrees, degrees)
                zone["params"].heat_loss_watts = core_thermal.blend_learned_value(
                    zone["params"].heat_loss_watts, watts)
        elif new_obs is not None:
            implied = core_thermal.implied_heat_loss_degrees(
                new_obs["rate"], new_obs["diff"], zone["params"].heat_loss_watts,
                zone["params"].thermal_mass_wh_per_c)
            if implied is not None and 0.0 < implied < 0.5:
                zone["params"].heat_loss_degrees = core_thermal.blend_learned_value(
                    zone["params"].heat_loss_degrees, implied)

        state["heat_loss_degrees"] = zone["params"].heat_loss_degrees
        state["heat_loss_watts"] = zone["params"].heat_loss_watts
        state["observation_count"] = len(state["observations"])
        self._save_json("thermal_learning_state.json", self.thermal_learning_state)

    def _run_thermal_forecast(self, zone, now, rate_curve):
        internal_temp = self._thermal_internal_temp(zone)
        if internal_temp is None:
            return None
        target_temp = self._thermal_target_temp(zone)
        if target_temp is None:
            target_temp = internal_temp
        external_now = self._thermal_external_temp(zone)
        heating_on = bool(zone["heating_entity"]) and self.get_state(zone["heating_entity"]) == "on"
        self._update_thermal_learning(zone, now, internal_temp, external_now, heating_on)

        external_curve = (self._thermal_weather_curve(
            zone["weather_entity"], now, THERMAL_HORIZON_STEPS, THERMAL_STEP_MIN, external_now)
            if zone["weather_entity"] else [external_now] * THERMAL_HORIZON_STEPS)

        # The GridWarm plan: ease the target down ahead of a forecast
        # warm-up (passive warming will do some of the work) and nudge
        # it up ahead of a forecast cold snap (get ahead of it while
        # it's easy), rather than only reacting to the current outdoor
        # temperature the way a plain weather-compensation curve does.
        # Compared against a fixed-target baseline (no lookahead at
        # all) so the dashboard shows whether the lookahead is actually
        # worth anything, not just asserted to be.
        plan_curve = core_thermal.anticipatory_target_curve(
            target_temp, external_curve, lookahead_steps=zone["anticipation_lookahead_steps"],
            sensitivity=zone["anticipation_sensitivity"], max_adjust=zone["anticipation_max_adjust"])
        trace = core_thermal.simulate(internal_temp, external_curve, plan_curve, zone["params"],
                                       heating_on0=heating_on, step_minutes=THERMAL_STEP_MIN)
        baseline_trace = core_thermal.simulate(
            internal_temp, external_curve, [target_temp] * THERMAL_HORIZON_STEPS, zone["params"],
            heating_on0=heating_on, step_minutes=THERMAL_STEP_MIN)

        steps_per_day = int(24 * 60 / THERMAL_STEP_MIN)
        predicted_kwh_today = sum(s["electrical_kw"] * THERMAL_STEP_MIN / 60.0
                                   for s in trace[:steps_per_day])
        baseline_kwh_today = sum(s["electrical_kw"] * THERMAL_STEP_MIN / 60.0
                                  for s in baseline_trace[:steps_per_day])
        predicted_cost_today = sum(s["electrical_kw"] * THERMAL_STEP_MIN / 60.0 * rate_curve[j]
                                    for j, s in enumerate(trace[:steps_per_day]))
        baseline_cost_today = sum(s["electrical_kw"] * THERMAL_STEP_MIN / 60.0 * rate_curve[j]
                                   for j, s in enumerate(baseline_trace[:steps_per_day]))

        # Downsample to one point per battery-plan slot for the dashboard
        # — keeps payload size comparable to the SoC/solar forecasts
        # rather than publishing every 5-min simulation step.
        per_slot = SLOT_MIN // THERMAL_STEP_MIN
        forecast_data = [
            {"x": (now + timedelta(minutes=SLOT_MIN * (i + 1))).isoformat(),
             "internal_temp": round(trace[j]["internal_temp"], 2),
             "external_temp": round(trace[j]["external_temp"], 2),
             "target_temp": round(trace[j]["target_temp"], 2),
             "heating_on": trace[j]["heating_on"],
             "action": ("Ahead of cold" if plan_curve[j] > target_temp + 0.1
                        else "Easing (warm-up)" if plan_curve[j] < target_temp - 0.1
                        else "Steady")}
            for i, j in enumerate(range(per_slot - 1, len(trace), per_slot))]

        live_cop = self.get_float_state(zone["cop_entity"], None) if zone["cop_entity"] else None
        usable_litres = showers = None
        if zone["tank_litres"]:
            usable_litres = core_thermal.usable_hot_water_litres(
                internal_temp, zone["tank_litres"], zone["shower_temp_c"],
                zone["cold_mains_temp_c"])
            showers = core_thermal.showers_available(usable_litres, zone["litres_per_shower"])
        return {"name": zone["name"],
                "current_temp": round(internal_temp, 2),
                "target_temp": round(target_temp, 2),
                "external_temp": round(external_now, 2),
                "heating_on": heating_on,
                # The model's own first-step decision (already reflects
                # the anticipation-adjusted plan_curve and its hysteresis
                # band) -- this is what active control, if enabled for
                # this zone, actually commands. Distinct from the LIVE
                # "heating_on" above, which is what the hardware is
                # observed doing right now, not what the model wants.
                "desired_heating_on": trace[0]["heating_on"],
                "cop": round(live_cop, 2) if live_cop is not None else zone["params"].heating_cop,
                "predicted_kwh_today": round(predicted_kwh_today, 2),
                "predicted_kwh_today_baseline": round(baseline_kwh_today, 2),
                "predicted_cost_today": round(predicted_cost_today, 2),
                "predicted_cost_today_baseline": round(baseline_cost_today, 2),
                "usable_hot_water_litres": round(usable_litres, 1) if usable_litres is not None else None,
                "showers_available": round(showers, 1) if showers is not None else None,
                "learned_heat_loss_degrees": round(zone["params"].heat_loss_degrees, 4),
                "config_heat_loss_degrees": round(zone["config_heat_loss_degrees"], 4),
                "learned_heat_loss_watts": round(zone["params"].heat_loss_watts, 1),
                "config_heat_loss_watts": round(zone["config_heat_loss_watts"], 1),
                "thermal_learning_observations": self.thermal_learning_state.get(
                    zone["name"], {}).get("observation_count", 0),
                "forecast_data": forecast_data}

    def _thermal_pause_helper(self, zone_name):
        slug = re.sub(r"[^a-z0-9]+", "_", zone_name.lower()).strip("_")
        return f"input_boolean.gridlock_gridwarm_control_{slug}"

    def _control_thermal_zone(self, zone, now, result):
        """The one place anything in GridLock writes to a heating device
        — off by default, only runs at all if control_entity is set for
        this zone. Gated by a per-zone pause helper (auto-created, same
        pattern as input_boolean.gridlock_enable) so it can be switched
        off instantly from the UI without editing apps.yaml or
        restarting. See core.thermal.decide_dhw_command for the actual
        safety-floor / max-continuous-off-time logic this defers to —
        this method is just the HA read/write glue around it."""
        entity = zone["control_entity"]
        if not entity:
            result["control"] = None
            return
        pause_helper = self._thermal_pause_helper(zone["name"])
        state = self._thermal_control_state.setdefault(
            zone["name"], {"off_since": None, "last_commanded": None})
        # The global master switch (defaults read_only) always wins over
        # a zone's own per-zone pause helper -- both have to say "go"
        # for anything to actually be written.
        read_only = self.get_state(self.ent_gridwarm_mode) != "active"
        paused = read_only or self.get_state(pause_helper) == "off"
        # Always published, even while paused/before the first real
        # decision -- the dashboard needs somewhere to show control
        # status regardless of whether a write is about to happen.
        result["control"] = {"entity": entity, "pause_helper": pause_helper,
                              "paused": paused, "commanded": state["last_commanded"]}
        if paused:
            return
        off_duration_hours = ((now - state["off_since"]).total_seconds() / 3600.0
                              if state["off_since"] else 0.0)
        command = core_thermal.decide_dhw_command(
            result["desired_heating_on"], result["current_temp"],
            zone["control_safety_min_temp"], off_duration_hours, zone["control_max_off_hours"])
        state["off_since"] = None if command else (state["off_since"] or now)
        result["control"]["commanded"] = command
        if command == state["last_commanded"]:
            return
        state["last_commanded"] = command
        # target={"entity_id": ...}, not the plain entity_id= kwarg --
        # matches the one other place in this codebase that actually
        # commands hardware (core/inverter.py's SigenergyAdapter).
        self.call_service(f"switch/turn_{'on' if command else 'off'}", target={"entity_id": entity})
        # decide_dhw_command only ever forces ON, never OFF -- so "off"
        # can only mean the plan itself wanted off, no override needed.
        if not command:
            reason = "GridWarm plan — heating off"
        elif result["current_temp"] <= zone["control_safety_min_temp"]:
            reason = "Safety floor reached — heating forced on regardless of plan"
        elif off_duration_hours >= zone["control_max_off_hours"]:
            reason = ("Held off long enough to hit the safety cap — heating on to give the "
                      "heat pump's own cycles a chance to run")
        else:
            reason = "GridWarm plan — heating on"
        self._log_decision(f"GridWarm: {zone['name']} {'ON' if command else 'OFF'}", reason)

    def _publish_thermal_forecast(self, now):
        if not self.thermal_zones:
            return
        rate_curve = self._thermal_rate_curve(self.build_slots(now))
        zones_out = []
        for zone in self.thermal_zones:
            try:
                result = self._run_thermal_forecast(zone, now, rate_curve)
            except Exception as exc:  # noqa: BLE001 — one bad zone shouldn't drop the rest
                self.log(f"Thermal forecast for zone '{zone['name']}' failed: {exc!r}",
                         level="WARNING")
                result = None
            if result:
                zones_out.append(result)
                try:
                    self._control_thermal_zone(zone, now, result)
                except Exception as exc:  # noqa: BLE001 — never let a control write crash the tick
                    self.log(f"Thermal control for zone '{zone['name']}' failed: {exc!r}",
                             level="ERROR")
        if not zones_out:
            return
        total_cost = sum(z["predicted_cost_today"] for z in zones_out)
        baseline_cost = sum(z["predicted_cost_today_baseline"] for z in zones_out)
        saving = baseline_cost - total_cost
        plan_summary = (
            f"Anticipating temperature swings ahead — easing off before forecast warm-ups, "
            f"adding a little heat ahead of cold snaps — predicted £{total_cost:.2f} today "
            f"across {len(zones_out)} zone{'s' if len(zones_out) != 1 else ''} vs £{baseline_cost:.2f} "
            f"reacting only to the current temperature "
            f"({'saving' if saving >= 0 else 'costing'} £{abs(saving):.2f}).")
        self.set_state("sensor.gridlock_gridwarm",
                       state=f"{zones_out[0]['current_temp']:.1f}",
                       attributes={"friendly_name": "GridLock GridWarm",
                                   "unit_of_measurement": "°C",
                                   "icon": "mdi:heat-pump",
                                   "plan_summary": plan_summary,
                                   "predicted_cost_today_total": round(total_cost, 2),
                                   "predicted_cost_today_baseline_total": round(baseline_cost, 2),
                                   "control_mode": self.get_state(self.ent_gridwarm_mode) or "read_only",
                                   "zones": zones_out})

    def _refresh_diagnostic_entities(self):
        """Re-resolve the watched entity list from the static "entities:"
        config plus a live substring scan for "entity_prefix:" — run at
        startup and again on every diagnostics poll so an entity that
        appears on the device later (a firmware update exposing a new
        sensor, say) gets picked up without an add-on restart. AppDaemon
        has no access to HA's device/entity registry (only entity_ids
        and their live state), so "everything for this device" has to be
        done as an entity_id substring match rather than a real registry
        lookup — the assumption is the same naming convention this
        controller already uses throughout ("loft_heatpump_controller_*"
        etc.), not a guess specific to any one device."""
        entities = list(self.gridwarm_diagnostic_static_entities)
        if self.gridwarm_diagnostic_prefix:
            matched = self.registry.find_all(contains=self.gridwarm_diagnostic_prefix)
            entities = list(dict.fromkeys(entities + matched))
        self.gridwarm_diagnostic_entities = entities

    def _on_heatpump_service_call(self, event_name, data, kwargs):
        """Fires on EVERY service call anywhere in HA — filtered down to
        just the watched heat pump entities. Deliberately a live event
        listener rather than reconstructing this after the fact from
        history: the history API only has the resulting state, not which
        service/domain/value actually caused it, which is exactly the
        detail that distinguishes "something external commanded this"
        from "the device reported its own state" (confirmed against a
        real mystery write this session — a number.set_value call on an
        entity GridLock has never touched)."""
        entity_ids = (data.get("service_data") or {}).get("entity_id")
        if entity_ids is None:
            entity_ids = (data.get("target") or {}).get("entity_id")
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        watched = [e for e in (entity_ids or []) if e in self.gridwarm_diagnostic_entities]
        if not watched:
            return
        now = self.get_now()
        service_data = data.get("service_data") or {}
        value = service_data.get("value")
        if value is None:
            value = service_data.get("option")
        for eid in watched:
            self.heatpump_events.append({
                "ts": now.isoformat(), "entity_id": eid,
                "domain": data.get("domain"), "service": data.get("service"),
                "value": value})
        self.heatpump_events = self.heatpump_events[-100:]
        self._save_json("heatpump_events.json", self.heatpump_events)
        self._notify(
            "GridLock: external command on a watched heat pump entity",
            f"{', '.join(watched)} — {data.get('domain')}.{data.get('service')}"
            f"{f' (value: {value})' if value is not None else ''}. "
            "Not from GridLock — GridWarm never calls this service on this entity.")

    def poll_heatpump_diagnostics(self, kwargs):
        self._refresh_diagnostic_entities()
        entities = self.gridwarm_diagnostic_entities
        if not entities:
            return
        live_status = {}
        for eid in entities:
            full = self.get_state(eid, attribute="all") or {}
            attrs = full.get("attributes") or {}
            live_status[eid] = {"name": attrs.get("friendly_name", eid),
                                 "state": full.get("state"),
                                 "unit": attrs.get("unit_of_measurement")}
        # One batched history call for every watched entity, not one
        # request per entity -- get_history() accepts a list and fetches
        # them all in a single /api/history/period round trip. With
        # entity_prefix matching everything on a device (a raw controller
        # dump can easily be 100+ entities), doing this one-at-a-time was
        # slow enough, run synchronously on this app's single worker
        # thread, to back up the real planning tick() behind it.
        sessions = {}
        try:
            hist = self.get_history(entity_id=entities, days=1, no_attributes=True) or []
        except Exception as exc:  # noqa: BLE001 — best-effort, a bad fetch shouldn't crash the poll
            self.log(f"Heat pump diagnostics history fetch failed: {exc!r}", level="WARNING")
            hist = []
        changes_by_entity = {}
        for entity_hist in hist:
            for s in (entity_hist or []):
                eid = s.get("entity_id")
                ts = s.get("last_changed")
                if not eid or ts is None:
                    continue
                # get_history() returns last_changed as a real datetime,
                # not a string -- convert before this ends up in a JSON
                # sensor attribute.
                ts = ts.isoformat() if hasattr(ts, "isoformat") else ts
                changes_by_entity.setdefault(eid, []).append({"ts": ts, "state": s.get("state")})
        temperature_series = {}
        for eid, changes in changes_by_entity.items():
            unit = (live_status.get(eid) or {}).get("unit")
            # A temperature reading becomes a real-valued curve (to chart
            # alongside what the heat pump was actually doing); a
            # discrete/binary entity becomes on/off-style sessions; a
            # numeric-but-not-temperature sensor (voltage, frequency,
            # wifi signal, an ever-changing counter) is neither -- a
            # session log of every distinct reading is just noise, and
            # it isn't a temperature worth charting either, so it's
            # dropped (its current value still shows in Live status).
            segs, series = core_diagnostics.classify_entity_history(changes, unit)
            # Only entities that actually changed state in the window are
            # worth a timeline row -- most of a raw Modbus register dump
            # (capability flags, reserved bits) never changes at all, and
            # a one-session "always been this since forever" row would
            # just be noise next to the ones that genuinely did something.
            if len(segs) > 1:
                sessions[eid] = segs[-20:]
            if series:
                temperature_series[eid] = series[-200:]
        self.set_state("sensor.gridlock_heatpump_diagnostics",
                       state=str(len(self.heatpump_events)),
                       attributes={"friendly_name": "GridLock Heat Pump Diagnostics",
                                   "icon": "mdi:heat-pump-outline",
                                   "live_status": live_status,
                                   "sessions": sessions,
                                   "temperature_series": temperature_series,
                                   "watched_entities": entities,
                                   "external_events": self.heatpump_events[-20:]})

    def _load_circuit_state(self):
        state = self._load_json("circuit_state.json", {})
        self.circuit_day = state.get("day")
        self.circuit_today_kwh = state.get("today_kwh", {})

    def _save_circuit_state(self):
        self._save_json("circuit_state.json",
                         {"day": self.circuit_day, "today_kwh": self.circuit_today_kwh})

    def publish_circuits(self, labeled_entities, now):
        """Live power breakdown for the dashboard — the EV charger (already
        discovered), any Shelly relay's own power sensor (naming
        convention alone, core/registry.py's find_shelly_power_entities —
        works with zero setup, but only for Shelly specifically), and
        anything the user tagged with the "GridLock Power" label in HA
        (see ha_support.yaml's template sensor for why a label rather
        than an apps.yaml list: registry access isn't something
        AppDaemon itself can do, so HA's own Jinja engine does the lookup
        and exposes it as a normal entity attribute instead — this one
        works for any brand, not just Shelly). Renaming what a circuit
        represents needs nothing here — it's just that entity's own
        friendly_name, already editable from Settings > Devices &
        services > Entities."""
        ids = list(dict.fromkeys(
            ([self.ent_ev_power] if self.ent_ev_power else [])
            + self.registry.find_shelly_power_entities()
            + labeled_entities))

        today_iso = now.date().isoformat()
        if self.circuit_day is None:
            self.circuit_day = today_iso
        elif today_iso != self.circuit_day:
            # Roll yesterday's accumulated totals into history and start
            # today fresh — same day-rollover shape as _roll_savings_day,
            # kept in its own file/dict rather than folded into that one
            # since circuits are a dynamic, variable-length set keyed by
            # entity_id, not a handful of fixed named figures.
            self.circuit_history[self.circuit_day] = {
                eid: round(kwh, 3) for eid, kwh in self.circuit_today_kwh.items()}
            self.circuit_history = dict(list(self.circuit_history.items())[-60:])
            self._save_json("circuit_history.json", self.circuit_history)
            self.circuit_day = today_iso
            self.circuit_today_kwh = {}

        circuits = []
        for eid in ids:
            if not self.entity_exists(eid):
                continue
            state = self.get_state(eid, attribute="all") or {}
            attrs = state.get("attributes", {}) or {}
            try:
                power_w = float(state.get("state"))
            except (TypeError, ValueError):
                power_w = None
            if power_w is not None:
                # Self-tracked rather than read off the Shelly's own
                # paired "*_energy" sensor: that sensor is typically a
                # total_increasing lifetime/since-reset counter (meant
                # for HA's own Energy dashboard + a utility_meter helper
                # to slice it into days), not already scoped to "today"
                # the way this figure is labelled — reading it directly
                # would have quietly shown the wrong number. Accumulating
                # this tick's own ~5-minute slice is the same technique
                # _update_savings already uses for its shadow simulation.
                self.circuit_today_kwh[eid] = (
                    self.circuit_today_kwh.get(eid, 0.0) + power_w / 1000.0 * (5 / 60))
            circuits.append({
                "entity_id": eid,
                "name": attrs.get("friendly_name", eid),
                "power_w": power_w,
                "energy_kwh": round(self.circuit_today_kwh.get(eid, 0.0), 3),
            })
        self._save_circuit_state()

        history = [{"date": d, "values": v} for d, v in sorted(self.circuit_history.items())]
        self.set_state("sensor.gridlock_circuits",
                       state=str(len(circuits)),
                       attributes={"friendly_name": "GridLock Power Circuits",
                                   "icon": "mdi:flash",
                                   "circuits": circuits,
                                   "history": history})

    def _publish_load_management(self, site_import_kw, load_mgmt_state, safe_charge_kw):
        # state must be a string — HA's API rejects a raw float/int with a
        # 400 Bad Request (confirmed against a real install: every other
        # set_state() call in this file already wraps its numeric value in
        # an f-string, e.g. publish_solar_forecast's state=f"{today_kwh:.2f}",
        # this one just missed it). Also renamed the "state" attribute key
        # to "load_state" — a HA entity's real top-level state and an
        # attribute confusingly also called "state" is exactly the kind of
        # thing that's easy to misread in Developer Tools > States.
        site_import_a = site_import_kw * 1000.0 / self.mains_voltage
        self.set_state("sensor.gridlock_load_management",
                       state=f"{site_import_a:.1f}",
                       attributes={"friendly_name": "GridLock Load Management",
                                   "unit_of_measurement": "A",
                                   "icon": "mdi:fuse",
                                   "main_fuse_amps": self.main_fuse_amps,
                                   "warn_amps": round(self.load_mgmt_warn_kw * 1000.0 / self.mains_voltage, 1),
                                   "critical_amps": round(self.load_mgmt_critical_kw * 1000.0 / self.mains_voltage, 1),
                                   "safe_charge_kw": round(safe_charge_kw, 2),
                                   "load_state": load_mgmt_state or "normal"})

    def publish_grid_connection_status(self):
        if not self.ent_grid_connection_status:
            return
        off = self.grid_connection_off()
        self.set_state("sensor.gridlock_grid_connection",
                       state="Off Grid" if off else "On Grid",
                       attributes={"friendly_name": "GridLock Grid Connection",
                                   "icon": ("mdi:transmission-tower-off" if off
                                            else "mdi:transmission-tower"),
                                   "raw_state": off or "connected"})

    def publish_storm_status(self):
        reason = self.storm_active()
        self.set_state("sensor.gridlock_storm_status",
                       state="Active" if reason else "Clear",
                       attributes={"friendly_name": "GridLock Storm Watch",
                                   "icon": ("mdi:weather-lightning" if reason
                                            else "mdi:weather-partly-cloudy"),
                                   "reason": reason or "No active alerts"})

    def update_daily_financials(self):
        now = self.get_now()
        self._update_energy_cost_tracking(now)
        self._update_warranty_tracking(now)
        imp = self.get_float_state(self.ent_daily_import_cost, self.tracked_import_cost_today)
        exp = self.get_float_state(self.ent_daily_export_value, self.tracked_export_value_today)
        stand = self.get_float_state(self.ent_daily_standing_charge)
        net = round(imp + stand - exp, 2)
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

        planned_kwh, completed_kwh = self.tariff_provider.ev_dispatch_totals()
        self.set_state("sensor.gridlock_ev_dispatch_kwh",
                       state=f"{planned_kwh:.2f}",
                       attributes={"friendly_name": "GridLock EV Dispatch kWh",
                                   "unit_of_measurement": "kWh",
                                   "icon": "mdi:ev-station",
                                   "planned_kwh": planned_kwh,
                                   "completed_kwh": completed_kwh})

    # ------------------------------------------------------------------
    # FAILSAFE
    # ------------------------------------------------------------------
    def _update_failsafe_liveness(self, now):
        soc_state = self.get_state(self.ent_soc)
        if soc_state not in (None, "unknown", "unavailable"):
            self._ha_last_live = now

        # Solar forecasting is optional (a no-panels install has no
        # Solcast entities at all, by design) — that absence isn't a
        # link failure, so it must never hold the failsafe in permanent
        # DEGRADED. Only entities that exist but have stopped updating
        # count as a genuine dropped connection.
        solcast_configured = any(self.entity_exists(e) for e in self.ent_solcast)
        if not solcast_configured:
            self._solcast_last_live = now
        elif any(self.get_state(e) not in (None, "unknown", "unavailable")
                for e in self.ent_solcast if self.entity_exists(e)):
            self._solcast_last_live = now

        return core_failsafe.check(now, ha_last_live=self._ha_last_live,
                                    solcast_last_live=self._solcast_last_live)

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
        self._resolve_mode()

        if self.get_state("input_boolean.gridlock_enable") == "off":
            self.set_state("sensor.gridlock_status", state="Disabled")
            return

        now = self.get_now()

        # Deadman switch: HA link or Solcast unreachable for >15 minutes
        # continuously -> drop to local self-consumption rather than plan
        # against data that may no longer reflect reality.
        fs = self._update_failsafe_liveness(now)
        if fs.state == core_failsafe.FailsafeState.DEGRADED:
            self.apply(self.mode_eco, self.discharge_kw, self.charge_kw,
                       "Failsafe (safe mode)", fs.reason, "")
            return

        # Main fuse load management: the single highest-priority override,
        # above even Storm Watch. Reacts to the live combined site-import
        # reading (self.ent_grid_power, net of everything — house, EV,
        # hot tub, heat pump, battery) rather than anything planned,
        # since GridLock has no visibility into what the EV/hot tub/heat
        # pump are about to do. Deliberately never discharges the battery
        # to "help" — the whole point of the overnight cheap-rate window
        # this is actually meant for is to charge it, so draining it
        # right back out would defeat the purpose. The only thing this
        # does is smoothly throttle the battery's own charge rate down
        # to whatever headroom is actually left once every other load
        # (Home Assistant knows nothing about) is accounted for — full
        # rate whenever there's room, reduced (never negative/discharge)
        # when there isn't, down to 0 only if genuinely no headroom
        # remains at all.
        if self.load_mgmt_enabled:
            site_import_kw = (self._read_live_kw(self.ent_grid_power) or 0.0) \
                if self.ent_importing and self.get_state(self.ent_importing) == "on" else 0.0
            # Isolate what everything ELSE (not the battery's own
            # charging) is drawing right now, so throttling the charge
            # rate down doesn't chase its own tail — site_import_kw
            # already includes whatever the battery itself is currently
            # pulling for charging.
            battery_charging_now = bool(self.ent_battery_charging) \
                and self.get_state(self.ent_battery_charging) == "on"
            battery_kw_now = (self._read_live_kw(self.ent_battery_power) or 0.0) \
                if battery_charging_now else 0.0
            other_loads_kw = max(0.0, site_import_kw - battery_kw_now)

            throttling = (other_loads_kw + self.charge_kw) > self.load_mgmt_warn_kw
            # Throttle toward the critical ceiling, not the (lower) warn
            # line — warn is only "start paying attention", the real
            # limit charging should never push past is critical, so
            # aiming there uses all the genuinely safe headroom rather
            # than cutting off early.
            safe_charge_kw = max(0.0, min(self.charge_kw, self.load_mgmt_critical_kw - other_loads_kw)) \
                if throttling else self.charge_kw
            load_mgmt_state = "throttle" if throttling else None

            self._publish_load_management(site_import_kw, load_mgmt_state, safe_charge_kw)
            if load_mgmt_state != self._prev_load_mgmt_state:
                if load_mgmt_state:
                    self._notify(
                        "GridLock: Load Management engaged",
                        f"Other loads are drawing {other_loads_kw:.1f}kW — battery charging "
                        f"reduced to {safe_charge_kw:.1f}kW to stay clear of your "
                        f"{self.main_fuse_amps:.0f}A main fuse (never discharges to help — "
                        "just charges slower).")
                elif self._prev_load_mgmt_state:
                    self._notify("GridLock: Load Management cleared",
                                "Full charge rate available again — normal planning resumed.")
            self._prev_load_mgmt_state = load_mgmt_state
            if load_mgmt_state == "throttle":
                if safe_charge_kw > 0.05:
                    # Deliberately no live numbers in this reason string —
                    # _log_decision() only dedupes on an EXACT state+reason
                    # match, and other_loads_kw/safe_charge_kw fluctuate
                    # most ticks even while nothing meaningful has
                    # changed, which would otherwise log a near-duplicate
                    # decision-log entry every 5 minutes throttling is
                    # active instead of one "engaged" entry plus the
                    # existing hourly "Still: ..." check-in. The exact
                    # live figures are still in the notification (sent
                    # once, on genuine transition) and the dashboard
                    # gauge (updated every tick regardless).
                    self.apply(self.mode_charge, 0.0, round(safe_charge_kw, 2),
                               "Load Management — Charging Throttled",
                               "Battery charge rate reduced to fit available headroom under the "
                               f"{self.main_fuse_amps:.0f}A main fuse", "")
                else:
                    self.apply(self.mode_eco, self.discharge_kw, 0.0,
                               "Load Management — Charging Paused",
                               "Other loads alone leave no headroom to charge — paused, "
                               "not discharging", "")
                return

        soc0 = self.get_float_state(self.ent_soc, 50.0)
        # Read fresh every tick, not just once at startup — a newly
        # labelled entity should show up within 5 minutes, not require an
        # AppDaemon restart. sensor.gridlock_power_circuits is the
        # trigger-based HA-side template (ha_support.yaml) bridging the
        # Label registry, which AppDaemon itself can't query at all.
        labeled_circuits = self._attr_list("sensor.gridlock_power_circuits", "entity_ids")
        # Same combined set publish_circuits() itself builds (label +
        # Shelly naming convention) — kept in step so a Shelly picked up
        # by naming alone gets subtracted from the whole-house learning
        # and forecast in its own right too, not just shown live.
        all_circuits = list(dict.fromkeys(
            self.registry.find_shelly_power_entities() + labeled_circuits))
        self.load_provider.circuit_power_entities = all_circuits
        self.load_provider.sample(now)
        self.publish_circuits(labeled_circuits, now)
        self._update_savings(now)
        self.publish_solar_forecast(now)
        self.publish_storm_status()
        self.publish_grid_connection_status()
        self._publish_thermal_forecast(now)

        slots = self.build_slots(now)
        result = self._solve_plan(slots, soc0, now)
        if result.infeasible:
            # Shouldn't happen — the on-peak reserve constraint is a soft
            # penalty specifically so the LP always has a solution (see
            # core/optimizer.py) — but if the solver ever reports
            # otherwise, its returned values can't be trusted (observed:
            # a nonsensical SoC trace on a genuinely infeasible solve
            # during review). Fall back to safe self-consumption rather
            # than act on a plan that might not reflect real energy
            # balance.
            self.log("LP solve reported infeasible — falling back to self-consumption "
                     "for this tick.", level="ERROR")
            self.apply(self.mode_eco, self.discharge_kw, self.charge_kw,
                       "Fault (safe mode)", "Optimiser reported infeasible", "")
            return
        slots, trace, cost_trace, grid_cost = result.slots, result.trace, result.cost_trace, result.grid_cost
        self._track_plan_accuracy(now, grid_cost, slots, soc0)
        self._check_solar_deficit(now, slots, cost_trace)

        cur = slots[0]
        action = core_optimizer.action(cur)
        ev_active = bool(self.ent_ev) and self.get_state(self.ent_ev) == "on"
        session = self.active_saving_session(now)
        storm = self.storm_active()
        off_grid = self.grid_connection_off()

        if bool(off_grid) != self._prev_off_grid:
            # Storm Watch is a separate, prediction-based signal (SSEN
            # outage feeds, weather alerts) that can easily be the actual
            # cause of a real islanding event — folded into the notification
            # as a likely-cause hint when both are true at once, rather
            # than reporting two disconnected-sounding alerts for what's
            # probably the same real event.
            cause = f" Likely cause: {storm}." if storm else ""
            if off_grid:
                self._notify("GridLock: Off-grid detected",
                            f"Grid connection status: {off_grid}.{cause} Forcing Maximum "
                            "Self Consumption until grid connection returns.")
            else:
                self._notify("GridLock: Grid connection restored",
                            "Back to normal cost-optimised planning.")
        self._prev_off_grid = bool(off_grid)

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

        # Computed whenever storm is active regardless of off_grid, not
        # just inside a "storm and not off_grid" branch — both the label
        # below and the apply() logic further down need it either way.
        storm_decision = None
        outage_hours = None
        if storm:
            outage_hours = self._estimated_outage_hours(now)
            expected_load_kwh = self._expected_load_kwh(now, outage_hours)
            usable_kwh = max(0.0, soc0 - self.floor_soc) / 100.0 * self.cfg.battery_kwh
            storm_decision = core_optimizer.storm_decision(
                soc0, storm_target_soc=self.storm_target_soc,
                discharge_kw=self.discharge_kw, charge_kw=self.charge_kw,
                ev_concurrent_charge_kw=self.ev_concurrent_charge_kw, ev_active=ev_active,
                usable_kwh=usable_kwh, expected_load_kwh=expected_load_kwh)

        # Logged only on the transition into/out of this state, not every
        # tick it remains true — logging it unconditionally each tick (as
        # first shipped) permanently broke _log_decision's own dedup for
        # BOTH this and whatever the normal plan action logs right after
        # it, since dedup only ever compares against the single immediately
        # preceding entry and these two calls kept alternating. off_grid
        # excluded: off-grid+storm always holds regardless of reserve (see
        # below), so there's nothing to announce standing down from.
        reserve_sufficient_now = (bool(storm_decision) and not storm_decision["override"]
                                  and not off_grid)
        if reserve_sufficient_now and not self._prev_storm_reserve_sufficient:
            self._log_decision(
                "Storm Watch — Reserve Sufficient",
                f"Weather alert ({storm}): already enough charge banked for the "
                "estimated outage — continuing the normal plan instead of "
                "holding/charging")
        self._prev_storm_reserve_sufficient = reserve_sufficient_now

        if off_grid and storm:
            # Always the plain holding label here regardless of what
            # storm_decision says — off-grid means there's genuinely no
            # grid to export TO at all, which "reserve is already
            # sufficient" doesn't change, so the apply() logic below never
            # honours storm_decision's override flag in this combination.
            live_label = "Storm Watch — Holding (Off-Grid)"
        elif off_grid:
            live_label = "Off-Grid — Self Consumption"
        elif storm:
            live_label = storm_decision["state"]
        elif session and soc0 > self.floor_soc + 5:
            live_label = "Saving Session Export"
        elif ev_active:
            live_label = "Charging (EV concurrent)" if action == "CHARGE" else "EV Protection"
        else:
            live_label = None

        plan_html = self.publish_plan(slots, trace, cost_trace, grid_cost, soc0, now, live_label)
        self.publish_compare(self.build_slots(now), soc0, grid_cost, now)
        self.plan = slots

        target = next((trace[i] for i, s in enumerate(slots)
                       if core_optimizer.action(s) == "CHARGE" and
                       (i + 1 == len(slots) or
                        core_optimizer.action(slots[i + 1]) != "CHARGE")),
                      max(self.floor_soc, trace[0]))
        self.set_state("sensor.gridlock_target_soc", state=str(int(target)),
                       attributes={"friendly_name": "GridLock Target SoC",
                                   "unit_of_measurement": "%"})

        # --- Off-grid + Storm Watch together: Storm Watch's own "hold, no
        #     export" behaviour already does exactly what off-grid needs,
        #     so reuse it rather than a separate generic command — but
        #     never its CHARGING branch, which specifically requires
        #     drawing from the grid, impossible while actually islanded. ---
        if off_grid and storm:
            self.apply(self.mode_eco, storm_decision["disch_kw"], storm_decision["charge_kw"],
                       "Storm Watch — Holding (Off-Grid)",
                       f"Off-grid ({off_grid}) during a storm alert ({storm}): holding "
                       "charge, exports suspended, grid-charging impossible while islanded",
                       plan_html)
            return

        # --- Off-grid on its own (no storm signal) overrides everything:
        #     no grid exists to trade against, so there's nothing to plan
        #     for beyond running the inverter's own self-consumption logic
        #     on whatever PV/battery is actually available. ---
        if off_grid:
            self.apply(self.mode_eco, self.discharge_kw, self.charge_kw,
                       "Off-Grid — Self Consumption",
                       f"Grid connection status: {off_grid} — no grid to trade "
                       "against, running on PV/battery only until it returns",
                       plan_html)
            return

        # --- Storm Watch overrides everything else: charge & hold, unless
        #     there's already enough banked to cover the estimated outage
        #     (see storm_decision's usable_kwh/expected_load_kwh), in
        #     which case it stands down and the normal price-optimised
        #     action below runs exactly as if there were no storm at all —
        #     Storm Watch still shows Active, it just isn't overriding
        #     anything right now. ---
        if storm:
            if storm_decision["charging"]:
                self.apply(self.mode_charge, storm_decision["disch_kw"], storm_decision["charge_kw"],
                           "Storm Watch — Charging",
                           f"Weather alert ({storm}): charging to "
                           f"{self.storm_target_soc:.0f}% regardless of rates",
                           plan_html)
                return
            if storm_decision["override"]:
                self.apply(self.mode_eco, storm_decision["disch_kw"], storm_decision["charge_kw"],
                           "Storm Watch — Holding",
                           f"Weather alert ({storm}): holding charge, "
                           "exports suspended", plan_html)
                return
            # Reserve sufficient: already logged (once, on the transition
            # into this state) further up — nothing more to do here, just
            # fall through to the normal price-optimised action below,
            # which logs its own decision exactly as it would with no
            # storm active at all.

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
                # "Command Charging (PV First)" keeps the inverter's own PV
                # routing alive (unlike MODE_BYPASS, which switches that off
                # entirely) — charge_kw=0 just blocks the battery itself from
                # taking any of that PV, so surplus solar goes to the house/
                # EV instead of topping up the battery while it's charging.
                self.apply(self.inverter_adapter.MODE_CHARGE_PV_FIRST, 0.0, 0.0,
                           "EV Protection",
                           "EV charging: battery charge/discharge both "
                           "clamped to 0 — PV still free to serve the house",
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
        elif cur["imp"] <= self.cheap_rate:
            # Mirrors the LP's own hard constraint (core/optimizer.py:
            # batt_to_load[i] == 0 whenever imp <= cheap_rate) — importing
            # fresh at this same cheap rate is strictly cheaper than
            # cycling stored charge to avoid paying it. Genuinely switches
            # the inverter into Sigenergy's own documented pass-through
            # state rather than approximating it with disch_kw=0 inside
            # Self Consumption mode — the dashboard's "Bypass" label must
            # match what the hardware is actually doing.
            self.apply(self.inverter_adapter.MODE_BYPASS, 0.0, self.charge_kw,
                       "Bypass",
                       f"Cheap off-peak import @ {cur['imp']*100:.1f}p — "
                       "bypassing battery, holding charge for later", plan_html)
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
                # "Maximum Self Consumption" still has the inverter actively
                # hunting for battery power that isn't there once it's at
                # the floor — "Unknown" is Sigenergy's own documented
                # bypass state, passing load straight through to the grid
                # instead. Not while PV is actively generating: an empty
                # battery with free solar arriving should charge from it
                # via normal self-consumption, not sit bypassed.
                mode = self.inverter_adapter.MODE_BYPASS
                state = f"{state} — Forced Bypass"
                reason = f"{reason} (battery at floor — bypass mode)"
        self._log_decision(state, reason)

        desired = {"mode": mode, "disch_kw": disch_kw, "charge_kw": charge_kw}
        if self.ent_discharge_cutoff:
            desired["discharge_cutoff"] = self.floor_soc
        current = self.inverter_adapter.read_state(self)
        writes = self.inverter_adapter.plan_writes(current, desired)
        self.inverter_adapter.execute(self, writes)

        self.set_state("sensor.gridlock_status", state=state,
                       attributes={"friendly_name": "GridLock Status",
                                   "icon": "mdi:brain",
                                   "action": reason, "reason": reason,
                                   "plan_html": plan_html,
                                   "soc_entity": self.ent_soc,
                                   "import_rate_entity": self.ent_import_rate,
                                   "export_rate_entity": self.ent_export_rate,
                                   "ev_entity": self.ent_ev,
                                   "dispatch_entity": self.ent_dispatch,
                                   "saving_events_entity": self.ent_saving_events,
                                   "daily_import_cost_entity": self.ent_daily_import_cost,
                                   "daily_export_value_entity": self.ent_daily_export_value,
                                   "daily_standing_charge_entity": self.ent_daily_standing_charge,
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
                                   "export_degradation_cost": self.export_degradation,
                                   "cheap_rate_threshold": self.cheap_rate,
                                   "thermal_derate": derate,
                                   "storm_watch_entities": [e for e, _ in self.storm_sources if e] or None,
                                   "ssen_postcode": self.ssen_postcode or None})

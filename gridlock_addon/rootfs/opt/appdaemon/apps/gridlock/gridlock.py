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
from core.tariff import KrakenTariffProvider
from core.forecast import SolcastForecastProvider, LearnedLoadForecastProvider
from core import thermal as core_thermal
from core import space_heating as core_space_heating
from core import flow_control as core_flow
from core import dhw_plan as core_dhw
from core import effective_price as core_effective_price
from core import diagnostics as core_diagnostics
from core import warranty as core_warranty
from core.ev_schedule import find_cheapest_window
from core import ev_charge_assist as core_ev_charge_assist
from core import tariff_backtest as core_tariff_backtest

STATE_FILES = ("load_profile.json", "savings_state.json", "savings_history.json",
               "cost_tracking_state.json", "decision_log.json",
               "circuit_state.json", "circuit_history.json")

# publish_plan()'s per-slot row shape — a single source of truth for both
# the row length it builds and the "columns" name list it publishes
# alongside sensor.gridlock_soc_forecast's plan_table attribute, so the
# two can never drift out of sync with each other.
PLAN_TABLE_COLS = ["slot", "import_p", "export_p", "pv_kwh", "load_kwh",
                   "grid_kwh", "charge_kwh", "battery_kwh", "action", "ev_kwh",
                   "dispatch", "saving_session", "power_up_session", "free_electricity",
                   "session_reward_p",
                   "session_baseline_kwh", "session_export_baseline_kwh",
                   "soc_pct", "cost_delta_p", "total_gbp",
                   "import_rank", "export_rank",
                   # GridWarm's planned heat pump electricity in this slot
                   # (space heating + hot water run), already inside
                   # load_kwh -- carried separately so the plan view can
                   # show what the battery is being asked to carry for it.
                   "heatpump_kwh"]

# Every HA integration whose entities this app auto-discovers by name
# that's actually a Kraken-platform fork sharing BottlecapDave's own
# entity suffix conventions (confirmed directly against source: EDF's
# integration is a literal fork of the Octopus one, same suffixes, only
# the domain prefix differs) — used as a tuple so registry.find()'s
# plain str.startswith() already matches whichever one is actually
# installed, with zero changes needed to registry.py itself. A supplier
# switch away from Octopus previously meant GridLock's own rate/dispatch
# discovery silently found nothing at all, not just "less reliable" —
# confirmed as a real gap, not a hypothetical one, before it shipped.
KRAKEN_FORK_DOMAINS = ("octopus_energy", "edf_energy")


def _kraken_prefixes(entity_domain, suffix_after_prefix=""):
    """e.g. _kraken_prefixes("sensor", "electricity_") ->
    ("sensor.octopus_energy_electricity_", "sensor.edf_energy_electricity_")"""
    return tuple(f"{entity_domain}.{d}_{suffix_after_prefix}" for d in KRAKEN_FORK_DOMAINS)

# The thermal model steps at a finer resolution than the battery plan's own
# 30-min slots (core/thermal.py's docstring/tests: a fast zone like a hot
# water tank can reach its target well within a single 30-min slot, and
# simulating at that coarse a resolution overshoots wildly since nothing
# gets a chance to react mid-slot). Simulated points are then downsampled
# to one per battery-plan slot for publishing, keeping payload size
# comparable to the SoC/solar forecasts rather than 576 raw points.
THERMAL_STEP_MIN = 5
THERMAL_HORIZON_STEPS = SLOT_MIN * HORIZON_SLOTS // THERMAL_STEP_MIN

# GridWarm's estimate of the heat pump's electrical draw, published as a
# power sensor so the whole-house load learning subtracts it (like any
# other circuit) and the battery plan's load forecast takes GridWarm's
# OWN plan for it slot by slot instead of a learned average -- the
# battery then charges for, and reserves for, the heating it knows is
# coming. The unit's lifetime kWh counters can't give a per-tick figure,
# so this is thermal output divided by the modelled COP.
HEATPUMP_POWER_ENTITY = "sensor.gridlock_heatpump_power"

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
            prefix=_kraken_prefixes("binary_sensor"), suffix="_intelligent_dispatching"))
        self.ent_import_rate = (a.get("import_rate")
                                or self.overrides.get("import_rate_override")
                                or self.registry.find(
            prefix=_kraken_prefixes("sensor", "electricity_"), suffix="_current_rate", avoid="export"))
        # Which Kraken-fork integration the import rate actually came
        # from — a supplier switch commonly leaves the previous
        # supplier's integration installed with its entities still
        # reporting, so anything else discovered below tries the import
        # rate's own integration first and only falls back to any
        # Kraken fork if that one has no match (e.g. export genuinely
        # staying with a different supplier than import).
        self.supplier_domain = next(
            (d for d in KRAKEN_FORK_DOMAINS if f".{d}_" in (self.ent_import_rate or "")), None)
        self.ent_export_rate = (a.get("export_rate")
                                or self.overrides.get("export_rate_override")
                                or (self.supplier_domain and self.registry.find(
            prefix=f"sensor.{self.supplier_domain}_electricity_",
            suffix="_export_current_rate"))
                                or self.registry.find(
            prefix=_kraken_prefixes("sensor", "electricity_"), suffix="_export_current_rate"))
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

        # Smart-charging dispatch and demand-flexibility sessions are all
        # part of the IMPORT supply, so an auto-discovered one from a
        # different integration than the import rate's is a leftover
        # from before a supplier switch — still planning around (or
        # auto-joining) it would chase rewards that can't be paid out.
        # An explicitly configured one is still trusted as-is.
        for attr, keys in (("ent_dispatch", ("octopus_dispatch",)),
                           ("ent_saving_events", ("octopus_saving_events",)),
                           ("ent_power_up_events", ("power_up_events",))):
            ent = getattr(self, attr)
            explicit = any(a.get(k) or self.overrides.get(f"{k}_override") for k in keys)
            if (self.supplier_domain and ent and not explicit
                    and f".{self.supplier_domain}_" not in ent):
                self.log(f"Ignoring {ent} — import is on {self.supplier_domain}, so "
                         "another integration's import-side programme no longer applies.")
                setattr(self, attr, None)

        # EDF's own "Power Perks" (part of its Flextras loyalty scheme) —
        # a genuinely separate mechanism from Octopus's Power Up: not a
        # baseline-vs-excess reward, just real announced free-electricity
        # windows straight from EDF's own API (confirmed live: real
        # `free_electricity_windows` data, not routed through the same
        # third-party feed Octopus's Free Electricity Sessions depends
        # on). Gated on the registration binary_sensor actually being
        # on — the windows show up in the event entity regardless of
        # whether you're enrolled to benefit from them, so reading them
        # unconditionally would plan around a discount that isn't really
        # yours. Auto-enrolled (see _auto_enroll_edf_flextras) — checked
        # directly against the integration's own button.py: these are
        # plain no-cost loyalty opt-ins (join Flextras, register for
        # Power Perks, claim the one-off joining bonus), same "no
        # downside, don't make the user do it by hand" reasoning as
        # Saving Sessions' own auto-join. Deliberately NOT extended to
        # Sunday Saver — the integration's own maintainer left that
        # join action out on purpose ("that endpoint is not known, and
        # guessing it risks unintended account changes"), so it still
        # needs enrolling by hand in EDF's own app.
        self.ent_edf_power_perks_events = (a.get("edf_power_perks_events")
                                           or self.overrides.get("edf_power_perks_events_override")
                                           or self.registry.find(
            prefix="event.edf_energy_", suffix="_free_electricity_session_events"))
        self.ent_edf_power_perks_registered = (a.get("edf_power_perks_registered_entity")
                                               or self.overrides.get(
            "edf_power_perks_registered_entity_override")
                                               or self.registry.find(
            prefix="binary_sensor.edf_energy_", suffix="_flextras_power_perks_registered"))
        self.ent_edf_flextras_registered = (a.get("edf_flextras_registered_entity")
                                            or self.overrides.get(
            "edf_flextras_registered_entity_override")
                                            or self.registry.find(
            prefix="binary_sensor.edf_energy_", suffix="_flextras_registered"))
        self.ent_edf_flextras_bonus_hours = (a.get("edf_flextras_bonus_hours_entity")
                                             or self.overrides.get(
            "edf_flextras_bonus_hours_entity_override")
                                             or self.registry.find(
            prefix="sensor.edf_energy_", suffix="_flextras_bonus_hours"))
        self.ent_edf_flextras_join_button = self.registry.find(
            prefix="button.edf_energy_", suffix="_flextras_join")
        # Every free session the integration knows about, any source --
        # booked Flextras hours above all (the real data shape, seen
        # populated: events: [{code, source, start, end, ...}]).
        self.ent_edf_free_sessions = (a.get("edf_free_sessions_entity") or self.registry.find(
            prefix="event.edf_energy_", suffix="_free_electricity_session_events"))
        self.ent_edf_power_perks_button = self.registry.find(
            prefix="button.edf_energy_", suffix="_flextras_register_power_perks")
        self.ent_edf_bonus_hours_button = self.registry.find(
            prefix="button.edf_energy_", suffix="_flextras_claim_bonus_hours")

        # EDF's separate "Sunday Saver" perk — checked directly against
        # the integration's own source rather than guessed from its
        # live attributes alone (they were empty/unenrolled when
        # checked, which isn't enough to trust a shape from). Its own
        # `sunday_saver_windows` attribute is a 60-day rolling HISTORY
        # for the EDF panel's own display, not a forward planning list
        # like Power Perks' event entity — the real forward-looking
        # data is this single start/end sensor pair (each sensor's own
        # state IS the next window's timestamp), gated on that same
        # sensor's own "is_enrolled" attribute rather than a separate
        # registration entity.
        self.ent_edf_sunday_saver_start = (a.get("edf_sunday_saver_start_entity")
                                           or self.overrides.get(
            "edf_sunday_saver_start_entity_override")
                                           or self.registry.find(
            prefix="sensor.edf_energy_", suffix="_sunday_saver_start"))
        self.ent_edf_sunday_saver_end = (a.get("edf_sunday_saver_end_entity")
                                         or self.overrides.get(
            "edf_sunday_saver_end_entity_override")
                                         or self.registry.find(
            prefix="sensor.edf_energy_", suffix="_sunday_saver_end"))

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
        # EV Charge Assist: every Tesla Fleet vehicle discovered, keyed by
        # its device slug — see core/ev_charge_assist.py's own docstring
        # for why this exists (the supplier's own smart charging is
        # linked to the shared charger, not either specific car, and
        # can't read a Tesla's real battery % over its own protocol).
        # last_commanded per vehicle avoids re-sending the same switch
        # command every 5-minute tick, matching the DHW control pattern
        # (_apply_thermal_control) elsewhere in this file.
        self.ev_vehicle_stems = self.registry.find_tesla_vehicle_stems()
        self.ev_vehicle_last_commanded = {stem: None for stem in self.ev_vehicle_stems}
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
        # Octopus's own published rate (800 points = £1) -- not
        # guaranteed to stay fixed, and redeeming via Shoptopus instead
        # of account credit is worth more, so configurable rather than
        # hardcoded. Used to decide whether a Saving/Power Down
        # session's reward is actually worth force-exporting the
        # battery for (see _tick_inner) -- joining a session is always
        # free (no downside, so every one gets joined unconditionally),
        # but forcing an export is real battery throughput, priced the
        # same as any other export-side cycling.
        self.octopoint_value_gbp = float(a.get("octopoint_value_gbp", 1 / 800))
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

        self.tariff_provider = KrakenTariffProvider(
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
        self._thermal_forecast_warned = {}   # weather entity -> when it last failed
        self._thermal_weather_ok = None      # the weather entity that last served a forecast
        self._thermal_control_state = {}
        # The dashboard-configured space heating zone (gridwarm_settings.json,
        # see core/space_heating.py) -- built lazily on the first tick and
        # rebuilt whenever the file changes, so a dashboard edit takes
        # effect on the next tick with no restart. (mtime, zone, settings).
        self._ui_zone_cache = (None, None, None)
        # control_entity -> zone name, for the "something else flipped my
        # switch" listener (the unit's own timer, a person in the UI).
        self._control_listeners = {}
        # The last solved battery plan (slots + cost trace), for pricing
        # heating electricity the way the battery actually makes it cost
        # (core/effective_price.py). None until the first solve.
        self._last_plan = None
        # heat_loss_degrees learning -- refines the config's own starting
        # figure against real cooling periods (heating off) over time,
        # same EMA-blend pattern as the load forecast's learned profile.
        # Restored from disk and re-applied to each zone's params
        # immediately, so a restart doesn't silently reset progress back
        # to the config default until the next learning tick happens to
        # fire.
        self.thermal_learning_state = self._load_json("thermal_learning_state.json", {})
        for zone in self.thermal_zones:
            self._apply_learned_thermal(zone)
        for zone in self.thermal_zones:
            if not zone["control_entity"]:
                continue
            self._watch_control_entity(zone)
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
        self._prev_session_not_worth_it = False

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
        # result" pattern as SSEN Power Track above. Falls back to your
        # own account's real tariff code (see _detect_dno_region) when
        # not set explicitly — Agile rates are region-specific (14 DNO
        # regions, letters A-P) and this add-on is shared across installs,
        # so an unconfigured, undetectable region still stays off rather
        # than silently defaulting to one, which would be actively wrong
        # for anyone outside it, not a harmless guess.
        self.agile_region = a.get("agile_region") or self._detect_dno_region()
        self.agile_rates = {}
        self.agile_standing_gbp = 0.0

        # EDF GoElectric — same DNO region letter as Agile above (it's
        # geographic, not supplier-specific), fetched live from EDF's own
        # public Kraken API rather than typed into compare_tariffs as a
        # static guess (see poll_edf_goelec_rates). Keyed by term length
        # (e.g. "18m") since more than one can be live with genuinely
        # different real rates at once.
        self.edf_goelec_products = {}
        # A real switch to EDF moves export to EDF's own rate too, not
        # Octopus Agile Outgoing's — unlike the Agile import-only
        # comparison above (same Octopus account, export untouched),
        # EDF is a different supplier entirely, so keeping s["exp"] (your
        # current live export) here would silently assume you keep
        # today's export deal after switching suppliers, which you can't.
        # No public EDF export-rate product found via the Kraken API
        # (checked) — real confirmed figure, override in apps.yaml if
        # yours differs.
        self.edf_export_rate = float(a.get("edf_export_rate", 0.13))

        # EV Agile smart-charging — off unless ev_daily_charge_hours ends
        # up set, from either source below. Only relevant once actually
        # ON Agile import: Octopus's own Intelligent dispatch (IOG)
        # already schedules EV charging for you; Agile has no
        # equivalent, so without this the car would just charge whenever
        # plugged in at whatever the live rate happens to be. GridLock
        # finds the cheapest contiguous window of this length in the
        # real published Agile rates itself and writes it straight onto
        # the Hypervolt's own schedule (session 1 — confirmed unused)
        # rather than depending on Octopus's "Target Rate" sensors,
        # which the integration removed entirely in late 2025 (the
        # mechanism Hypervolt's own "Set Schedule" service was built
        # around, now broken for current installs).
        #
        # apps.yaml is only the fallback default here — the dashboard's
        # own EV Charging card (ev_schedule_settings.json, no apps.yaml
        # edit or add-on restart needed) wins if present, same
        # config-vs-dashboard precedence as component warranties
        # (_all_warranties/_ev_schedule_settings re-read this fresh on
        # every poll, not just once at startup).
        self.ev_charge_hours_cfg = float(a.get("ev_daily_charge_hours", 0) or 0)
        self.ev_high_price_notify_p_cfg = float(a.get("ev_high_price_notify_p", 15.0))
        self.ent_ev_schedule_start = a.get(
            "ev_schedule_start_entity", "time.hypervolt_schedule_session_1_start_time")
        self.ent_ev_schedule_end = a.get(
            "ev_schedule_end_entity", "time.hypervolt_schedule_session_1_end_time")
        self.ent_ev_schedule_days = a.get(
            "ev_schedule_days_entity", "text.hypervolt_schedule_session_1_days_of_week")
        self.ent_ev_schedule_apply = a.get(
            "ev_schedule_apply_entity", "button.hypervolt_apply_schedule")
        self._ev_scheduled_window = None

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
        # Real per-half-hour-of-day import/export kWh, today only —
        # rolled into savings_history as a daily tariff backtest (see
        # _compute_tariff_backtest) once the day ends. Kept separate from
        # the aggregate offpeak/onpeak split above since a backtest needs
        # to re-classify each slot against OTHER tariffs' own off-peak
        # windows too, not just this account's real one.
        self._backtest_import_kwh = [0.0] * 48
        self._backtest_export_kwh = [0.0] * 48
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
            self.run_every(self.poll_edf_goelec_rates, "now", 3600)

        if self.ent_edf_flextras_registered:
            self.run_every(self._auto_enroll_edf_flextras, "now", 3600)

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

    def _dashboard_path(self, filename):
        """Files the web UI writes (warranty_entries.json,
        ev_schedule_settings.json, gridwarm_settings.json) are NOT
        per-site state and must not go through _state_path's site
        namespacing -- the web UI knows nothing about sites and writes
        the plain filename next to this file. Reading them through
        _state_path looked for "<site>__<file>" and silently never found
        a dashboard edit at all (fixed in 3.45.0)."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

    def _load_dashboard_json(self, filename, default):
        try:
            with open(self._dashboard_path(filename)) as f:
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

    def _detect_dno_region(self):
        """DNO region letter (A-P) straight from your own account's real
        tariff code, e.g. "E-1R-IOG-SMB-FIX-12M-25-08-29-H" -> "H" — both
        Octopus's and EDF's integrations carry this on the import rate
        sensor (as "tariff" on *_current_rate, "tariff_code" elsewhere;
        EDF's is a literal fork, same convention), derived by your
        supplier from your actual address either way. More authoritative
        than asking you to go find and type in the same letter apps.yaml's
        own comments used to walk you through by hand, and correct even
        if you move house without touching config, or switch supplier."""
        if not self.ent_import_rate:
            return None
        for attr in ("tariff", "tariff_code"):
            code = self.get_state(self.ent_import_rate, attribute=attr)
            m = re.search(r"-([A-P])$", str(code or ""))
            if m:
                return m.group(1)
        return None

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

        # Real 30-day tariff backtest — each tariff's own rate structure
        # applied to what you actually used/exported each day (see
        # _compute_tariff_backtest), not a forward-looking plan. A
        # tariff missing from an older day's record (e.g. added to
        # compare_tariffs after that day rolled, or EDF's poll failing
        # that day) just doesn't contribute to that tariff's total for
        # that day rather than being back-filled or guessed.
        backtest_days = sorted(
            ((d, v["tariff_backtest"]) for d, v in self.savings_history.items()
             if "tariff_backtest" in v),
            key=lambda p: p[0])[-30:]
        backtest_totals = {}
        backtest_day_counts = {}
        for _, tb in backtest_days:
            for name, cost in tb.items():
                backtest_totals[name] = backtest_totals.get(name, 0.0) + cost
                backtest_day_counts[name] = backtest_day_counts.get(name, 0) + 1
        backtest_totals = {k: round(v, 2) for k, v in backtest_totals.items()}
        backtest_history = [{"date": d, **tb} for d, tb in backtest_days]

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
                                   "bill_month_to_date": bill_month_to_date,
                                   "tariff_backtest_totals": backtest_totals,
                                   "tariff_backtest_days": backtest_day_counts,
                                   "tariff_backtest_history": backtest_history})

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
        self._backtest_import_kwh = state.get("backtest_import_kwh") or [0.0] * 48
        self._backtest_export_kwh = state.get("backtest_export_kwh") or [0.0] * 48

    def _save_cost_tracking_state(self):
        self._save_json("cost_tracking_state.json", {
            "day": self.cost_tracking_day,
            "import_cost": self.tracked_import_cost_today,
            "export_value": self.tracked_export_value_today,
            "backtest_import_kwh": self._backtest_import_kwh,
            "backtest_export_kwh": self._backtest_export_kwh,
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
            "onpeak_import_cost": round(self.tracked_onpeak_cost_today, 4),
            "tariff_backtest": self._compute_tariff_backtest()})
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
        self._backtest_import_kwh = [0.0] * 48
        self._backtest_export_kwh = [0.0] * 48

    def _compute_tariff_backtest(self):
        """What the day that just ended would REALLY have cost under each
        configured tariff, applying that tariff's own real rate
        structure to what you actually used and exported — not a
        forward-looking re-optimized plan. Exists because doing this by
        hand in chat (cross-referencing several separately-fetched real
        data sources over many turns) produced two different answers for
        the same real numbers before the mistake was caught — this is
        the tested, single-source-of-truth version of that calculation,
        computed automatically every day rather than on demand and by
        hand. Best-effort: any one tariff's price data being unavailable
        (e.g. EDF's live poll having failed that day) just skips that
        tariff for the day rather than losing the whole day's record."""
        results = {}
        live_standing = self.get_float_state(self.ent_daily_standing_charge, 0.0)
        results["Current (real)"] = round(
            self.tracked_import_cost_today - self.tracked_export_value_today + live_standing, 4)
        for t in self.compare_tariffs:
            name = t.get("name", "tariff")
            try:
                results[name] = core_tariff_backtest.backtest_day_cost(
                    self._backtest_import_kwh, self._backtest_export_kwh,
                    import_default=t.get("import_default", self.default_import),
                    import_windows=t.get("import", []),
                    export_rate=float(t.get("export", 0.0)),
                    standing_gbp=float(t.get("standing") or 0.0))
            except (KeyError, ValueError, TypeError) as exc:
                self.log(f"Tariff backtest skipped {name!r} for today: {exc!r}", level="DEBUG")
        for term, prod in self.edf_goelec_products.items():
            pattern = core_tariff_backtest.edf_windows_to_daily_pattern(prod.get("windows"))
            if not pattern:
                continue
            results[f"EDF GoElectric ({term})"] = core_tariff_backtest.backtest_day_cost(
                self._backtest_import_kwh, self._backtest_export_kwh,
                import_default=pattern["default"], import_windows=pattern["windows"],
                export_rate=self.edf_export_rate, standing_gbp=prod.get("standing_gbp", 0.0))
        return results

    def _update_energy_cost_tracking(self, now):
        self._roll_cost_day(now)
        grid_kw = self._read_live_kw(self.ent_grid_power)
        if grid_kw is None:
            return
        kwh = grid_kw * (5 / 60)
        slot_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
        if self.ent_exporting and self.get_state(self.ent_exporting) == "on":
            exp_rate = self.get_float_state(self.ent_export_rate, self.default_export)
            self.tracked_export_value_today += kwh * exp_rate
            self._backtest_export_kwh[slot_idx] += kwh
        elif self.ent_importing and self.get_state(self.ent_importing) == "on":
            imp_rate = self.get_float_state(self.ent_import_rate, self.default_import)
            self.tracked_import_cost_today += kwh * imp_rate
            self.tracked_import_kwh_today += kwh
            self._backtest_import_kwh[slot_idx] += kwh
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
                             for w in self._load_dashboard_json("warranty_entries.json", [])
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

    def _auto_enroll_edf_flextras(self, kwargs):
        """Auto-enroll in EDF's own free-electricity loyalty perks, same
        "no cost, no downside, don't make the user do it by hand"
        reasoning as Saving Sessions' own auto-join above — confirmed
        directly against the integration's button.py that these three
        are plain opt-ins (join Flextras, register for Power Perks,
        claim the one-off joining bonus), not a financial commitment of
        any kind. Order matters: Power Perks registration only becomes
        possible once actually a Flextras member, so this checks and
        presses in dependency order rather than trying all three
        blindly. Idempotent by construction — each press is gated on the
        real underlying state actually still needing it, so a restart or
        a repeat poll never re-presses something already done, and a
        transient failure just retries next poll rather than being
        silently abandoned. Sunday Saver is deliberately not included
        here — see the discovery comment on ent_edf_power_perks_events
        for why the integration itself doesn't support joining it."""
        if self.get_state("input_boolean.gridlock_enable") == "off":
            return
        if not self.ent_edf_flextras_registered:
            return  # EDF integration not present at all -- nothing to do
        try:
            if (self.get_state(self.ent_edf_flextras_registered) != "on"
                    and self.ent_edf_flextras_join_button):
                self.call_service("button/press",
                                  target={"entity_id": self.ent_edf_flextras_join_button})
                self._log_decision("EDF Flextras joined",
                                  "Auto-enrolled — no cost, unlocks Power Perks free-electricity slots")
                return  # let the next poll see the new registered state before going further
            if (self.get_state(self.ent_edf_flextras_registered) == "on"
                    and self.ent_edf_power_perks_registered
                    and self.get_state(self.ent_edf_power_perks_registered) != "on"
                    and self.ent_edf_power_perks_button):
                self.call_service("button/press",
                                  target={"entity_id": self.ent_edf_power_perks_button})
                self._log_decision("EDF Power Perks registered",
                                  "Auto-enrolled — GridLock will now plan around announced free-electricity windows")
            claimed = self.get_state(self.ent_edf_flextras_bonus_hours, attribute="claimed") \
                if self.ent_edf_flextras_bonus_hours else None
            if (self.get_state(self.ent_edf_flextras_registered) == "on"
                    and not claimed and self.ent_edf_bonus_hours_button):
                self.call_service("button/press",
                                  target={"entity_id": self.ent_edf_bonus_hours_button})
                self._log_decision("EDF Flextras bonus hours claimed", "Auto-claimed the one-off joining bonus")
        except Exception as exc:  # noqa: BLE001 — best-effort, retried next poll
            self.log(f"EDF Flextras auto-enrollment failed: {exc!r}", level="WARNING")

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

    def _find_active_kraken_products(self, *, api_base, products_query, name_match, log_label):
        """Any Kraken-platform brand's public product list, filtered to
        whatever's genuinely live right now — Octopus and EDF both run on
        the same underlying Kraken billing platform (confirmed directly:
        api.edfgb-kraken.energy mirrors api.octopus.energy's exact
        schema), no auth needed for either. More than one product can
        match and be active at once during a rollover — confirmed live:
        EDF ran "Go Electric 12m v2" alongside "Go Electric 18m" during
        one transition, then replaced both with "12m v3" and "18m v2" at
        the exact same launch timestamp shortly after, with genuinely
        different real day rates between the two terms (not a rounding
        difference — 38.19p vs 36.19p). There's no reliable field marking
        one as "the" headline product, so every currently-active match is
        returned rather than guessing at one — callers decide how to use
        them (Agile has never actually had two live at once, so takes the
        most-recently-launched; EDF GoElectric shows one row per term
        rather than silently picking a term-length for you)."""
        try:
            req = urllib.request.Request(
                f"{api_base}/products/?{products_query}",
                headers={"User-Agent": "GridLock/3"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                products = json.load(resp).get("results", [])
        except Exception as exc:  # noqa: BLE001 — network is best-effort
            self.log(f"{log_label} product list fetch failed: {exc!r}", level="WARNING")
            return []
        now = self.get_now()
        return [p for p in products
                if name_match in (p.get("full_name") or "")
                and self._iso(p["available_from"]) <= now
                and (not p.get("available_to") or self._iso(p["available_to"]) > now)]

    def _fetch_kraken_tariff_rates(self, *, api_base, code, region, log_label):
        """Real rate/standing-charge records for one specific product code
        + region — both looked up live every poll rather than assumed,
        same "don't guess at something checkable" discipline as
        everywhere else real money is on the line in this codebase.
        Returns (rate_records, standing_records) raw from the API, or
        (None, None) on failure — callers do their own post-processing
        since Octopus Agile (half-hourly bands) and EDF GoElectric (wide
        day/off-peak bands) suit different shapes downstream."""
        try:
            now = self.get_now()
            tariff_code = f"E-1R-{code}-{region}"
            period_from = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
            period_to = (now + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
            base = f"{api_base}/products/{code}/electricity-tariffs/{tariff_code}"
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
            return rates, standing
        except Exception as exc:  # noqa: BLE001 — network is best-effort
            self.log(f"{log_label} rate poll failed: {exc!r}", level="WARNING")
            return None, None

    def poll_agile_rates(self, kwargs):
        candidates = self._find_active_kraken_products(
            api_base="https://api.octopus.energy/v1",
            products_query="is_variable=true&brand=OCTOPUS_ENERGY",
            name_match="Agile Octopus", log_label="Agile")
        if not candidates:
            self.log("Agile poll: no currently-active Agile product found", level="WARNING")
            return
        product = max(candidates, key=lambda p: self._iso(p["available_from"]))
        rates, standing = self._fetch_kraken_tariff_rates(
            api_base="https://api.octopus.energy/v1", code=product["code"],
            region=self.agile_region, log_label="Agile")
        if rates is None:
            return
        self.agile_rates = {
            self._agile_slot_key(self._iso(r["valid_from"])): r["value_inc_vat"] / 100.0
            for r in rates if r.get("valid_from") and r.get("value_inc_vat") is not None
        }
        if standing and standing[0].get("value_inc_vat") is not None:
            self.agile_standing_gbp = standing[0]["value_inc_vat"] / 100.0
        self._schedule_ev_agile_charge()

    def _is_on_agile_import(self):
        """Whether the account is genuinely ON Agile import right now, not
        just that agile_region is set (that also drives the "what-if"
        comparison row for accounts on a different real tariff)."""
        if not self.ent_import_rate:
            return False
        for attr in ("tariff", "tariff_code"):
            code = self.get_state(self.ent_import_rate, attribute=attr)
            if code and "AGILE" in str(code).upper():
                return True
        return False

    def _ev_schedule_settings(self):
        """apps.yaml's ev_daily_charge_hours/ev_high_price_notify_p are
        only the fallback default — a value saved from the dashboard's
        own EV Charging card (ev_schedule_settings.json, written by the
        web UI, no apps.yaml edit or add-on restart needed) wins if
        present. Re-read fresh every call, same pattern as
        _all_warranties(), so a dashboard change shows up on the next
        poll rather than only after a restart."""
        dash = self._load_dashboard_json("ev_schedule_settings.json", {})
        try:
            hours = float(dash.get("daily_charge_hours", self.ev_charge_hours_cfg) or 0)
        except (TypeError, ValueError):
            hours = self.ev_charge_hours_cfg
        try:
            notify_p = float(dash.get("high_price_notify_p", self.ev_high_price_notify_p_cfg))
        except (TypeError, ValueError):
            notify_p = self.ev_high_price_notify_p_cfg
        return hours, notify_p

    def _schedule_ev_agile_charge(self):
        """Find the cheapest contiguous ev_charge_hours window in the real
        published Agile rates and write it onto the Hypervolt's own daily
        schedule (see ev_charge_hours_cfg's own comment for why this
        exists and why it doesn't use Hypervolt's "Set Schedule"
        service). Always schedules the cheapest window it can find, even
        on a genuinely expensive day (e.g. a low-wind stretch where even
        the best 6h averages 30p) — the car still needs its charge every
        day regardless, so this never silently skips a day. A price
        notify threshold flags those days instead, so you can step in
        manually if you'd genuinely rather skip one."""
        ev_charge_hours, ev_high_price_notify_p = self._ev_schedule_settings()
        if not (ev_charge_hours and self._is_on_agile_import()):
            return
        rates_by_start = {self._iso(k): v for k, v in self.agile_rates.items()}
        result = find_cheapest_window(rates_by_start, ev_charge_hours, SLOT_MIN)
        if result is None:
            self.log("EV Agile scheduling: no contiguous window found in published rates",
                     level="WARNING")
            return
        best_start, best_end, best_avg = result
        if (best_start, best_end) == self._ev_scheduled_window:
            return  # already set to this exact window -- don't hammer the charger's API
        local_start, local_end = best_start.astimezone(), best_end.astimezone()
        try:
            self.call_service("time/set_value", target={"entity_id": self.ent_ev_schedule_start},
                               time=local_start.strftime("%H:%M:%S"))
            self.call_service("time/set_value", target={"entity_id": self.ent_ev_schedule_end},
                               time=local_end.strftime("%H:%M:%S"))
            self.call_service("text/set_value", target={"entity_id": self.ent_ev_schedule_days},
                               value="")  # blank = every day, matching this schedule's own default
            self.call_service("button/press", target={"entity_id": self.ent_ev_schedule_apply})
        except Exception as exc:  # noqa: BLE001 — best-effort, retried next poll
            self.log(f"EV Agile scheduling: failed to apply schedule: {exc!r}", level="WARNING")
            return
        self._ev_scheduled_window = (best_start, best_end)
        avg_p = best_avg * 100
        self._log_decision(
            "EV Agile Charge Scheduled",
            f"Cheapest {ev_charge_hours:.0f}h window found: "
            f"{local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')} at {avg_p:.1f}p/kWh avg")
        if avg_p > ev_high_price_notify_p:
            self._notify(
                "GridLock: expensive EV charge window today",
                f"No genuinely cheap Agile window today — the cheapest "
                f"{ev_charge_hours:.0f}h block available averages {avg_p:.1f}p/kWh "
                f"({local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')}). "
                "Charging anyway so the car doesn't go short — step in manually on the "
                "Hypervolt app if you'd rather skip today.")

    def poll_edf_goelec_rates(self, kwargs):
        """EDF GoElectric — same Kraken platform as Agile above, but a
        fixed-term product (a day/off-peak split over wide bands, not
        half-hourly), so kept as a proper (start, end, rate) window list
        per term.py for core_slots.rate_at() rather than Agile's per-
        half-hour-slot dict — that also means it gets the same "repeat
        the prior day once real data runs out" fallback rate_at() already
        has, for free. Keyed by term length (see
        _find_active_kraken_products) since more than one term can be
        live with genuinely different real rates at once."""
        candidates = self._find_active_kraken_products(
            api_base="https://api.edfgb-kraken.energy/v1",
            products_query="brand=EDF", name_match="Go Electric", log_label="EDF GoElectric")
        if not candidates:
            self.log("EDF GoElectric poll: no currently-active product found", level="WARNING")
            return
        products = {}
        for p in candidates:
            rates, standing = self._fetch_kraken_tariff_rates(
                api_base="https://api.edfgb-kraken.energy/v1", code=p["code"],
                region=self.agile_region, log_label="EDF GoElectric")
            if rates is None:
                continue
            windows = [(self._iso(r["valid_from"]), self._iso(r["valid_to"]), r["value_inc_vat"] / 100.0)
                       for r in rates if r.get("valid_from") and r.get("valid_to")
                       and r.get("value_inc_vat") is not None]
            standing_gbp = (standing[0]["value_inc_vat"] / 100.0
                             if standing and standing[0].get("value_inc_vat") is not None else 0.0)
            term = p.get("term")
            label = f"{term}m" if term else p["code"]
            launched = self._iso(p["available_from"])
            if label not in products or launched > products[label]["launched"]:
                products[label] = {"windows": windows, "standing_gbp": standing_gbp, "launched": launched}
        self.edf_goelec_products = products

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

    def _ev_is_charging(self):
        """Whether the EV is genuinely drawing charge current right now.
        Prefers the EV's own power reading — real current flowing is the
        actually-reliable signal — over ent_ev's discovered switch/state
        entity. Real bug, confirmed live: a Hypervolt charger's "is
        charging" entity is a switch that reflects whether charging is
        *enabled* (schedule armed), not whether the car is actually
        pulling power, and other chargers expose status as a string
        sensor ("Charging"/"Paused"/"Complete") that never equals the
        literal "on" this used to require — either way EV Protection
        (pausing battery discharge while the EV shares the circuit)
        silently never engaged despite the car genuinely charging.
        Falls back to the old switch-state check only when no power
        entity is configured at all, so this never goes from "wrong" to
        "no signal" instead of "correct"."""
        if self.ent_ev_power:
            kw = self.get_float_state(self.ent_ev_power, 0.0) or 0.0
            return kw > 0.05
        return bool(self.ent_ev) and self.get_state(self.ent_ev) == "on"

    def _manage_ev_charge_assist(self):
        """For each Tesla Fleet vehicle discovered (self.ev_vehicle_stems),
        take over active charge control if it's the one currently plugged
        into the shared charger — see core/ev_charge_assist.py's own
        docstring for the full reasoning. "Plugged in" is read from each
        vehicle's OWN charging-state sensor (any state other than
        "disconnected" means a cable is connected — Tesla's real states
        include "Charging"/"Stopped"/"Complete"/"NoPower" etc, none of
        which are "disconnected"), not the shared charger's own state,
        since the charger has no way to tell the two vehicles apart at
        all. last_commanded per vehicle (reset to None once unplugged)
        avoids re-sending an unchanged switch command every tick, same
        pattern as the DHW control's own state["last_commanded"]."""
        if not self.ev_vehicle_stems:
            return
        in_cheap_window = self.get_float_state(
            self.ent_import_rate, self.default_import) <= self.cheap_rate
        if self.ent_dispatch and self.get_state(self.ent_dispatch) == "on":
            in_cheap_window = True
        vehicles = {}
        for stem in self.ev_vehicle_stems:
            charging_state = self.get_state(f"sensor.{stem}_charging")
            plugged_in = bool(charging_state) and charging_state not in (
                "disconnected", "unavailable", "unknown")
            battery_pct = self.get_float_state(f"sensor.{stem}_battery_level", None) \
                if plugged_in else None
            charge_limit_pct = self.get_float_state(f"number.{stem}_charge_limit", None) \
                if plugged_in else None
            command = core_ev_charge_assist.should_charge(
                battery_pct, charge_limit_pct, in_cheap_window) if plugged_in else False
            # _json_safe(), not the raw None — a None reaching HA's state
            # attributes silently breaks serialisation (confirmed live:
            # the whole nested vehicle dict came back empty, not just
            # this one field), the same failure mode _json_safe was
            # already built to guard plan_table cells against.
            vehicles[stem] = {"plugged_in": plugged_in,
                               "battery_pct": self._json_safe(battery_pct),
                               "charge_limit_pct": self._json_safe(charge_limit_pct),
                               "charging": command}
            if not plugged_in:
                self.ev_vehicle_last_commanded[stem] = None
                continue
            if command == self.ev_vehicle_last_commanded.get(stem):
                continue
            self.ev_vehicle_last_commanded[stem] = command
            # target={"entity_id": ...}, not the plain entity_id= kwarg —
            # matches every other place this codebase commands hardware.
            self.call_service(f"switch/turn_{'on' if command else 'off'}",
                               target={"entity_id": f"switch.{stem}_charge"})
            self._log_decision(
                f"EV Charge Assist: {stem} {'charging' if command else 'idle'}",
                f"{stem} at {battery_pct}% (target {charge_limit_pct}%), "
                f"{'a cheap/dispatch window is active' if in_cheap_window else 'no cheap/dispatch window right now'}")
        plugged = [s for s, v in vehicles.items() if v["plugged_in"]]
        overview = ", ".join(f"{s}: {'charging' if vehicles[s]['charging'] else 'waiting'}"
                              for s in plugged) if plugged else "no vehicle connected"
        self.set_state("sensor.gridlock_ev_charge_assist", state=overview,
                       attributes={"friendly_name": "GridLock EV Charge Assist",
                                   "icon": "mdi:ev-station",
                                   "in_cheap_window": in_cheap_window,
                                   "vehicles": vehicles})

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

    def _edf_free_electricity_windows(self):
        """Every genuinely-free EDF import window currently known about —
        Power Perks (a real multi-window forward list) plus Sunday Saver
        (a single next-window pair) — each only trusted once its own
        real enrollment signal confirms you actually benefit from it,
        not just that the data happens to be visible. Deliberately not
        wired up: EDF's football/event-tied free electricity and
        Flextras Bonus Hours — their real data shape wasn't available to
        verify (both were unenrolled/inactive when checked, the same
        reason Sunday Saver's own history attribute was double-checked
        against source rather than assumed from an empty example) —
        adding those without seeing a real populated example would be
        guessing at what they actually mean, not reading real data."""
        windows = []
        if self.ent_edf_power_perks_events and self.ent_edf_power_perks_registered \
                and self.get_state(self.ent_edf_power_perks_registered) == "on":
            for period in self._attr_list(self.ent_edf_power_perks_events, "free_electricity_windows"):
                try:
                    windows.append((self._iso(period["start"]), self._iso(period["end"])))
                except (KeyError, ValueError, TypeError):
                    continue
        if self.ent_edf_free_sessions:
            # Booked Flextras hours (and anything else the integration
            # lists as a free session). Flextras hours are only ever
            # listed once booked, so no enrollment gate is needed.
            for ev in self._attr_list(self.ent_edf_free_sessions, "events"):
                try:
                    windows.append((self._iso(ev["start"]), self._iso(ev["end"])))
                except (KeyError, ValueError, TypeError):
                    continue
        if self.ent_edf_sunday_saver_start and self.ent_edf_sunday_saver_end:
            enrolled = self.get_state(self.ent_edf_sunday_saver_start, attribute="is_enrolled")
            start = self.get_state(self.ent_edf_sunday_saver_start)
            end = self.get_state(self.ent_edf_sunday_saver_end)
            if enrolled and start not in (None, "unknown", "unavailable") \
                    and end not in (None, "unknown", "unavailable"):
                try:
                    windows.append((self._iso(start), self._iso(end)))
                except (ValueError, TypeError):
                    pass
        # The same window can arrive from more than one entity.
        return sorted(set(windows))

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
            free_electricity_windows=self._edf_free_electricity_windows(),
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
            # A genuinely free import window (see build_slots()'s
            # free_electricity_windows) shares the same column: without
            # its own marker the only trace of it in the plan is a 0.0p
            # import rate, easy to read past.
            in_free_electricity = bool(s.get("free_electricity"))
            power_up_cell = ("<span style='color:#4ade80'>🆓</span>" if in_free_electricity
                             else "<span style='color:#4ade80'>⚡🆓</span>" if in_power_up_session
                             else "—")
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
                1 if in_free_electricity else 0,
                round(session_reward_p, 2),
                round(session_baseline_kwh, 3),
                round(session_export_baseline_kwh, 3),
                trace[i], round(delta_p, 2), cost_trace[i]["total"],
                imp_rank[i], exp_rank[i],
                round(self._heatpump_slot_kwh(s["start"]), 3)]
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
                "<th>EV kWh</th><th>Saving session</th><th>Free</th><th>SoC</th><th>Grid £</th><th>Total £</th></tr>"
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
                                                  "total_slots": len(slots)},
                                   # What the plan itself is treating as
                                   # free import, and how far ahead it
                                   # looks — lets the dashboard say per
                                   # announced window whether it's being
                                   # planned around yet, rather than
                                   # leaving that to be inferred.
                                   "free_electricity_slots": [
                                       s["start"].isoformat() for s in slots
                                       if s.get("free_electricity")],
                                   "plan_horizon_end": (slots[-1]["end"].isoformat()
                                                        if slots else None)})
        return html

    def publish_compare(self, slots, soc0, live_cost, now):
        # is_live distinguishes "we actually queried this tariff's real
        # rates" (Current, Agile) from "a fixed rate + time windows typed
        # into apps.yaml as an approximation of this tariff's published
        # structure" (every static compare_tariffs entry) -- shown on the
        # dashboard so a close-but-not-identical number between "Current"
        # and a static row for the SAME product you're actually on reads
        # as "estimate vs your real dispatch", not as a discrepancy/bug.
        #
        # The horizon is 48h (HORIZON_SLOTS), not 24h — a configured
        # `standing` figure (a £/day rate, same convention as apps.yaml's
        # own comments) is scaled to it so a 2-day standing charge isn't
        # compared against a 1-day one.
        #
        # "Current" deliberately does NOT add its own live standing charge
        # here even though a real sensor for it exists (self.
        # ent_daily_standing_charge) — `standing` on every compare_tariffs
        # entry defaults to 0.0 unless the user's actually typed in that
        # tariff's real published figure, so adding a real number only to
        # Current would bias every unconfigured row (almost certainly all
        # of them) in its favour by that exact amount, for no reason tied
        # to the tariffs themselves. Comparison stays unit-rate-only unless
        # standing is explicitly configured on the entries you care about.
        horizon_hours = len(slots) * SLOT_MIN / 60.0
        live_imp = self.get_float_state(self.ent_import_rate, self.default_import)
        live_exp = self.get_float_state(self.ent_export_rate, self.default_export)
        live_standing = self.get_float_state(self.ent_daily_standing_charge, None)
        rows = [("Current (live rates)", live_cost, True,
                  {"import_desc": f"{round(live_imp * 100, 1)}p right now",
                   "export_p": round(live_exp * 100, 1),
                   "standing_p": round(live_standing * 100, 1) if live_standing is not None else None},
                  horizon_hours)]
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
            standing = t.get("standing")
            c = result.grid_cost + float(standing or 0.0) * horizon_hours / 24.0
            default_p = round(float(t.get("import_default", self.default_import)) * 100, 1)
            windows_desc = "; ".join(
                f"{round(float(w['rate']) * 100, 1)}p off-peak {w['start']}–{w['end']}"
                for w in t.get("import", []))
            import_desc = f"{windows_desc} / {default_p}p otherwise" if windows_desc else f"{default_p}p"
            rows.append((t.get("name", "tariff"), c, False,
                         {"import_desc": import_desc,
                          "export_p": round(float(t.get("export", 0.0)) * 100, 1),
                          "standing_p": round(standing * 100, 1) if standing is not None else None},
                         horizon_hours))

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
                # EV energy is priced separately below rather than through
                # the LP's own ev_kwh handling (zeroed here) — confirmed
                # live against real data: unlike every other row (a fixed
                # daily off-peak clock window an EV charge can always sit
                # inside), Agile has no such window, so pricing the SAME
                # real dispatched EV kWh at whatever Agile rate happens to
                # apply at IOG's own real dispatch time (this constraint's
                # only other option) isn't a real Agile-native charging
                # strategy — averaging Agile's rate implicitly like that
                # is exactly the mistake the whole-30-days-vs-7.7p analysis
                # caught: some nights beat IOG, most don't, and only a
                # genuine per-day cheapest-window search reflects that.
                ev_total_kwh = sum(s.get("ev_kwh", 0.0) for s in agile_slots)
                cp = [dict(s, charge=0.0, export=0.0, imp=agile_imp[i], exp=s["exp"], ev_kwh=0.0)
                      for i, s in enumerate(agile_slots)]
                result = core_optimizer.solve(cp, soc0, self.cfg, today_date=now.date())
                if result.infeasible:
                    self.log("Agile tariff comparison reported infeasible — "
                             "skipping it this tick.", level="WARNING")
                else:
                    hours = covered * SLOT_MIN / 60.0
                    standing = self.agile_standing_gbp * hours / 24.0
                    lo, hi = min(agile_imp) * 100, max(agile_imp) * 100
                    ev_charge_hours, _ = self._ev_schedule_settings()
                    ev_cost, ev_desc = 0.0, ""
                    if ev_total_kwh > core_optimizer.EPS and ev_charge_hours:
                        rates_by_start = {self._iso(k): v for k, v in self.agile_rates.items()}
                        window = find_cheapest_window(rates_by_start, ev_charge_hours, SLOT_MIN)
                        if window:
                            _, _, ev_avg_rate = window
                            ev_cost = ev_avg_rate * ev_total_kwh
                            ev_desc = f" · EV {ev_total_kwh:.1f}kWh @ {round(ev_avg_rate * 100, 1)}p (best window)"
                    rows.append((f"Octopus Agile (import, next {hours:.0f}h)",
                                 result.grid_cost + standing + ev_cost, True,
                                 {"import_desc": f"{round(lo, 1)}p–{round(hi, 1)}p live half-hourly{ev_desc}",
                                  "export_p": round(agile_slots[0]["exp"] * 100, 1),
                                  "standing_p": round(self.agile_standing_gbp * 100, 1)},
                                 hours))
            else:
                self.log("Agile comparison skipped — no published rate data "
                         "yet for the upcoming slots.", level="DEBUG")

        # EDF GoElectric, live via the same Kraken API Agile above uses
        # (see poll_edf_goelec_rates) — a proper window list rather than
        # a per-slot dict, so rate_at()'s prior-day fallback covers the
        # full 48h horizon without needing Agile's own coverage-counting
        # dance. One row per term length (see poll_edf_goelec_rates for
        # why). Export uses edf_export_rate, NOT the slot's own live
        # export price — a real switch to EDF moves your export contract
        # to EDF's own rate too, unlike the Agile row above where import
        # and export stay with the same Octopus account.
        for term, prod in self.edf_goelec_products.items():
            edf_imp = [core_slots.rate_at(prod["windows"], s["start"], self.default_import)
                       for s in slots]
            cp = [dict(s, charge=0.0, export=0.0, imp=edf_imp[i], exp=self.edf_export_rate)
                  for i, s in enumerate(slots)]
            result = core_optimizer.solve(cp, soc0, self.cfg, today_date=now.date())
            if result.infeasible:
                self.log(f"EDF GoElectric ({term}) comparison reported infeasible — "
                         "skipping it this tick.", level="WARNING")
                continue
            standing = prod["standing_gbp"] * horizon_hours / 24.0
            lo, hi = min(edf_imp) * 100, max(edf_imp) * 100
            rows.append((f"EDF GoElectric ({term}, live)", result.grid_cost + standing, True,
                         {"import_desc": f"{round(lo, 1)}p–{round(hi, 1)}p live (off-peak/day)",
                          "export_p": round(self.edf_export_rate * 100, 1),
                          "standing_p": round(prod["standing_gbp"] * 100, 1)},
                         horizon_hours))

        # Ranked by £/day, not raw £ total — Agile's row covers whatever
        # prefix of the horizon Octopus has actually published (often far
        # short of the full 48h every other row uses), so sorting on the
        # raw total would let a row win purely for covering fewer hours,
        # independent of whether its rate is actually any cheaper.
        # Confirmed live: without this, "best" was technically still
        # correct on the day it was checked, but only because the
        # per-day gap happened to be wide enough to survive the
        # normalisation too — not something the raw-total sort actually
        # guaranteed.
        rows.sort(key=lambda r: r[1] / r[4])
        best_per_day = rows[0][1] / rows[0][4]
        html_rows = "".join(
            f"<tr><td>{n}</td><td>£{c:.2f}</td>"
            f"<td>{'—' if abs(c / h - best_per_day) < 1e-9 else f'+£{(c / h - best_per_day) * h:.2f}'}</td></tr>"
            for n, c, _, _, h in rows)
        html = ("<table class='gridlock-plan'><tr><th>Tariff</th>"
                f"<th>{horizon_hours:.0f}h cost</th><th>vs best</th></tr>" + html_rows +
                "</table>")
        ev_charge_hours, ev_high_price_notify_p = self._ev_schedule_settings()
        self.set_state("sensor.gridlock_tariff_compare", state=rows[0][0],
                       attributes={"friendly_name": "GridLock Tariff Compare",
                                   "compare_html": html,
                                   "results": [{"name": n, "cost": c, "is_live": is_live,
                                                 "cost_per_day": round(c / h * 24, 2), **rate_info}
                                               for n, c, is_live, rate_info, h in rows],
                                   "ev_schedule": {"daily_charge_hours": ev_charge_hours,
                                                    "high_price_notify_p": ev_high_price_notify_p,
                                                    "on_agile": self._is_on_agile_import()}})

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
    # (see core.thermal.decide_heating_command), with preconditions on
    # the device itself checked first and a read-back check after. This
    # is the one thing in GridLock that writes to a heating device at
    # all -- a hot water tank's force-heat switch, or the heat pump's
    # own space-heating power switch. Zones come from apps.yaml or from
    # the dashboard (core/space_heating.py); same code path downstream.
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
        # tank_litres is what makes a zone a tank rather than a room --
        # the control defaults below differ by kind (a tank's floor is a
        # hot-water figure with a cap for anti-legionella cycles; a
        # room's is a frost/comfort floor with no cap at all).
        is_tank = "tank_litres" in z
        return {"name": z.get("name", "Zone"),
                "internal_temp_entity": z.get("internal_temp_entity"),
                "internal_temp_attribute": z.get("internal_temp_attribute"),
                # Second-choice room reading for when the first is
                # unavailable -- the heat pump's own controller sensor,
                # typically. Read the same way (climate.* auto-detected).
                "internal_temp_fallback_entity": z.get("internal_temp_fallback_entity"),
                "target_temp_entity": z.get("target_temp_entity"),
                "target_temp_attribute": z.get("target_temp_attribute"),
                "external_temp_entity": z.get("external_temp_entity"),
                "external_temp_constant": z.get("external_temp_constant"),
                "weather_entity": z.get("weather_entity"),
                "heating_entity": z.get("heating_entity"),
                # The heating_entity state that means "heating" -- "on" for
                # a binary/switch, or a text sensor's own word (the Midea
                # controller's active-state sensor says "Heating", and
                # unlike its compressor flag isn't also on during a hot
                # water run).
                "heating_entity_state": str(z.get("heating_entity_state", "on")),
                # A fixed target for a zone with no target entity (a room
                # with no thermostat worth trusting -- the dashboard zone
                # always works this way, its target set from the UI).
                "target_temp_constant": (float(z["target_temp_constant"])
                                         if z.get("target_temp_constant") is not None else None),
                "is_tank": is_tank,
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
                "control_safety_min_temp": float(z.get("control_safety_min_temp",
                                                       45.0 if is_tank else 16.0)),
                "control_max_off_hours": float(z.get("control_max_off_hours",
                                                     6.0 if is_tank else 0.0)),
                # Preconditions on the device itself (see
                # core.thermal.unmet_requirements) -- checked before every
                # write; unmet means Blocked, not a command into the void.
                "control_requires": core_space_heating.clean_requirements(
                    z.get("control_requires")),
                # Dynamic flow temperature (core/flow_control.py) -- opt-in
                # via flow_entity. The emitter rating, COP factor and
                # thermal mass are starting points that self-refine.
                "flow_entity": z.get("flow_entity"),
                "flow_min": float(z.get("flow_min", 35.0)),
                "flow_max": float(z.get("flow_max", 50.0)),
                "flow_max_step": float(z.get("flow_max_step", 3.0)),
                "flow_fallback": float(z.get("flow_fallback", 40.0)),
                "water_dt": float(z.get("water_dt", 5.0)),
                "emitter_rated_w_at_dt50": float(z.get("emitter_rated_w_at_dt50", 10000.0)),
                "config_emitter_rated_w_at_dt50": float(z.get("emitter_rated_w_at_dt50", 10000.0)),
                "cop_eta": float(z.get("cop_eta", 0.45)),
                "config_cop_eta": float(z.get("cop_eta", 0.45)),
                "config_thermal_mass_wh_per_c": mass,
                "comfort_below": float(z.get("comfort_below", 1.0)),
                "comfort_above": float(z.get("comfort_above", 1.0)),
                "tariff_optimise": bool(z.get("tariff_optimise", True)),
                "comfort_priority": str(z.get("comfort_priority", "cop")),
                "min_on_minutes": float(z.get("min_on_minutes", 0.0 if is_tank else 30.0)),
                "min_off_minutes": float(z.get("min_off_minutes", 0.0 if is_tank else 20.0)),
                "emergency_temp": float(z.get("emergency_temp", 10.0)),
                "emergency_stop_temp": float(z.get("emergency_stop_temp", 12.0)),
                "emergency_flow": float(z.get("emergency_flow", 25.0)),
                "solar_gain_w_per_kw_pv": float(z.get("solar_gain_w_per_kw_pv", 0.0)),
                "thermal_power_entity": z.get("thermal_power_entity"),
                "flow_temp_entity": z.get("flow_temp_entity"),
                "return_temp_entity": z.get("return_temp_entity"),
                "compressor_entity": z.get("compressor_entity"),
                # Hot water scheduling (core/dhw_plan.py) -- set by the
                # dashboard hot water zone; None for everything else.
                "dhw": z.get("dhw"),
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

    def _apply_learned_thermal(self, zone):
        """Re-apply everything previously learned about a zone (persisted
        in thermal_learning_state.json) over its configured starting
        figures -- at startup and whenever a zone is rebuilt from the
        dashboard settings, so neither resets what the model has learned
        about the house back to the defaults."""
        saved = self.thermal_learning_state.get(zone["name"], {})
        params = zone["params"]
        if saved.get("heat_loss_degrees") is not None:
            params.heat_loss_degrees = saved["heat_loss_degrees"]
        if saved.get("heat_loss_watts") is not None:
            params.heat_loss_watts = saved["heat_loss_watts"]
        if saved.get("thermal_mass_wh_per_c") is not None:
            params.thermal_mass_wh_per_c = saved["thermal_mass_wh_per_c"]
        if saved.get("emitter_w_at_dt50") is not None:
            zone["emitter_rated_w_at_dt50"] = saved["emitter_w_at_dt50"]
        if saved.get("cop_eta") is not None:
            zone["cop_eta"] = saved["cop_eta"]

    def _hp_live(self, zone):
        """The heat pump's own live readings a flow-controlled zone uses
        for learning and shows on the dashboard. All optional."""
        g = lambda key: (self.get_float_state(zone[key], None) if zone.get(key) else None)  # noqa: E731
        return {"thermal_kw": g("thermal_power_entity"),
                "flow_temp": g("flow_temp_entity"),
                "return_temp": g("return_temp_entity"),
                "compressor_hz": g("compressor_entity"),
                "live_cop": g("cop_entity"),
                "flow_setpoint_live": g("flow_entity")}

    def _update_flow_learning(self, zone, now, internal_temp, external_temp, heating_on, hp):
        """Learns the three numbers dynamic flow control depends on, from
        the heat pump's own live readings while it's heating:
        - the emitter (radiator) rating, from measured thermal output vs
          how far the mean water temperature is above the room;
        - the COP factor (fraction of Carnot), from the live COP sensor
          vs flow and outdoor temperature;
        - the house's thermal mass, from heat delivered over a continuous
          heating run of an hour or more vs how much the room rose
          (shorter intervals drown in the thermometer's 0.1 °C steps).
        Same gradual EMA blend as every other learned figure here, each
        clamped to a physically plausible range. Must run BEFORE
        _update_thermal_learning, which overwrites the prior-tick state
        this reads."""
        if not zone["flow_entity"]:
            return
        state = self.thermal_learning_state.setdefault(zone["name"], {})
        params = zone["params"]
        thermal_w = (hp["thermal_kw"] or 0.0) * 1000.0
        prior_ts, prior_on = state.get("ts"), state.get("heating_on")
        try:
            elapsed_h = (now - self._iso(prior_ts)).total_seconds() / 3600.0 if prior_ts else None
        except (ValueError, TypeError):
            elapsed_h = None
        if heating_on and thermal_w > 300.0:
            if hp["flow_temp"] is not None and hp["return_temp"] is not None:
                mean = (hp["flow_temp"] + hp["return_temp"]) / 2.0
                obs = core_flow.implied_emitter_rating(thermal_w, mean, internal_temp)
                if obs is not None and 500.0 <= obs <= 100000.0:
                    zone["emitter_rated_w_at_dt50"] = core_flow.blend(zone["emitter_rated_w_at_dt50"], obs)
            # The controller's COP sensor is a lifetime average (total kWh
            # out / total kWh in), not a live figure -- learning the Carnot
            # factor from it per tick would just learn a constant. eta
            # stays at its configured value until a real per-run COP
            # source exists.
            run = state.get("heat_run")
            if run is None or not prior_on or elapsed_h is None or elapsed_h > 0.3:
                run = {"start_ts": now.isoformat(), "start_temp": internal_temp, "wh": 0.0, "loss_wh": 0.0}
            else:
                loss_w = core_flow.steady_state_loss_w(internal_temp, external_temp, params)
                run["wh"] += thermal_w * elapsed_h
                run["loss_wh"] += loss_w * elapsed_h
                try:
                    run_h = (now - self._iso(run["start_ts"])).total_seconds() / 3600.0
                except (ValueError, TypeError, KeyError):
                    run_h = 0.0
                rise = internal_temp - run["start_temp"]
                if run_h >= 1.0 and rise >= 0.3:
                    obs = core_flow.implied_thermal_mass(run["wh"] / run_h, run["loss_wh"] / run_h, rise, run_h)
                    if obs is not None and 500.0 <= obs <= 50000.0:
                        params.thermal_mass_wh_per_c = core_flow.blend(params.thermal_mass_wh_per_c, obs, alpha=0.1)
                    run = {"start_ts": now.isoformat(), "start_temp": internal_temp, "wh": 0.0, "loss_wh": 0.0}
            state["heat_run"] = run
        else:
            state["heat_run"] = None
        state["emitter_w_at_dt50"] = zone["emitter_rated_w_at_dt50"]
        state["cop_eta"] = zone["cop_eta"]
        state["thermal_mass_wh_per_c"] = params.thermal_mass_wh_per_c

    def _update_dhw_learning(self, zone, now, tank_temp, heating_on):
        """Learns when this house draws hot water (core/dhw_plan.py's
        usage profile): every tick's tank-temperature drop beyond
        standing loss, while not heating, is hot water used in that
        half-hour of the day; today's buckets are blended into the
        learned profile at the first tick of each new day."""
        learn = self.thermal_learning_state.setdefault(zone["name"], {})
        params = zone["params"]
        local = now.astimezone()
        date_key = local.date().isoformat()
        bucket = core_dhw.profile_bucket(local)
        day = learn.get("dhw_day_buckets")
        if learn.get("dhw_day_date") != date_key:
            if day is not None and learn.get("dhw_day_date"):
                learn["dhw_profile"] = core_dhw.blend_day_into_profile(learn.get("dhw_profile"), day)
                learn["dhw_profile_days"] = int(learn.get("dhw_profile_days", 0)) + 1
            day = [None] * core_dhw.PROFILE_SLOTS
            learn["dhw_day_date"] = date_key
        prev_ts, prev_temp, prev_on = learn.get("dhw_prev_ts"), learn.get("dhw_prev_temp"), learn.get("dhw_prev_on")
        if prev_ts is not None:
            try:
                elapsed_h = (now - self._iso(prev_ts)).total_seconds() / 3600.0
            except (ValueError, TypeError):
                elapsed_h = None
            if elapsed_h is not None:
                wh = core_dhw.draw_wh(prev_temp, tank_temp, elapsed_h, params.heat_loss_watts,
                                      params.thermal_mass_wh_per_c, heating_on or bool(prev_on))
                if wh is not None:
                    day[bucket] = (day[bucket] or 0.0) + wh
        learn["dhw_day_buckets"] = day
        learn["dhw_prev_ts"], learn["dhw_prev_temp"], learn["dhw_prev_on"] = now.isoformat(), tank_temp, heating_on

    def _plan_dhw(self, zone, now, tank_temp, setpoint, external_curve, rate_curve,
                  grid_rate_curve=None, pricing_mode="tariff"):
        """When the DHW circuit gets power (see core/dhw_plan.py): the
        cheapest window by price / predicted COP that finishes before
        the deadline -- the learned usage peak if one is coming, else
        the max-interval guarantee -- committed once it starts so a
        re-plan can't abandon a half-done run, and skipped when the tank
        already covers the learned usage until the next window."""
        d = zone["dhw"]
        per_slot = SLOT_MIN // THERMAL_STEP_MIN
        slot_hours = SLOT_MIN / 60.0
        idx = list(range(0, len(external_curve), per_slot))
        ext = [external_curve[j] for j in idx]
        rates = [rate_curve[j] * 100.0 for j in idx]           # £ -> p
        grid_rates = [(grid_rate_curve or rate_curve)[j] * 100.0 for j in idx]
        setpoint = setpoint if setpoint is not None else 53.0
        hyst = (self.get_float_state(d["start_hysteresis_entity"], 2.0)
                if d.get("start_hysteresis_entity") else 2.0)
        run_slots = core_dhw.run_slots_needed(d["run_minutes"], SLOT_MIN)
        state = self._thermal_control_state.setdefault(
            zone["name"], {"off_since": None, "last_commanded": None, "rejected": False,
                           "rejected_notified": False, "blocked_key": None})
        # Legionella cycle bookkeeping lives in the persisted learning state
        # so a restart mid-week (or mid-cycle) doesn't lose it.
        lg_cfg = d.get("legionella") or {}
        lg = self.thermal_learning_state.setdefault(zone["name"], {}).setdefault("legionella", {})
        lg_due = False
        lg_days_since = None
        if lg_cfg.get("enabled"):
            try:
                lg_days_since = ((now - self._iso(lg["last_done"])).total_seconds() / 86400.0
                                 if lg.get("last_done") else None)
            except (ValueError, TypeError):
                lg_days_since = None
            lg_due = lg_days_since is None or lg_days_since >= lg_cfg["interval_days"]
            try:
                if lg_due and lg.get("retry_after") and now < self._iso(lg["retry_after"]):
                    lg_due = False   # failed recently -- not again until the next night
            except (ValueError, TypeError):
                pass
            if lg_due and not lg.get("phase"):
                # Window sized from the actual tank: the heat pump's share at
                # its DHW output, the rest at the immersion's, plus the hold
                # and a 10-minute margin -- capped by the configured maximum.
                mass = max(zone["params"].thermal_mass_wh_per_c, 1.0)
                t0 = tank_temp if tank_temp is not None else setpoint - hyst
                hp_lim = d.get("hp_max_tank_temp", 55.0)
                hp_wh = max(0.0, min(lg_cfg["temp"], hp_lim) - t0) * mass
                imm_wh = max(0.0, lg_cfg["temp"] - max(t0, hp_lim)) * mass
                imm_w = d.get("immersion_power_w", 2000.0)
                heat_min = (hp_wh / max(zone["params"].heat_max_power or 3500.0, 500.0)
                            + imm_wh / max(imm_w, 500.0)) * 60.0
                cycle_min = min(lg_cfg["window_minutes"], heat_min + lg_cfg["hold_minutes"] + 10.0)
                run_slots = max(run_slots, core_dhw.run_slots_needed(cycle_min, SLOT_MIN))
        # Cost of a kWh of hot water per slot: the COP at the flow the run
        # needs (setpoint + margin, or the legionella temperature on a cycle
        # night) in that slot's outdoor temperature -- colder air, worse COP
        # -- with the share above the heat pump's own DHW limit priced at
        # COP 1, because the immersion does that part.
        heat_target = lg_cfg.get("temp", setpoint) if (lg_due or lg.get("phase")) else setpoint
        hp_limit = d.get("hp_max_tank_temp", 55.0)
        flow = min(heat_target, hp_limit) + d["flow_margin"]
        start_t = min(tank_temp if tank_temp is not None else setpoint - hyst, heat_target - 1.0)
        hp_part = max(0.0, min(heat_target, hp_limit) - start_t)
        imm_part = max(0.0, heat_target - max(start_t, hp_limit))
        cops = []
        for e in ext:
            c = core_flow.cop_estimate(flow, e, d["cop_eta"])
            cops.append((hp_part + imm_part) / (hp_part / c + imm_part / 1.0) if (hp_part + imm_part) > 0 else c)
        scores = [rates[i] / max(cops[i], 0.5) for i in range(min(len(rates), len(cops)))]
        committed = state.get("dhw_window")
        if committed:
            try:
                if now >= self._iso(committed["end"]):
                    state["dhw_last_window_end"] = committed["end"]
                    committed = state["dhw_window"] = None
            except (ValueError, TypeError, KeyError):
                committed = state["dhw_window"] = None
        last_end = state.get("dhw_last_window_end")
        try:
            hours_since = (now - self._iso(last_end)).total_seconds() / 3600.0 if last_end else None
        except (ValueError, TypeError):
            hours_since = None
        deadline = core_dhw.deadline_slots(hours_since, d["max_interval_hours"], slot_hours)
        learn = self.thermal_learning_state.get(zone["name"], {})
        profile = learn.get("dhw_profile")
        bucket = core_dhw.profile_bucket(now.astimezone())
        expected = core_dhw.expected_draw(profile, bucket, len(scores))
        # Learned usage informs the "skip, the tank covers it" check below,
        # but never forces a run: the tank stratifies, so a cool sensor still
        # has showers at the top. Runs are driven by the ready-by times.
        ready = core_dhw.ready_by_slot(expected, d.get("draw_threshold_wh", 300.0), lead_slots=1)
        # The configured ready-by times (hot tank for the morning, showers
        # by the evening) bound the deadline whenever a run is actually
        # needed -- the tank is below the point the unit starts at.
        by = core_dhw.slots_until_ready_by(now.astimezone(), d.get("ready_by_times") or [], SLOT_MIN)
        if by is not None and core_dhw.unit_will_heat(tank_temp, setpoint, hyst):
            deadline = min(deadline, max(run_slots, by))
        # Genuinely low tank: top it up in the cheapest slot of the next
        # couple of hours rather than waiting for the night.
        low = tank_temp is not None and tank_temp < d.get("top_up_below", 25.0)
        if low:
            deadline = min(deadline, run_slots + 4)
        window = core_dhw.cheapest_window(scores, run_slots, deadline)
        # A window only starts when there's something to heat: the tank
        # is below the point the unit would start a run at, or the daily
        # guarantee is up. Otherwise "cheapest window is now" on a flat
        # tariff would run the circuit back to back for ever.
        # A run is due when the tank is below the unit's top-up point AND
        # the next ready-by is what the window is racing (the cheapest slot
        # before it gets chosen), when it's below the low threshold, on a
        # legionella night, or when a configured forced-run timer is up.
        needed = (low or lg_due or deadline <= run_slots
                  or (by is not None and core_dhw.unit_will_heat(tank_temp, setpoint, hyst)))
        skipped = False
        # A running legionella cycle never gets a fresh window booked under
        # it -- when its window ends the cycle stops (below), full stop.
        if not committed and window and window[0] == 0 and needed and not lg.get("phase"):
            horizon = max(run_slots, deadline)
            if profile and core_dhw.tank_can_cover(
                    tank_temp, setpoint, hyst, zone["params"].thermal_mass_wh_per_c,
                    sum(expected[:horizon]), zone["params"].heat_loss_watts, horizon * slot_hours):
                # Warm enough for everything the house is expected to draw
                # before the next chance -- count it as done, don't run.
                state["dhw_last_window_end"] = now.isoformat()
                skipped = True
            else:
                committed = {"start": now.isoformat(),
                             "end": (now + timedelta(minutes=SLOT_MIN * run_slots)).isoformat()}
                state["dhw_window"] = committed
        if committed:
            start_iso, end_iso = committed["start"], committed["end"]
        elif window:
            # Projected window: slot-aligned like the chart, not "now + n".
            start_iso = (self._slot_floor(now) + timedelta(minutes=SLOT_MIN * window[0])).isoformat()
            end_iso = (self._slot_floor(now) + timedelta(minutes=SLOT_MIN * window[1])).isoformat()
        else:
            start_iso = end_iso = None
        w_cop = (sum(cops[window[0]:window[1]]) / run_slots) if window else None
        w_price = (sum(rates[window[0]:window[1]]) / run_slots) if window else None
        # Legionella cycle: starts with a committed window, holds the
        # setpoint at legionella_temp until the tank has been there for
        # the hold time, then restores the setpoint it found.
        setpoint_wanted = None
        lg_changed = False
        controllable = (self.get_state(self.ent_gridwarm_mode) == "active"
                        and self.get_state(self._thermal_pause_helper(zone["name"])) != "off")
        if lg_cfg.get("enabled") and lg_due and not lg.get("phase") and committed and controllable:
            lg.update({"phase": "heating", "started": now.isoformat(), "hold_start": None,
                       "orig_setpoint": (setpoint if setpoint < lg_cfg["temp"] - 0.5
                                         else lg.get("orig_setpoint") or 53.0),
                       "immersion_used": False})
            lg_changed = True
            self._log_decision(f"GridWarm: {zone['name']} legionella cycle",
                               f"Heating the tank to {lg_cfg['temp']:.0f}°C and holding it "
                               f"{lg_cfg['hold_minutes']:.0f} min in this window")
        phase = lg.get("phase")
        if phase:
            orig = lg.get("orig_setpoint") or 53.0
            try:
                elapsed_h = (now - self._iso(lg["started"])).total_seconds() / 3600.0
            except (ValueError, TypeError, KeyError):
                elapsed_h = 0.0
            if d.get("immersion_entity") and self.get_state(d["immersion_entity"]) == "on" \
                    and not lg.get("immersion_used"):
                lg["immersion_used"] = True
                lg_changed = True
            # The tank sensor reads whole degrees and the unit stops a hair
            # short of its setpoint, so "at temperature" means within 1 °C.
            reached = tank_temp is not None and tank_temp >= lg_cfg["temp"] - 1.0
            if not lg_cfg.get("enabled"):
                lg["phase"] = None
                setpoint_wanted = orig
            elif elapsed_h > lg_cfg["max_hours"] or committed is None:
                # Out of time, or the planned window is over: never hold the
                # circuit on into dearer hours chasing it. Try tomorrow night.
                lg.update({"phase": None, "last_failed": now.isoformat(),
                           "retry_after": (now + timedelta(hours=12)).isoformat()})
                setpoint_wanted = orig
                self._log_decision(f"GridWarm: {zone['name']} legionella cycle abandoned",
                                   f"Tank didn't hold {lg_cfg['temp']:.0f}°C "
                                   + ("before the window ended" if committed is None else
                                      f"within {lg_cfg['max_hours']:.0f} h")
                                   + " — setpoint restored, retrying tomorrow night")
            elif phase == "heating":
                setpoint_wanted = lg_cfg["temp"]
                if reached:
                    lg.update({"phase": "holding", "hold_start": now.isoformat()})
            elif phase == "holding":
                setpoint_wanted = lg_cfg["temp"]
                if tank_temp is not None and tank_temp < lg_cfg["temp"] - 2.0:
                    # Dropped out of the band (a big draw-off) -- the hold restarts.
                    lg.update({"phase": "heating", "hold_start": None})
                try:
                    held_min = (now - self._iso(lg["hold_start"])).total_seconds() / 60.0
                except (ValueError, TypeError, KeyError):
                    held_min = 0.0
                if lg.get("phase") == "holding" and held_min >= lg_cfg["hold_minutes"]:
                    lg.update({"phase": None, "last_done": now.isoformat(), "retry_after": None})
                    setpoint_wanted = orig
                    self._log_decision(f"GridWarm: {zone['name']} legionella cycle done",
                                       f"Held {lg_cfg['temp']:.0f}°C for {lg_cfg['hold_minutes']:.0f} min"
                                       + (" (immersion heater helped)" if lg.get("immersion_used") else
                                          " on the heat pump alone"))
            lg_changed = lg_changed or lg.get("phase") != phase
        lg_active = bool(lg.get("phase"))
        if lg_due and lg.get("last_done") == now.isoformat():
            lg_due = False   # finished this tick
        if lg_changed:
            self._save_json("thermal_learning_state.json", self.thermal_learning_state)
        # A plain word for the dashboard -- booleans that are False don't
        # survive the trip through HA's attribute store (see _never_zero_deep).
        if committed is not None:
            status = "running"
        elif skipped:
            status = "skipped"
        elif not needed:
            status = "not_needed"
        else:
            status = "waiting"
        if lg_active:
            status = "running"
        # Predicted tank temperature per slot, for the chart: heats at the
        # unit's DHW output during the planned window up to the setpoint
        # (or the legionella temperature on a cycle), otherwise cools by
        # standing loss plus the learned draw for that half-hour.
        n_show = min(48, len(scores))
        if committed is not None or lg_active:
            w_lo, w_hi = 0, run_slots if not lg_active else n_show
            if committed is not None:
                try:
                    w_hi = max(1, int(math.ceil((self._iso(committed["end"]) - now).total_seconds() / 1800.0)))
                except (ValueError, TypeError, KeyError):
                    pass
        elif window and needed:
            w_lo, w_hi = window[0], window[1]
        else:
            w_lo = w_hi = -1
        heat_to = (lg_cfg.get("temp", setpoint) if (lg_due or lg_active) else setpoint)
        mass = max(zone["params"].thermal_mass_wh_per_c, 1.0)
        tank_curve = []
        tk = tank_temp if tank_temp is not None else setpoint - hyst
        for i in range(n_show):
            if w_lo <= i < w_hi and tk < heat_to:
                tk = min(heat_to, tk + (zone["params"].heat_max_power or 3500.0) * slot_hours / mass)
            else:
                tk = tk - (zone["params"].heat_loss_watts * slot_hours + (expected[i] if i < len(expected) else 0.0)) / mass
            tank_curve.append(round(tk, 1))
        try:
            lg_next = (self._iso(lg["last_done"]) + timedelta(days=lg_cfg.get("interval_days", 7))).isoformat() \
                if lg.get("last_done") else None
        except (ValueError, TypeError):
            lg_next = None
        # Why a run is booked, for the panel: the tank needs it, the weekly
        # legionella cycle is due, or only the daily guarantee is.
        reason = ("legionella" if (lg_due or lg_active) else
                  "top_up" if core_dhw.unit_will_heat(tank_temp, setpoint, hyst) else
                  "guarantee" if needed else "none")
        return {"heat_now": committed is not None,
                "committed": committed is not None,
                "reason": reason,
                "setpoint_wanted": setpoint_wanted,
                "legionella": {"enabled": bool(lg_cfg.get("enabled")), "due": lg_due,
                               "phase": lg.get("phase") or ("due" if lg_due else "idle"),
                               "temp": lg_cfg.get("temp"), "hold_minutes": lg_cfg.get("hold_minutes"),
                               "last_done": lg.get("last_done"), "next_due": lg_next,
                               "hold_start": lg.get("hold_start"),
                               "immersion_used": "yes" if lg.get("immersion_used") else "no",
                               "last_failed": lg.get("last_failed")},
                "status": status,
                "pricing_mode": pricing_mode,
                "needed": needed,
                "skipped_covered": skipped,
                "window_start": start_iso, "window_end": end_iso,
                "window_price": round(w_price, 2) if w_price is not None else None,
                "window_cop": round(w_cop, 2) if w_cop is not None else None,
                "window_score": round(window[2], 2) if window else None,
                "price_now": round(rates[0], 2) if rates else None,
                "cop_now": round(cops[0], 2) if cops else None,
                "deadline_hours": round(deadline * slot_hours, 1),
                "ready_by": ((now + timedelta(minutes=SLOT_MIN * ready)).isoformat()
                             if ready is not None else None),
                "hours_since_last": round(hours_since, 1) if hours_since is not None else None,
                "will_heat": core_dhw.unit_will_heat(tank_temp, setpoint, hyst),
                "setpoint": setpoint, "start_hysteresis": hyst,
                "usage_profile_wh": [round(v, 0) for v in (profile or [])],
                "usage_days_learned": int(learn.get("dhw_profile_days", 0)),
                "expected_24h_kwh": round(sum(expected[:48]) / 1000.0, 2),
                "slots": [{"x": (self._slot_floor(now) + timedelta(minutes=SLOT_MIN * i)).isoformat(),
                           "price": round(rates[i], 2), "grid_price": round(grid_rates[i], 2),
                           "cop": round(cops[i], 2),
                           "tank": tank_curve[i] if i < len(tank_curve) else None,
                           "score": round(scores[i], 2)} for i in range(min(48, len(scores)))]}

    @staticmethod
    def _slot_floor(now):
        """The start of the tariff half-hour containing now -- what slot 0
        of every per-slot curve here actually covers."""
        return now.replace(minute=(now.minute // SLOT_MIN) * SLOT_MIN, second=0, microsecond=0)

    def _plan_flow(self, zone, now, internal_temp, external_curve, plan_curve, rate_curve,
                   grid_rate_curve=None, pricing_mode="tariff"):
        """The flow-temperature plan for one tick (see core/flow_control.py):
        a least-cost heat schedule over the horizon at half-hour
        resolution, then the flow setpoint that delivers its first slot
        at the lowest temperature the radiators allow."""
        per_slot = SLOT_MIN // THERMAL_STEP_MIN
        slot_hours = SLOT_MIN / 60.0
        idx = list(range(0, len(external_curve), per_slot))
        ext = [external_curve[j] for j in idx]
        tgt = [plan_curve[j] for j in idx]
        # Curves arrive in £/kWh; the planner and the dashboard work in pence.
        rates = ([rate_curve[j] * 100.0 for j in idx] if zone["tariff_optimise"]
                 else [1.0] * len(idx))
        grid_rates = [(grid_rate_curve or rate_curve)[j] * 100.0 for j in idx]
        params = zone["params"]
        emitter, eta = zone["emitter_rated_w_at_dt50"], zone["cop_eta"]
        max_w = params.heat_max_power or 7000.0
        low_w = min(max_w, core_flow.emitter_output_w(
            zone["flow_min"] - zone["water_dt"] / 2.0, tgt[0] if tgt else internal_temp, emitter))
        cop_low = [core_flow.cop_estimate(zone["flow_min"], e, eta) for e in ext]
        cop_high = [core_flow.cop_estimate(zone["flow_max"], e, eta) for e in ext]
        segment_curve = [core_flow.heat_segments(tgt[i], ext[i], emitter, eta, zone["flow_min"],
                                                 zone["flow_max"], zone["water_dt"], 5.0, max_w)
                         for i in range(len(ext))]
        hp = self._hp_live(zone)
        delivering_kwh = ((hp["thermal_kw"] or 0.0) * slot_hours
                          if self._thermal_heating_on(zone) else 0.0)
        pv_kw = getattr(self, "_thermal_pv_kw", None) or []
        gain_curve = [pv_kw[j] * zone.get("solar_gain_w_per_kw_pv", 0.0) if j < len(pv_kw) else 0.0
                      for j in idx]
        plan, status = core_flow.plan_heating(
            internal_temp, ext, rates, tgt, params,
            comfort_below=zone["comfort_below"], comfort_above=zone["comfort_above"],
            slot_hours=slot_hours, low_flow_power_w=low_w, cop_low_curve=cop_low,
            cop_high_curve=cop_high, max_power_w=max_w, solver=core_optimizer._solver(),
            heat_kwh_prev=delivering_kwh, gain_curve_w=gain_curve, segment_curve=segment_curve,
            comfort_cost_per_degree_slot=core_flow.COMFORT_COST_BY_PRIORITY.get(
                zone.get("comfort_priority", "cop"), core_flow.COMFORT_COST_PER_DEGREE_SLOT))
        if plan:
            required_w = max(0.0, plan[0]["heat_kwh"] * 1000.0 / slot_hours)
        else:
            required_w = max(0.0, core_flow.steady_state_loss_w(internal_temp, ext[0] if ext else 10.0, params))
        wanted = core_flow.flow_setpoint_for_power(
            required_w, internal_temp, emitter, zone["water_dt"], zone["flow_min"], zone["flow_max"], 1.0)
        ext_now = ext[0] if ext else 10.0
        steps_per_day = int(24 / slot_hours)
        today = plan[:steps_per_day]
        heat_kwh = sum(p["heat_kwh"] for p in today)
        elec_kwh = sum(p["heat_kwh"] / max(p["cop"], 0.1) for p in today)
        return {"required_w": required_w,
                "pricing_mode": pricing_mode,
                "required_kw": round(required_w / 1000.0, 2),
                "flow_setpoint_wanted": wanted,
                "predicted_cop": round(core_flow.cop_estimate(wanted, ext_now, eta), 2),
                "low_flow_kw": round(low_w / 1000.0, 2),
                "plan_status": status,
                "plan_heat_kwh_today": round(heat_kwh, 2),
                "plan_elec_kwh_today": round(elec_kwh, 2),
                "plan_cost_today": round(sum(p["cost"] for p in today), 2),
                "plan": [{"x": (self._slot_floor(now) + timedelta(minutes=SLOT_MIN * i)).isoformat(),
                          "heat_kwh": round(p["heat_kwh"], 3), "temp": round(p["temp"], 2),
                          "target": round(tgt[i], 2), "external": round(ext[i], 1),
                          "price": round(p["price"], 2), "grid_price": round(grid_rates[i], 2),
                          "cop": round(p["cop"], 2),
                          "solar_gain_w": round(gain_curve[i], 0) if i < len(gain_curve) else 0.0,
                          "flow": core_flow.flow_setpoint_for_power(
                              p["heat_kwh"] * 1000.0 / slot_hours, p["temp"], emitter,
                              zone["water_dt"], zone["flow_min"], zone["flow_max"], 1.0)
                          if p["heat_kwh"] > 1e-6 else None}
                         for i, p in enumerate(today)]}

    def _thermal_internal_temp(self, zone):
        """A climate.* entity's own state is its HVAC mode ("heat"/"off"),
        not a temperature — its live reading is the current_temperature
        attribute instead. Auto-detect that by domain so a room zone
        pointed at a climate entity (the common case) works with no
        extra config; internal_temp_attribute can still override it for
        anything that doesn't follow that convention."""
        for ent, is_fallback in ((zone["internal_temp_entity"], False),
                                 (zone.get("internal_temp_fallback_entity"), True)):
            if not ent:
                continue
            attr = (None if is_fallback else zone["internal_temp_attribute"]) or (
                "current_temperature" if ent.startswith("climate.") else None)
            if attr:
                v = self.get_state(ent, attribute=attr)
                try:
                    value = float(v) if v not in (None, "unknown", "unavailable") else None
                except (ValueError, TypeError):
                    value = None
            else:
                value = self.get_float_state(ent, None)
            if value is not None:
                zone["_reading_source"] = ent if is_fallback else None
                return value
        zone["_reading_source"] = None
        return None

    def _thermal_target_temp(self, zone):
        ent = zone["target_temp_entity"]
        if not ent:
            return zone.get("target_temp_constant")
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
        if zone["external_temp_constant"] is not None:
            return float(zone["external_temp_constant"])
        # No outdoor sensor configured: every weather entity carries the
        # current outdoor temperature, so take it from the one serving
        # the forecast (or the configured/any one).
        for ent in self._weather_candidates(zone.get("weather_entity")):
            v = self.get_state(ent, attribute="temperature")
            try:
                if v not in (None, "unknown", "unavailable"):
                    return float(v)
            except (ValueError, TypeError):
                continue
        return 18.0

    @staticmethod
    def _forecast_points(resp, ent):
        """The forecast list out of whatever shape the service response
        arrives in. AppDaemon hands back HA's websocket result, so the
        per-entity data sits under "response" (and sometimes "result"
        above that) -- reading it as if it were the bare per-entity dict
        found nothing, silently, and left the model with a flat outdoor
        curve for a whole day."""
        if not isinstance(resp, dict):
            return []
        for layer in (resp, resp.get("result"), resp.get("response"),
                      (resp.get("result") or {}).get("response") if isinstance(resp.get("result"), dict) else None):
            if isinstance(layer, dict):
                ent_data = layer.get(ent)
                if isinstance(ent_data, dict) and isinstance(ent_data.get("forecast"), list):
                    return ent_data["forecast"]
        return []

    def _weather_candidates(self, configured):
        """Weather entities to ask for a forecast, best first: the one
        that worked last time, the configured one, then every weather.*
        entity HA has. Some (the OpenWeatherMap free tier's) serve no
        forecast at all -- auto-detecting means nobody has to know which
        of theirs does."""
        out = []
        for ent in (self._thermal_weather_ok, configured):
            if ent and ent not in out and self.entity_exists(ent):
                out.append(ent)
        try:
            for ent in self.registry.find_all(domain="weather"):
                if ent not in out:
                    out.append(ent)
        except Exception as exc:  # noqa: BLE001 — registry lookup is best-effort, but say so
            self.log(f"GridWarm: weather entity lookup failed ({exc!r})", level="WARNING")
        return out

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
        used = None
        for ent in self._weather_candidates(weather_entity):
            raw = self.get_state(ent, attribute="forecast")
            if isinstance(raw, list) and raw:
                points, used = raw, ent
                break
            warned_at = self._thermal_forecast_warned.get(ent)
            if warned_at is not None and (now - warned_at).total_seconds() < 3600:
                continue
            try:
                resp = self.call_service("weather/get_forecasts", entity_id=ent,
                                         type="hourly", return_response=True) or {}
                points = self._forecast_points(resp, ent)
            except Exception as exc:  # noqa: BLE001 — best-effort only
                points = []
                self.log(f"GridWarm: {ent} gave no hourly forecast ({exc!r}) — trying any other "
                         "weather entity; this one is retried in an hour.", level="WARNING")
            if points:
                used = ent
                break
            self._thermal_forecast_warned[ent] = now
        if used and used != self._thermal_weather_ok:
            self._thermal_weather_ok = used
            self.log(f"GridWarm: outdoor forecast from {used} ({len(points)} points)"
                     + (f" (configured {weather_entity} serves none)" if weather_entity and used != weather_entity else ""))
        elif not used and not self._thermal_weather_ok:
            self.log("GridWarm: no weather entity served an hourly forecast — outdoor temperature held flat",
                     level="WARNING")
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

    def _heating_rate_curve(self, slots, mode):
        """What the heat pump planners should price a kWh at, per 5-min
        step, in £/kWh (same shape as _thermal_rate_curve):
        - "tariff": the raw import rate, as if there were no battery.
        - "cop": flat (the battery's own energy cost), so the plan just
          minimises electricity -- maximises COP.
        - "battery": from the last solved battery plan (see
          core/effective_price.py): the battery's energy cost where it
          covers the load, the import rate where it runs flat or is
          charging, the export rate where it would otherwise export.
          Falls back to the tariff before the first solve."""
        raw = self._thermal_rate_curve(slots)
        if mode == "tariff":
            return raw
        plan = self._last_plan
        # Wear priced at a realistic per-kWh figure from the dashboard
        # setting -- NOT self.degradation, which is the battery LP's own
        # mode weighting (15p/kWh in eco): a preference about how hard to
        # cycle, not a cost. Using it here made battery energy look like
        # 22p and pushed all the heating into the overnight import slots.
        settings = self._ui_zone_cache[2] if self._ui_zone_cache else None
        wear_gbp = float((settings or {}).get("battery_wear_p_per_kwh", 3.0)) / 100.0
        batt_cost_p = core_effective_price.battery_energy_cost_p(
            plan["slots"] if plan else [], plan["cost_trace"] if plan else [],
            self.cheap_rate, getattr(self.cfg, "efficiency", 0.9), wear_gbp)
        if mode == "cop":
            return [batt_cost_p / 100.0] * len(raw)
        if not plan:
            # Before the first battery solve (the thermal step runs ahead
            # of it in a tick) there is nothing to read the battery's
            # behaviour off -- price at the tariff, never flat: a flat
            # price on the first tick after a restart committed a hot
            # water run at peak rate because "now" had the best COP.
            return raw
        eff_p = core_effective_price.effective_prices_p(plan["slots"], plan["cost_trace"], batt_cost_p)
        by_start = {plan["slots"][i]["start"]: eff_p[i] / 100.0 for i in range(len(eff_p))}
        step_ratio = SLOT_MIN // THERMAL_STEP_MIN
        curve = []
        for s in slots:
            v = by_start.get(s["start"])
            if v is None:
                v = s["imp"] if math.isfinite(s["imp"]) else self.default_import
            curve.extend([v] * step_ratio)
        if len(curve) < THERMAL_HORIZON_STEPS:
            curve.extend([curve[-1] if curve else batt_cost_p / 100.0] * (THERMAL_HORIZON_STEPS - len(curve)))
        return curve[:THERMAL_HORIZON_STEPS]

    def _thermal_pv_curve(self, slots):
        """Forecast PV output in kW per 5-min thermal step, from the
        battery plan's own per-slot PV kWh -- the house model turns a
        share of it into passive solar gain."""
        step_ratio = SLOT_MIN // THERMAL_STEP_MIN
        curve = []
        for s in slots:
            pv_kwh = s.get("pv", 0.0) or 0.0
            kw = pv_kwh / (SLOT_MIN / 60.0) if math.isfinite(pv_kwh) else 0.0
            curve.extend([kw] * step_ratio)
        if len(curve) < THERMAL_HORIZON_STEPS:
            curve.extend([0.0] * (THERMAL_HORIZON_STEPS - len(curve)))
        return curve[:THERMAL_HORIZON_STEPS]

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

    def _run_thermal_forecast(self, zone, now, rate_curve, heat_rate_curve=None, pricing_mode="tariff"):
        heat_rate_curve = heat_rate_curve if heat_rate_curve is not None else rate_curve
        internal_temp = self._thermal_internal_temp(zone)
        if internal_temp is None:
            return None
        target_temp = self._thermal_target_temp(zone)
        if target_temp is None:
            target_temp = internal_temp
        external_now = self._thermal_external_temp(zone)
        heating_on = self._thermal_heating_on(zone)
        hp = self._hp_live(zone)
        self._update_flow_learning(zone, now, internal_temp, external_now, heating_on, hp)
        if zone.get("dhw"):
            self._update_dhw_learning(zone, now, internal_temp, heating_on)
        self._update_thermal_learning(zone, now, internal_temp, external_now, heating_on)

        # A room, or a hot water zone (its COP per slot follows the outdoor
        # temperature), always gets the outdoor forecast -- auto-detected
        # when no weather entity is configured. A plain yaml tank zone
        # loses heat to a constant indoor ambient and has nothing to
        # look ahead at.
        wants_forecast = bool(zone["weather_entity"]) or not zone["is_tank"] or bool(zone.get("dhw"))
        external_curve = (self._thermal_weather_curve(
            zone["weather_entity"], now, THERMAL_HORIZON_STEPS, THERMAL_STEP_MIN, external_now)
            if wants_forecast else [external_now] * THERMAL_HORIZON_STEPS)

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

        live_cop = hp["live_cop"]
        dhw = None
        if zone.get("dhw"):
            try:
                dhw = self._plan_dhw(zone, now, internal_temp, target_temp, external_curve,
                                     heat_rate_curve, rate_curve, pricing_mode)
            except Exception as exc:  # noqa: BLE001 — a plan failure must not drop the zone
                self.log(f"GridWarm: hot water plan for '{zone['name']}' failed: {exc!r}", level="WARNING")
                dhw = None
        flow = None
        if zone["flow_entity"]:
            try:
                flow = self._plan_flow(zone, now, internal_temp, external_curve, plan_curve,
                                       heat_rate_curve, rate_curve, pricing_mode)
            except Exception as exc:  # noqa: BLE001 — a plan failure must not drop the zone
                self.log(f"GridWarm: flow plan for '{zone['name']}' failed: {exc!r}", level="WARNING")
                flow = None
            if flow and flow["plan_status"] != "empty":
                # The flow plan is the real forecast for this zone -- its
                # cost figures supersede the on/off simulation's.
                predicted_kwh_today = flow["plan_elec_kwh_today"]
                predicted_cost_today = flow["plan_cost_today"]
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
                "no_reading": False,
                # Set when the primary room sensor was unavailable and the
                # fallback (the heat pump's own controller sensor, say) is
                # being used instead -- the dashboard says so.
                "reading_from_fallback": zone.get("_reading_source"),
                "is_tank": zone["is_tank"],
                # Only the dashboard zone's target is GridLock's own to
                # change; a yaml zone's comes from its target entity.
                "target_editable": zone.get("source") == "dashboard" and not zone["target_temp_entity"],
                # The model's own first-step decision (already reflects
                # the anticipation-adjusted plan_curve and its hysteresis
                # band) -- this is what active control, if enabled for
                # this zone, actually commands. Distinct from the LIVE
                # "heating_on" above, which is what the hardware is
                # observed doing right now, not what the model wants.
                "desired_heating_on": (dhw["heat_now"] if dhw else
                                       flow["required_w"] >= (zone["params"].heat_min_power or 1500.0) * 0.5
                                       if flow else trace[0]["heating_on"]),
                "flow": flow,
                "dhw": dhw,
                "hp": hp,
                "learned_emitter_w_at_dt50": round(zone["emitter_rated_w_at_dt50"], 0),
                "config_emitter_w_at_dt50": round(zone["config_emitter_rated_w_at_dt50"], 0),
                "learned_cop_eta": round(zone["cop_eta"], 3),
                "config_cop_eta": round(zone["config_cop_eta"], 3),
                "learned_thermal_mass": round(zone["params"].thermal_mass_wh_per_c, 0),
                "config_thermal_mass": round(zone["config_thermal_mass_wh_per_c"], 0),
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

    def _thermal_heating_on(self, zone):
        """Is the zone's heat source running right now, per its
        heating_entity -- compared against heating_entity_state ("on" by
        default), case-insensitively, so a text sensor's "Heating" works
        the same as a switch's "on"."""
        if not zone["heating_entity"]:
            return False
        live = self.get_state(zone["heating_entity"])
        return (live is not None
                and str(live).strip().lower() == str(zone["heating_entity_state"]).strip().lower())

    def _ui_space_heating_zone(self):
        """The dashboard-configured space heating zone, from
        gridwarm_settings.json (written by the web UI -- see
        core/space_heating.py for the settings shape and the reference
        install's defaults). Re-read whenever the file changes, same
        no-restart pattern as _ev_schedule_settings(), but cached on
        mtime since building a zone and re-applying its learned
        parameters every tick is pointless churn. Returns (zone, settings)
        -- zone None when the user has hidden it ("configured: false")."""
        path = self._dashboard_path("gridwarm_settings.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        cached_mtime, cached_zones, cached_settings = self._ui_zone_cache
        if cached_settings is not None and mtime == cached_mtime:
            return cached_zones, cached_settings
        settings = core_space_heating.merge_settings(
            self._load_dashboard_json("gridwarm_settings.json", {}))
        zones = []
        cfgs = []
        if settings["configured"]:
            cfgs.append(("space heating", core_space_heating.to_zone_cfg(settings)))
        if settings["hot_water"]["configured"]:
            cfgs.append(("hot water", core_space_heating.to_hot_water_zone_cfg(settings["hot_water"])))
        for kind, cfg in cfgs:
            zone = self._build_thermal_zone(cfg)
            zone["source"] = "dashboard"
            # Same learned-parameter restore as the apps.yaml zones get at
            # startup -- a settings edit (or restart) mustn't reset what
            # the model has learned about the house back to the defaults.
            self._apply_learned_thermal(zone)
            if zone["control_entity"]:
                pause_helper = self._thermal_pause_helper(zone["name"])
                if not self.entity_exists(pause_helper):
                    # Virtual fallback, defaults to "not paused" -- the
                    # dashboard's Pause button writes it directly if no
                    # real helper exists, so this one is usable as-is.
                    self.set_state(pause_helper, state="on",
                                   attributes={"friendly_name": f"GridLock GridWarm control: {zone['name']}"})
                self._watch_control_entity(zone)
            self.log(f"GridWarm: dashboard {kind} zone '{zone['name']}' loaded "
                     f"(control_entity={zone['control_entity']})")
            zones.append(zone)
        self._ui_zone_cache = (mtime, zones, settings)
        return zones, settings

    def _watch_control_entity(self, zone):
        """React within seconds, not at the next 5-minute tick, when
        something other than GridWarm flips a controlled switch -- the
        heat pump's own built-in DHW timer most of all (there is no
        Modbus register for that timer, so it can't be deleted; this
        makes it irrelevant instead). Our own writes trigger this too,
        but then the state already matches and nothing happens."""
        entity = zone["control_entity"]
        if not entity or entity in self._control_listeners:
            return
        self._control_listeners[entity] = zone["name"]
        self.listen_state(self._on_control_entity_changed, entity)

    def _on_control_entity_changed(self, entity, attribute, old, new, kwargs):
        name = self._control_listeners.get(entity)
        state = self._thermal_control_state.get(name or "")
        if not state or state.get("last_commanded") is None or new not in ("on", "off"):
            return
        if core_thermal.command_matches_state(state["last_commanded"], new):
            return
        if self.get_state(self.ent_gridwarm_mode) != "active":
            return
        if self.get_state(self._thermal_pause_helper(name)) == "off":
            return
        wanted = state["last_commanded"]
        self.call_service(f"switch/turn_{'on' if wanted else 'off'}", target={"entity_id": entity})
        self._log_decision(f"GridWarm: {name} {'ON' if wanted else 'OFF'} re-asserted",
                           f"{entity} was switched {new} by something else (the unit's own timer, "
                           "or a hand) — put back to what the plan wants")

    def _thermal_stub_result(self, zone):
        """What a controlled zone publishes when it has no usable room
        reading (sensor unavailable, dead battery, wrong entity) -- it
        still has to appear on the dashboard with its control status,
        and _control_thermal_zone still has to run for it (on weather
        compensation alone, see there), rather than silently vanish
        along with whatever state its switch was last left in."""
        target = self._thermal_target_temp(zone)
        return {"name": zone["name"],
                "current_temp": None,
                "target_temp": round(target, 2) if target is not None else None,
                "external_temp": round(self._thermal_external_temp(zone), 2),
                "heating_on": self._thermal_heating_on(zone),
                "no_reading": True,
                "is_tank": zone["is_tank"],
                "target_editable": zone.get("source") == "dashboard" and not zone["target_temp_entity"],
                "desired_heating_on": True,
                "flow": None,
                "dhw": None,
                "hp": self._hp_live(zone),
                "learned_emitter_w_at_dt50": round(zone["emitter_rated_w_at_dt50"], 0),
                "config_emitter_w_at_dt50": round(zone["config_emitter_rated_w_at_dt50"], 0),
                "learned_cop_eta": round(zone["cop_eta"], 3),
                "config_cop_eta": round(zone["config_cop_eta"], 3),
                "learned_thermal_mass": round(zone["params"].thermal_mass_wh_per_c, 0),
                "config_thermal_mass": round(zone["config_thermal_mass_wh_per_c"], 0),
                "cop": zone["params"].heating_cop,
                "predicted_kwh_today": 0.0, "predicted_kwh_today_baseline": 0.0,
                "predicted_cost_today": 0.0, "predicted_cost_today_baseline": 0.0,
                "usable_hot_water_litres": None, "showers_available": None,
                "learned_heat_loss_degrees": round(zone["params"].heat_loss_degrees, 4),
                "config_heat_loss_degrees": round(zone["config_heat_loss_degrees"], 4),
                "learned_heat_loss_watts": round(zone["params"].heat_loss_watts, 1),
                "config_heat_loss_watts": round(zone["config_heat_loss_watts"], 1),
                "thermal_learning_observations": self.thermal_learning_state.get(
                    zone["name"], {}).get("observation_count", 0),
                "forecast_data": []}

    def _control_thermal_zone(self, zone, now, result):
        """The one place anything in GridLock writes to a heating device
        -- off by default, only runs at all if control_entity is set for
        this zone (a hot water tank's force-heat switch, or the heat
        pump's own space-heating power switch). Gated by the global
        READ-ONLY/ACTIVE master switch and a per-zone pause helper
        (auto-created, same pattern as input_boolean.gridlock_enable) so
        it can be stopped instantly from the UI. Layered on top of the
        plan's own decision, all biased toward heating rather than
        withholding it:
        - core.thermal.decide_heating_command's safety floor / max-off cap
        - control_requires: preconditions on the device itself. While any
          is unmet nothing is written -- the device would ignore it (on
          the Midea unit this was built against the switch reverts within
          seconds, no fault, no log line) -- and the zone shows Blocked
          with the reason, so the fix is a known click, not an afternoon
          of history digging.
        - a read-back check: if the device reverts a command it's re-sent
          every tick and flagged Reverted, so an overridden switch can't
          masquerade as "heating on".
        A zone with no usable room reading (a stub result) runs on plain
        weather compensation from the loss model -- never "just on"."""
        entity = zone["control_entity"]
        if not entity:
            result["control"] = None
            return
        name = zone["name"]
        pause_helper = self._thermal_pause_helper(name)
        state = self._thermal_control_state.setdefault(
            name, {"off_since": None, "last_commanded": None, "rejected": False,
                   "rejected_notified": False, "blocked_key": None})
        # The global master switch (defaults read_only) always wins over
        # a zone's own per-zone pause helper -- both have to say "go"
        # for anything to actually be written.
        read_only = self.get_state(self.ent_gridwarm_mode) != "active"
        paused = read_only or self.get_state(pause_helper) == "off"
        requirements = zone["control_requires"]
        live = {r["entity"]: self.get_state(r["entity"]) for r in requirements}
        unmet = core_thermal.unmet_requirements(requirements, live.get)
        blocked_items = [{"entity": r["entity"], "state": r["state"], "actual": live.get(r["entity"])}
                         for r in requirements if core_thermal.unmet_requirements([r], live.get)]
        live_state = self.get_state(entity)
        # Always published, even while paused/before the first real
        # decision -- the dashboard needs somewhere to show control
        # status regardless of whether a write is about to happen.
        result["control"] = {"entity": entity, "pause_helper": pause_helper,
                              "paused": paused, "commanded": state["last_commanded"],
                              "blocked": unmet, "blocked_items": blocked_items,
                              "rejected": state["rejected"], "live_state": live_state,
                              "flow_entity": zone["flow_entity"],
                              "flow_setpoint": state.get("flow_setpoint"),
                              # A plain word -- False booleans don't survive the
                              # trip through HA's attribute store, so "off" and
                              # "never decided" looked identical on the dashboard.
                              "status": ("paused" if paused else "blocked" if unmet
                                         else "rejected" if state["rejected"]
                                         else "heating" if state["last_commanded"]
                                         else "off" if state["last_commanded"] is False
                                         else "pending")}
        # A hot water setpoint the plan wants (legionella cycle up, then
        # back). Raising it needs control; restoring it happens even when
        # paused -- leaving a 60 °C setpoint behind is never intended.
        sw = (result.get("dhw") or {}).get("setpoint_wanted")
        tgt_ent = zone.get("target_temp_entity")
        if sw is not None and tgt_ent:
            live_sp = self.get_float_state(tgt_ent, None)
            raising = live_sp is None or sw > live_sp + 0.4
            if (live_sp is None or abs(live_sp - sw) > 0.4) and not (paused and raising):
                self.call_service("number/set_value", target={"entity_id": tgt_ent}, value=float(sw))
                self._log_decision(f"GridWarm: {name} setpoint {sw:.0f}°C",
                                   "Legionella cycle" if raising else "Legionella cycle over — setpoint restored")
        if paused:
            state["blocked_key"] = None
            return
        if unmet:
            key = tuple(unmet)
            if state["blocked_key"] != key:
                state["blocked_key"] = key
                self._log_decision(f"GridWarm: {name} BLOCKED",
                                   "Not commanding — the device would ignore it until this is fixed: "
                                   + "; ".join(unmet))
                self._notify(f"GridLock: GridWarm can't control {name}",
                             "A setting on the heat pump itself is overriding its heating switch, so "
                             f"GridWarm isn't commanding it: {'; '.join(unmet)}. Fix it from the GridWarm "
                             "tab (Blocked → Fix) or on the unit's own controller.")
            return
        state["blocked_key"] = None
        no_reading = result.get("no_reading", False)
        off_duration_hours = ((now - state["off_since"]).total_seconds() / 3600.0
                              if state["off_since"] else 0.0)
        flow = result.get("flow")
        flow_entity = zone["flow_entity"]
        setpoint = None
        flow_reason = None
        if no_reading:
            # No room reading to regulate against. Holding heating ON
            # "to be safe" is NOT safe -- it ran a real house flat out at
            # 45°C flow for fourteen hours before anyone noticed. Instead,
            # fall back to plain weather compensation computed here: the
            # heat the house would need at its target in this outdoor
            # temperature, from the learned loss model -- enough to stop
            # it going cold, switched off in mild weather, flow capped.
            # The fallback cap is deliberately low; a thermometer is the
            # real fix and the dashboard says so.
            params = zone["params"]
            target = result["target_temp"] if result.get("target_temp") is not None else 20.0
            need_w = max(0.0, core_flow.steady_state_loss_w(target, result["external_temp"], params))
            currently_on = (state["last_commanded"] if state["last_commanded"] is not None
                            else live_state == "on")
            min_w = params.heat_min_power or 1500.0
            command = need_w > min_w * 0.2 if currently_on else need_w >= min_w * 0.5
            if flow_entity:
                setpoint = min(zone["flow_fallback"], core_flow.flow_setpoint_for_power(
                    need_w, target, zone["emitter_rated_w_at_dt50"], zone["water_dt"],
                    zone["flow_min"], zone["flow_max"], 1.0))
            flow_reason = (f"no room reading — weather compensation only: ~{need_w / 1000.0:.1f} kW to hold "
                           f"{target:.1f}°C at {result['external_temp']:.1f}°C outside")
        elif flow_entity and flow:
            # Dynamic flow temperature: the plan's first slot says how much
            # heat the house needs; decide_flow_command turns that into
            # on/off with demand hysteresis and a rate-limited setpoint.
            currently_on = (state["last_commanded"] if state["last_commanded"] is not None
                            else live_state == "on")
            command, setpoint = core_flow.decide_flow_command(
                flow["required_w"], currently_on, zone["params"].heat_min_power or 1500.0,
                state.get("flow_setpoint"), flow["flow_setpoint_wanted"], zone["flow_max_step"])
            # Same two safety overrides as on/off control, layered on top.
            forced = core_thermal.decide_heating_command(
                command, result["current_temp"], zone["control_safety_min_temp"],
                off_duration_hours, zone["control_max_off_hours"])
            if forced and not command:
                command = True
                setpoint = max(setpoint, flow["flow_setpoint_wanted"],
                               (zone["flow_min"] + zone["flow_max"]) / 2.0)
            flow_reason = (f"plan needs {flow['required_kw']:.1f} kW at {result['current_temp']:.1f}°C "
                           f"in / {result['external_temp']:.1f}°C out — predicted COP {flow['predicted_cop']:.2f}")
        else:
            command = core_thermal.decide_heating_command(
                result["desired_heating_on"], result["current_temp"],
                zone["control_safety_min_temp"], off_duration_hours, zone["control_max_off_hours"])
        # Power cut: the battery is all there is. Hot water waits; space
        # heating only runs if the house is getting dangerously cold, then
        # at the gentlest flow until it's back above the stop temperature.
        try:
            off_grid = bool(self.grid_connection_off())
        except Exception:  # noqa: BLE001 — unknown grid state is treated as on-grid
            off_grid = False
        if off_grid:
            if zone.get("dhw"):
                command = False
                flow_reason = "Power cut — hot water suspended to save the battery"
            elif result.get("current_temp") is not None:
                t = result["current_temp"]
                em = state.get("emergency", False)
                if t <= zone["emergency_temp"] or (em and t < zone["emergency_stop_temp"]):
                    command, em = True, True
                    if flow_entity:
                        setpoint = zone["emergency_flow"]
                    flow_reason = (f"Power cut — house at {t:.1f}°C, emergency heat at "
                                   f"{zone['emergency_flow']:.0f}°C flow until {zone['emergency_stop_temp']:.0f}°C")
                else:
                    command, em = False, False
                    flow_reason = "Power cut — heating held off to save the battery"
                state["emergency"] = em
        else:
            state["emergency"] = False
        # Anti short-cycling: hold the last command for its minimum time,
        # unless the safety floor is hit or the grid is down.
        if not off_grid and not no_reading and state["last_commanded"] is not None \
                and command != state["last_commanded"]:
            try:
                since_min = (now - self._iso(state["switched_at"])).total_seconds() / 60.0
            except (ValueError, TypeError, KeyError):
                since_min = 1e9
            floor_hit = (result.get("current_temp") is not None
                         and result["current_temp"] <= zone["control_safety_min_temp"])
            if not floor_hit:
                if state["last_commanded"] and since_min < zone["min_on_minutes"]:
                    command = True
                    setpoint = state.get("flow_setpoint") if flow_entity else setpoint
                elif not state["last_commanded"] and since_min < zone["min_off_minutes"]:
                    command = False
        result["control"]["off_grid"] = "yes" if off_grid else "no"
        state["off_since"] = None if command else (state["off_since"] or now)
        result["control"]["commanded"] = command
        result["control"]["status"] = "heating" if command else "off"
        # Flow setpoint first, then the on/off switch -- so the unit starts
        # at the temperature the plan wants, not at whatever was left.
        if flow_entity and setpoint is not None and command:
            setpoint = float(round(setpoint))
            if state.get("flow_setpoint") != setpoint:
                self.call_service("number/set_value", target={"entity_id": flow_entity}, value=setpoint)
                state["flow_setpoint"] = setpoint
                self._log_decision(f"GridWarm: {name} flow {setpoint:.0f}°C", flow_reason or "flow setpoint")
        result["control"]["flow_entity"] = flow_entity
        result["control"]["flow_setpoint"] = state.get("flow_setpoint")
        service = f"switch/turn_{'on' if command else 'off'}"
        if command == state["last_commanded"]:
            if core_thermal.command_matches_state(command, live_state):
                state["rejected"] = False
                result["control"]["rejected"] = False
                return
            # Sent last tick (or earlier), and the device now reads the
            # opposite -- it reverted us. Re-send, flag it, say so once.
            state["rejected"] = True
            result["control"]["rejected"] = True
            self.call_service(service, target={"entity_id": entity})
            self._log_decision(f"GridWarm: {name} {'ON' if command else 'OFF'} re-sent",
                               "The device reverted the last command — something on the unit itself is "
                               "overriding its switch (check its room-thermostat / weather-compensation "
                               "settings, see DOCS.md)")
            if not state["rejected_notified"]:
                state["rejected_notified"] = True
                self._notify(f"GridLock: {name} heating command reverted by the device",
                             f"GridWarm switched {entity} {'on' if command else 'off'} but the unit set it "
                             "back. It's being re-sent each tick. A setting on the unit is overriding its "
                             "switch — add it to this zone's requirements on the GridWarm tab once found, "
                             "so it's checked up front next time.")
            return
        state["last_commanded"] = command
        state["switched_at"] = now.isoformat()
        state["rejected"] = False
        state["rejected_notified"] = False
        result["control"]["rejected"] = False
        # target={"entity_id": ...}, not the plain entity_id= kwarg --
        # matches the one other place in this codebase that actually
        # commands hardware (core/inverter.py's SigenergyAdapter).
        self.call_service(service, target={"entity_id": entity})
        # decide_heating_command only ever forces ON, never OFF -- so "off"
        # can only mean the plan itself wanted off, no override needed.
        if off_grid:
            reason = flow_reason or "Power cut"
        elif no_reading:
            reason = (flow_reason or "No usable room temperature reading") + (
                " — heating on" if command else " — not enough demand, heating off")
        elif not command:
            reason = "GridWarm plan — heating off"
        elif result["current_temp"] <= zone["control_safety_min_temp"]:
            reason = "Safety floor reached — heating forced on regardless of plan"
        elif zone["control_max_off_hours"] > 0 and off_duration_hours >= zone["control_max_off_hours"]:
            reason = ("Held off long enough to hit the safety cap — heating on to give the "
                      "heat pump's own cycles a chance to run")
        else:
            reason = "GridWarm plan — heating on"
        self._log_decision(f"GridWarm: {name} {'ON' if command else 'OFF'}", reason)

    def _publish_thermal_forecast(self, now):
        ui_zones, ui_settings = self._ui_space_heating_zone()
        # A yaml zone reading the same sensor as a dashboard zone is the
        # same thing configured twice -- the dashboard one wins, so the
        # old apps.yaml tank entry doesn't show up as a second row.
        ui_sensors = {z["internal_temp_entity"] for z in ui_zones}
        zones = ([z for z in self.thermal_zones if z["internal_temp_entity"] not in ui_sensors]
                 + ui_zones)
        if not zones:
            # Still publish the settings so the dashboard's space heating
            # form has something to show (a hidden zone can be un-hidden).
            self.set_state("sensor.gridlock_gridwarm", state="unknown",
                           attributes={"friendly_name": "GridLock GridWarm",
                                       "icon": "mdi:heat-pump",
                                       "zones": [],
                                       "control_mode": self.get_state(self.ent_gridwarm_mode) or "read_only",
                                       "space_heating_settings": ui_settings,
                                       "hot_water_settings": ui_settings["hot_water"]})
            return
        slots = self.build_slots(now)
        rate_curve = self._thermal_rate_curve(slots)
        self._thermal_pv_kw = self._thermal_pv_curve(slots)
        pricing_mode = ui_settings.get("heat_pricing", "battery")
        heat_rate_curve = self._heating_rate_curve(slots, pricing_mode)
        zones_out = []
        for zone in zones:
            try:
                result = self._run_thermal_forecast(zone, now, rate_curve, heat_rate_curve, pricing_mode)
            except Exception as exc:  # noqa: BLE001 — one bad zone shouldn't drop the rest
                self.log(f"Thermal forecast for zone '{zone['name']}' failed: {exc!r}",
                         level="WARNING")
                result = None
            if result is None and zone["control_entity"]:
                # A controlled zone must never just disappear -- its
                # switch is still out there in whatever state it was
                # last left; see _thermal_stub_result.
                result = self._thermal_stub_result(zone)
            if result:
                zones_out.append(result)
                try:
                    self._control_thermal_zone(zone, now, result)
                except Exception as exc:  # noqa: BLE001 — never let a control write crash the tick
                    self.log(f"Thermal control for zone '{zone['name']}' failed: {exc!r}",
                             level="ERROR")
        if not zones_out:
            return
        self._publish_heatpump_load(zones, zones_out, now)
        total_cost = sum(z["predicted_cost_today"] for z in zones_out)
        baseline_cost = sum(z["predicted_cost_today_baseline"] for z in zones_out)
        saving = baseline_cost - total_cost
        plan_summary = (
            f"Anticipating temperature swings ahead — easing off before forecast warm-ups, "
            f"adding a little heat ahead of cold snaps — predicted £{total_cost:.2f} today "
            f"across {len(zones_out)} zone{'s' if len(zones_out) != 1 else ''} vs £{baseline_cost:.2f} "
            f"reacting only to the current temperature "
            f"({'saving' if saving >= 0 else 'costing'} £{abs(saving):.2f}).")
        with_reading = [z for z in zones_out if z["current_temp"] is not None]
        self.set_state("sensor.gridlock_gridwarm",
                       state=f"{with_reading[0]['current_temp']:.1f}" if with_reading else "unknown",
                       attributes={"friendly_name": "GridLock GridWarm",
                                   "unit_of_measurement": "°C",
                                   "icon": "mdi:heat-pump",
                                   "plan_summary": plan_summary,
                                   "predicted_cost_today_total": round(total_cost, 2),
                                   "predicted_cost_today_baseline_total": round(baseline_cost, 2),
                                   "control_mode": self.get_state(self.ent_gridwarm_mode) or "read_only",
                                   "space_heating_settings": ui_settings,
                                   "hot_water_settings": ui_settings["hot_water"],
                                   "zones": zones_out})

    def _heatpump_slot_kwh(self, slot_start):
        """GridWarm's planned heat pump electricity for one plan slot --
        the figure the load forecast is already carrying (planned where
        there is a plan, else that circuit's learned profile)."""
        try:
            planned = self.load_provider.planned_circuit_kwh.get(HEATPUMP_POWER_ENTITY) or {}
            v = planned.get(self.load_provider.slot_key(slot_start))
            if v is not None:
                return float(v)
            if HEATPUMP_POWER_ENTITY in (self.load_provider.circuit_power_entities or []):
                slot_idx = str(slot_start.hour * 2 + (1 if slot_start.minute >= 30 else 0))
                return float(self.load_provider.circuit_profiles.get(HEATPUMP_POWER_ENTITY, {}).get(slot_idx, 0.0))
        except Exception:  # noqa: BLE001 — a display figure, never worth failing the plan over
            pass
        return 0.0

    def _publish_heatpump_load(self, zones, zones_out, now):
        """Tell the battery plan what the heat pump will draw (see
        HEATPUMP_POWER_ENTITY): the space heating plan's electricity per
        slot plus the hot water run's, keyed by slot; and the live
        estimate as a power sensor for the load learning to subtract."""
        planned = {}
        live_w = 0.0
        by_name = {z["name"]: z for z in zones}
        for r in zones_out:
            zone = by_name.get(r["name"])
            if zone is None:
                continue
            flow = r.get("flow")
            if flow and flow.get("plan"):
                for p in flow["plan"]:
                    try:
                        key = self.load_provider.slot_key(self._iso(p["x"]))
                    except (ValueError, TypeError):
                        continue
                    planned[key] = planned.get(key, 0.0) + p["heat_kwh"] / max(p["cop"], 0.5)
            dhw = r.get("dhw")
            if dhw and dhw.get("window_start") and dhw.get("window_end"):
                try:
                    start, end = self._iso(dhw["window_start"]), self._iso(dhw["window_end"])
                except (ValueError, TypeError):
                    start = end = None
                if start and end and end > start:
                    tank = r.get("current_temp")
                    setpoint = dhw.get("setpoint") or 53.0
                    hyst = dhw.get("start_hysteresis") or 2.0
                    from_temp = tank if tank is not None else setpoint - hyst
                    heat_kwh = max(0.0, setpoint - from_temp) * zone["params"].thermal_mass_wh_per_c / 1000.0
                    if dhw.get("will_heat") or dhw.get("committed"):
                        elec = heat_kwh / max(dhw.get("window_cop") or zone["params"].heating_cop, 0.5)
                        n_slots = max(1, int((end - start).total_seconds() // 1800))
                        for i in range(n_slots):
                            key = self.load_provider.slot_key(start + timedelta(minutes=SLOT_MIN * i))
                            planned[key] = planned.get(key, 0.0) + elec / n_slots
            hp = r.get("hp") or {}
            if r.get("heating_on") and hp.get("thermal_kw"):
                flow_t = hp.get("flow_temp") if hp.get("flow_temp") is not None else 45.0
                ext = r.get("external_temp") if r.get("external_temp") is not None else 10.0
                cop = core_flow.cop_estimate(flow_t, ext, zone.get("cop_eta", 0.45))
                live_w += hp["thermal_kw"] * 1000.0 / max(cop, 0.5)
        self.load_provider.planned_circuit_kwh[HEATPUMP_POWER_ENTITY] = planned
        self.set_state(HEATPUMP_POWER_ENTITY, state=f"{live_w:.0f}",
                       attributes={"friendly_name": "GridLock heat pump power (estimated)",
                                   "unit_of_measurement": "W", "device_class": "power",
                                   "state_class": "measurement", "icon": "mdi:heat-pump",
                                   "planned_kwh_next_24h": round(sum(
                                       v for k, v in planned.items()
                                       if 0 <= k - self.load_provider.slot_key(now) < 48), 2)})

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
        # GridWarm's own writes (a zone's on/off switch, its flow
        # setpoint) go through this same event bus and used to be
        # reported as "external" -- a notification for every command
        # GridLock itself sent. Anything GridWarm controls is excluded;
        # a genuinely external flip of those is caught by the read-back
        # check and the live listener instead, and logged there.
        own = set()
        for zone in list(self.thermal_zones) + list((self._ui_zone_cache[1] or []) if self._ui_zone_cache else []):
            own.update(e for e in (zone.get("control_entity"), zone.get("flow_entity")) if e)
        watched = [e for e in watched if e not in own]
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
        # Logged on the GridWarm tab regardless; a push/persistent
        # notification only if asked for (off by default -- it got noisy).
        settings = self._ui_zone_cache[2] if self._ui_zone_cache else None
        if settings and settings.get("notify_external_commands"):
            self._notify(
                "GridLock: external command on a watched heat pump entity",
                f"{', '.join(watched)} — {data.get('domain')}.{data.get('service')}"
                f"{f' (value: {value})' if value is not None else ''}. "
                "Not from GridLock.")

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
        """Surfaces the SAME reserve-sufficiency numbers storm_decision()
        itself weighs (see _tick_inner) -- without this, "why is it
        still charging at 90%?" has no answer short of reading the code:
        Storm Watch charges toward storm_target_soc regardless of
        current SoC UNLESS the estimated reserve already covers the
        estimated outage, and that comparison isn't visible anywhere
        else. Recomputed independently here (cheap, pure) rather than
        threaded through from _tick_inner's own call, so this stays
        correct regardless of when/how often it's called."""
        reason = self.storm_active()
        outage_hours = expected_load_kwh = usable_kwh = reserve_sufficient = None
        if reason:
            now = self.get_now()
            outage_hours = self._estimated_outage_hours(now)
            expected_load_kwh = self._expected_load_kwh(now, outage_hours)
            soc0 = self.get_float_state(self.ent_soc, 0.0)
            usable_kwh = max(0.0, soc0 - self.floor_soc) / 100.0 * self.battery_kwh
            reserve_sufficient = usable_kwh >= expected_load_kwh
        self.set_state("sensor.gridlock_storm_status",
                       state="Active" if reason else "Clear",
                       attributes={"friendly_name": "GridLock Storm Watch",
                                   "icon": ("mdi:weather-lightning" if reason
                                            else "mdi:weather-partly-cloudy"),
                                   "reason": reason or "No active alerts",
                                   "estimated_outage_hours": round(outage_hours, 1)
                                                              if outage_hours is not None else None,
                                   "expected_load_kwh": round(expected_load_kwh, 2)
                                                        if expected_load_kwh is not None else None,
                                   "usable_reserve_kwh": round(usable_kwh, 2)
                                                          if usable_kwh is not None else None,
                                   "reserve_sufficient": reserve_sufficient})

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
        # Confirmed live: the EV power entity can end up carrying the
        # gridlock_power label too (easy mistake — it's a completely
        # ordinary-looking power sensor from the label picker's point of
        # view), which silently reinflates the load forecast by learning
        # its own charging pattern as a "circuit" ON TOP of the
        # ev_entity subtraction already excluding it from house load —
        # exactly the double-count core/forecast.py's own docstring says
        # this design is meant to prevent. That's an HA-side labelling
        # mistake this code can't fix at the source (no registry/label
        # API available here), but it can refuse to act on it — the
        # entity already wired up as ev_power_entity is never a valid
        # circuit no matter what it's labelled, so it's dropped here
        # unconditionally rather than trusting the label.
        if self.ent_ev_power:
            all_circuits = [c for c in all_circuits if c != self.ent_ev_power]
        if self.entity_exists(HEATPUMP_POWER_ENTITY) and HEATPUMP_POWER_ENTITY not in all_circuits:
            all_circuits.append(HEATPUMP_POWER_ENTITY)
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
        self._last_plan = {"slots": slots, "cost_trace": cost_trace, "ts": now.isoformat()}
        self._track_plan_accuracy(now, grid_cost, slots, soc0)
        self._check_solar_deficit(now, slots, cost_trace)

        cur = slots[0]
        action = core_optimizer.action(cur)
        ev_active = self._ev_is_charging()
        self._manage_ev_charge_assist()
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

        # --- Saving session: force export, but only when it's actually
        #     worth it. Joining a session is always free (no downside
        #     to being enrolled, so every one gets joined unconditionally
        #     in check_and_join_sessions) -- but forcing an export is
        #     real battery throughput, priced the same as any other
        #     export-side cycling. A low-reward session isn't worth
        #     draining the battery for. ---
        session_worth_it = (bool(session) and core_optimizer.session_export_worth_it(
            session.get("octopoints_per_kwh") if session else None,
            self.octopoint_value_gbp, self.export_degradation))
        if session and not session_worth_it and not self._prev_session_not_worth_it:
            self._log_decision(
                "Saving Session — Not Worth It",
                f"Session {session.get('code', '')} reward ({session.get('octopoints_per_kwh', '?')} "
                "pts/kWh) is worth less than the export-side degradation cost — running the "
                "normal plan instead of force-exporting")
        self._prev_session_not_worth_it = bool(session) and not session_worth_it
        if session_worth_it and soc0 > self.floor_soc + 5:
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
                                   "engine_version": VERSION,
                                   "action": reason, "reason": reason,
                                   "plan_html": plan_html,
                                   "soc_entity": self.ent_soc,
                                   "import_rate_entity": self.ent_import_rate,
                                   "export_rate_entity": self.ent_export_rate,
                                   "ev_entity": self.ent_ev,
                                   "dispatch_entity": self.ent_dispatch,
                                   "supplier_domain": self.supplier_domain,
                                   "edf_power_perks_events_entity": self.ent_edf_power_perks_events,
                                   "edf_power_perks_registered_entity":
                                       self.ent_edf_power_perks_registered,
                                   "edf_flextras_registered_entity":
                                       self.ent_edf_flextras_registered,
                                   "edf_sunday_saver_start_entity":
                                       self.ent_edf_sunday_saver_start,
                                   "edf_sunday_saver_end_entity":
                                       self.ent_edf_sunday_saver_end,
                                   "edf_flextras_bonus_hours_entity":
                                       self.ent_edf_flextras_bonus_hours,
                                   "ev_vehicle_stems": self.ev_vehicle_stems or None,
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

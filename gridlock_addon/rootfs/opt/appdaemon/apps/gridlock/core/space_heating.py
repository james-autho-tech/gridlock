"""Dashboard-configured space heating zone for GridWarm — the settings
shape, its defaults, and the merge/validation between what the web UI
saved and what the engine runs with.

Everything about this zone is set from the GridWarm tab (saved to
gridwarm_settings.json next to the other dashboard-owned settings, re-read
by the engine on change — no apps.yaml edit, no add-on restart). apps.yaml
zones still work exactly as before; this is the zone for people who'd
rather never open that file.

DEFAULTS describe the reference install this was built and tested
against — a Midea air-to-water heat pump on the Mosibi "Heatpump
Controller" ESPHome integration, one radiator loop, one representative
room thermometer — so the zone appears pre-filled and working on that
hardware, and as a worked example of which entity does what on any other.
Every field is overridable from the dashboard, and "configured: false"
hides the zone entirely.
"""

import copy

DEFAULTS = {
    # Show the zone at all. Off hides it (nothing predicted, nothing
    # commanded) without losing the rest of the settings.
    "configured": True,
    "name": "House",
    # The one room thermometer the model treats as "the house". The heat
    # pump's own wired controller has a thermometer in it: with the
    # unit's "supports room temperature sensor Ta" installer flag on it
    # reports it here at 1 °C resolution -- coarse, but always there,
    # which a battery/Thread thermostat is not. A climate.* entity's
    # current_temperature is read automatically if one is chosen instead.
    "internal_temp_entity": "sensor.loft_heatpump_controller_room_temperature_ta",
    # Used whenever the room sensor above is unavailable.
    "internal_temp_fallback_entity": "climate.bedroom_4_qing_thermo",
    # GridLock owns the target itself — there's deliberately no
    # thermostat in the loop. Set from the GridWarm tab's ✏️ or −/+.
    "target_temp": 15.0,
    # The heat pump measures the outdoor temperature itself, at the
    # outdoor unit -- always present, no cloud. The forecast ahead comes
    # from whichever weather.* entity actually serves HA's get_forecasts
    # (auto-detected when blank; the OpenWeatherMap free tier's doesn't,
    # which silently left the model with a flat outdoor curve).
    "external_temp_entity": "sensor.loft_heatpump_controller_outdoor_ambient_temperature",
    "weather_entity": None,
    # The heat pump's own space-heating power switch (zone 1). Commanded
    # only while the GridWarm master switch is ACTIVE and the zone isn't
    # paused — same gates as tank control.
    "control_entity": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1",
    # "Is it heating right now?" — the controller's own active-state text
    # sensor, which (unlike the compressor flag) isn't also on during a
    # hot water run.
    "heating_entity": "sensor.loft_heatpump_controller_active_state",
    "heating_entity_state": "Heating",
    "cop_entity": "sensor.loft_heatpump_controller_coefficient_of_performance",
    # Frost/comfort floor — heat regardless of plan below this.
    "control_safety_min_temp": 16.0,
    # No continuous-off cap for a room (a mild day legitimately needs no
    # heat at all); the floor above is the safety net. A tank's 6h cap
    # exists for anti-legionella cycles, which a room doesn't have.
    "control_max_off_hours": 0.0,
    # Settings on the unit itself that silently override its power
    # switch — checked before every write; while any is unmet the zone
    # shows Blocked with the reason instead of commanding into a wall.
    # Both of the Midea ones below were found on real hardware: the
    # first makes the unit revert the zone 1 switch within seconds, the
    # second leaves it in "Heating / Idle" for ever (its curve asked for
    # a 25 °C flow, under the unit's own 35 °C radiator minimum).
    "control_requires": [
        {"entity": "switch.loft_heatpump_controller_parameter_setting_1_supports_room_thermostat",
         "state": "off"},
        {"entity": "switch.loft_heatpump_controller_weather_compensation_zone_1",
         "state": "off"},
        {"entity": "select.loft_heatpump_controller_operational_mode",
         "state": "Heat"},
    ],
    # Thermal model — same meanings as the apps.yaml zone keys (see
    # core/thermal.py). Starting points; heat_loss_* then self-refine.
    "heat_loss_degrees": 0.035,
    "heat_loss_watts": 170.0,
    "heat_gain_static": 300.0,
    "heat_max_power": 7000.0,
    "heat_min_power": 2000.0,
    "heating_cop": 4.15,
    # Effective thermal mass of the whole house (fabric, not just air) --
    # a typical UK house is several kWh per °C. Learned from real heating
    # runs (measured heat in vs. temperature rise), so this is only where
    # it starts. Together with heat_loss_degrees this gives a heat-loss
    # coefficient of 0.035 * 8000 = 280 W/K, about 4 kW at 15 °C of lift.
    "thermal_mass_wh_per_c": 8000.0,
    "hysteresis": 0.5,
    "hysteresis_off": 0.1,
    # Sunshine through the windows, as a share of the PV forecast: watts
    # of passive room gain per kW of forecast PV output. A 4 kW array in
    # full sun means roughly a kilowatt into the rooms for most houses.
    # Starting point -- compare predicted vs actual on sunny days.
    "solar_gain_w_per_kw_pv": 250.0,
    # --- Dynamic flow temperature (core/flow_control.py) ---
    # The heat pump's space-heating flow setpoint. With this set GridWarm
    # modulates the flow temperature every tick to the lowest that meets
    # the plan, instead of just switching heating on and off at whatever
    # the unit's fixed setpoint is -- that's where the COP comes from.
    "flow_entity": "number.loft_heatpump_controller_set_water_temperature_t1s_zone_1",
    # With the unit's zone 1 emission type set to "Underfloor Heating"
    # its T1S range is 25-55 °C (on "Radiator" it's 35-65 and anything
    # lower is snapped back). 25 lets the plan run genuinely low on
    # mild days; 50 is plenty for radiators at the other end.
    "flow_min": 25.0,
    "flow_max": 50.0,
    "flow_max_step": 3.0,      # °C per 5-min tick, rate limit on setpoint moves
    "flow_fallback": 38.0,     # flow CAP while there's no room reading (weather compensation only)
    "water_dt": 5.0,           # design flow/return split, °C
    # Radiator output at ΔT50 (what datasheets quote), total for the
    # loop. Learned from the unit's measured thermal output while
    # running -- this is only the starting point.
    "emitter_rated_w_at_dt50": 10000.0,
    "cop_eta": 0.45,           # fraction of Carnot the unit achieves; learned from the live COP
    # How far the tariff-aware plan may let the room drift either side
    # of the target to shift heat into cheaper slots.
    "comfort_below": 1.0,
    "comfort_above": 1.0,
    "tariff_optimise": True,
    # What a kWh of electricity for heating costs in each slot:
    #   "battery" -- GridLock's own battery plan decides: the cheap rate
    #              the battery was charged at (plus losses and wear) in
    #              slots the battery covers, the import rate where it
    #              would be empty, the export rate where it would
    #              otherwise be exporting. With a battery sized to carry
    #              the heating this is flat most of the time, so the plan
    #              simply minimises electricity -- i.e. maximises COP.
    #   "cop"     -- ignore prices entirely, minimise electricity.
    #   "tariff"  -- the raw import rate, as if there were no battery.
    "heat_pricing": "battery",
    # What a kWh through the battery costs in wear, p/kWh, for the
    # battery-plan pricing above. A £6k, 36 kWh pack good for ~6000
    # cycles is under 3p; this is a real cost, unlike the battery LP's
    # own "degradation" weighting (which is a preference about cycling).
    "battery_wear_p_per_kwh": 3.0,
    # COP over comfort: inside the comfort band the plan drifts freely to
    # catch the warmest hours and the lowest flow; the band edges are
    # what make sure the house is actually heated.
    "comfort_priority": "cop",
    # A persistent/push notification for every external service call on
    # a watched heat pump entity. Off: still listed on the GridWarm tab.
    "notify_external_commands": False,
    # Live readings for learning the emitter curve, COP factor and
    # thermal mass (all optional -- without them the starting values
    # above just stay put).
    "thermal_power_entity": "sensor.loft_heatpump_controller_ability_of_hydraulic_module",
    "flow_temp_entity": "sensor.loft_heatpump_controller_total_water_outlet_temperature_t1",
    "return_temp_entity": "sensor.loft_heatpump_controller_water_inlet_temperature",
    "compressor_entity": "sensor.loft_heatpump_controller_compressor_operating_frequency",
}

# --- Hot water (DHW) zone, same dashboard-configured arrangement ---
# The unit's own DHW logic is left intact; GridWarm just decides when the
# DHW circuit gets power (core/dhw_plan.py): the cheapest window of the
# day by price / predicted COP, a floor that forces a run, and a maximum
# interval so it always runs at least once a day.
HOT_WATER_DEFAULTS = {
    "configured": True,
    "name": "Hot water",
    "tank_temp_entity": "sensor.loft_heatpump_controller_water_tank_temperature_t5",
    # The "standard" the tank is heated to -- the unit's own DHW setpoint
    # is read live, so changing it on the unit changes it here.
    "target_temp_entity": "number.loft_heatpump_controller_set_dhw_tank_temperature_t5s",
    # The unit only starts a run once the tank is this far below the
    # setpoint (Midea dT5_ON) -- read live so "will it actually heat?"
    # on the panel is right.
    "start_hysteresis_entity": "number.loft_heatpump_controller_dt5_on",
    # Power to the DHW circuit -- the same switch the unit's own timer
    # drives. NOT the "forced tank heating" one, which fires the
    # immersion heater on this unit.
    "control_entity": "switch.loft_heatpump_controller_power_dhw_t5s",
    "heating_entity": "sensor.loft_heatpump_controller_active_state",
    "heating_entity_state": "DHW",
    "cop_entity": "sensor.loft_heatpump_controller_coefficient_of_performance",
    "external_temp_entity": "sensor.loft_heatpump_controller_outdoor_ambient_temperature",
    "weather_entity": None,            # auto: any weather entity that serves forecasts
    "control_safety_min_temp": 20.0,   # effectively off -- the ready-by times and daily guarantee do the work
    # A needed run (tank below the unit's start point) must have finished
    # before the next of these local times: hot tank for the morning,
    # enough for showers by the evening. Blank list = daily guarantee only.
    "ready_by_times": ["06:30", "18:30"],
    "run_minutes": 60.0,               # length of the window the circuit is powered for
    # Legionella: once every interval, heat the tank to legionella_temp
    # and hold it there for legionella_hold_minutes, in the cheapest
    # window the normal plan would pick -- by raising the unit's own DHW
    # setpoint for the cycle and putting it back after. A cycle that
    # can't finish within legionella_max_hours is abandoned (setpoint
    # restored) and retried at the next window.
    "legionella_enabled": True,
    "legionella_temp": 60.0,
    "legionella_hold_minutes": 60.0,
    "legionella_interval_days": 7.0,
    "legionella_window_minutes": 180.0,
    "legionella_max_hours": 5.0,
    # The immersion heater's output, to report whether a cycle needed it.
    "immersion_entity": "binary_sensor.loft_heatpump_controller_load_output_electric_heater_tbh",
    "max_interval_hours": 24.0,        # never longer than this between windows
    "flow_margin": 5.0,                # DHW flow runs about this far above the tank setpoint (for the COP estimate)
    "cop_eta": 0.45,
    "control_requires": [
        {"entity": "switch.loft_heatpump_controller_parameter_setting_1_enable_water_heating",
         "state": "on"},
    ],
    # Tank model (prediction only -- the plan above doesn't need it).
    "tank_litres": 150.0,
    "heat_loss_watts": 60.0,
    "heat_max_power": 3500.0,
    "heat_min_power": 1500.0,
    "heating_cop": 2.5,
    "hysteresis": 2.0,
    "hysteresis_off": 0.5,
}

HOT_WATER_NUMERIC_BOUNDS = {
    "control_safety_min_temp": (20.0, 55.0),
    "run_minutes": (15.0, 240.0),
    "legionella_temp": (55.0, 70.0),
    "legionella_hold_minutes": (5.0, 180.0),
    "legionella_interval_days": (1.0, 31.0),
    "legionella_window_minutes": (60.0, 480.0),
    "legionella_max_hours": (1.0, 12.0),
    "max_interval_hours": (0.0, 72.0),
    "flow_margin": (0.0, 15.0),
    "cop_eta": (0.2, 0.8),
    "tank_litres": (20.0, 1000.0),
    "heat_loss_watts": (0.0, 1000.0),
    "heat_max_power": (200.0, 30000.0),
    "heat_min_power": (0.0, 30000.0),
    "heating_cop": (0.5, 10.0),
    "hysteresis": (0.0, 15.0),
    "hysteresis_off": (0.0, 5.0),
}

HOT_WATER_ENTITY_FIELDS = ("tank_temp_entity", "target_temp_entity", "start_hysteresis_entity", "immersion_entity",
                           "control_entity", "heating_entity", "cop_entity",
                           "external_temp_entity", "weather_entity")


def merge_hot_water(saved):
    out = copy.deepcopy(HOT_WATER_DEFAULTS)
    saved = saved if isinstance(saved, dict) else {}
    for key in ("configured", "legionella_enabled"):
        if key in saved:
            out[key] = bool(saved[key])
    name = saved.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()[:60]
    for key in HOT_WATER_ENTITY_FIELDS:
        if key in saved:
            out[key] = _clean_entity(saved[key])
    if "heating_entity_state" in saved:
        hs = saved["heating_entity_state"]
        out["heating_entity_state"] = (str(hs).strip() or "on") if hs is not None else "on"
    for key, (lo, hi) in HOT_WATER_NUMERIC_BOUNDS.items():
        if key in saved:
            try:
                out[key] = min(hi, max(lo, float(saved[key])))
            except (TypeError, ValueError):
                pass
    if "control_requires" in saved:
        out["control_requires"] = clean_requirements(saved["control_requires"])
    if "ready_by_times" in saved:
        out["ready_by_times"] = clean_times(saved["ready_by_times"])
    return out


def clean_times(raw):
    """A list of HH:MM strings (or one comma-separated string from the
    form) -> sorted, validated ["HH:MM", ...]."""
    if isinstance(raw, str):
        raw = raw.split(",")
    out = []
    for item in raw or []:
        t = str(item).strip()
        if not t:
            continue
        parts = t.split(":")
        try:
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            continue
        if 0 <= h < 24 and 0 <= m < 60:
            out.append(f"{h:02d}:{m:02d}")
    return sorted(set(out))


def to_hot_water_zone_cfg(hw):
    """The yaml-shaped tank zone dict the engine's zone builder already
    understands, plus the DHW scheduling keys the control step reads."""
    return {
        "name": hw["name"],
        "internal_temp_entity": hw["tank_temp_entity"],
        "target_temp_entity": hw["target_temp_entity"],
        "external_temp_constant": 18.0,
        "external_temp_entity": hw["external_temp_entity"],
        "weather_entity": hw["weather_entity"],
        "heating_entity": hw["heating_entity"],
        "heating_entity_state": hw["heating_entity_state"],
        "cop_entity": hw["cop_entity"],
        "control_entity": hw["control_entity"],
        "control_safety_min_temp": hw["control_safety_min_temp"],
        "control_max_off_hours": 0.0,   # the max interval below is the run guarantee
        "control_requires": hw["control_requires"],
        "tank_litres": hw["tank_litres"],
        "heat_loss_degrees": 0.0,
        "heat_loss_watts": hw["heat_loss_watts"],
        "heat_max_power": hw["heat_max_power"],
        "heat_min_power": hw["heat_min_power"],
        "heat_share": 1.0,
        "heating_cop": hw["heating_cop"],
        "hysteresis": hw["hysteresis"],
        "hysteresis_off": hw["hysteresis_off"],
        "active": False,   # no anticipation curve for a tank
        "dhw": {"start_hysteresis_entity": hw["start_hysteresis_entity"],
                "ready_by_times": hw.get("ready_by_times", []),
                "immersion_entity": hw.get("immersion_entity"),
                "legionella": {"enabled": hw["legionella_enabled"],
                               "temp": hw["legionella_temp"],
                               "hold_minutes": hw["legionella_hold_minutes"],
                               "interval_days": hw["legionella_interval_days"],
                               "window_minutes": hw["legionella_window_minutes"],
                               "max_hours": hw["legionella_max_hours"]},
                "run_minutes": hw["run_minutes"],
                "max_interval_hours": hw["max_interval_hours"],
                "flow_margin": hw["flow_margin"],
                "cop_eta": hw["cop_eta"]},
    }


# (min, max) clamps for the numeric fields — a typo must never reach a
# heating device as a setpoint.
NUMERIC_BOUNDS = {
    "target_temp": (5.0, 30.0),
    "control_safety_min_temp": (5.0, 25.0),
    "control_max_off_hours": (0.0, 48.0),
    "heat_loss_degrees": (0.0, 1.0),
    "heat_loss_watts": (0.0, 5000.0),
    "heat_gain_static": (0.0, 10000.0),
    "heat_max_power": (0.0, 50000.0),
    "heat_min_power": (0.0, 50000.0),
    "heating_cop": (0.5, 10.0),
    "thermal_mass_wh_per_c": (500.0, 50000.0),
    "hysteresis": (0.0, 5.0),
    "hysteresis_off": (0.0, 5.0),
    "flow_min": (20.0, 60.0),
    "flow_max": (25.0, 70.0),
    "flow_max_step": (0.5, 20.0),
    "flow_fallback": (25.0, 60.0),
    "water_dt": (2.0, 15.0),
    "emitter_rated_w_at_dt50": (500.0, 100000.0),
    "cop_eta": (0.2, 0.8),
    "comfort_below": (0.0, 5.0),
    "comfort_above": (0.0, 5.0),
    "battery_wear_p_per_kwh": (0.0, 30.0),
    "solar_gain_w_per_kw_pv": (0.0, 2000.0),
}

ENTITY_FIELDS = ("internal_temp_entity", "internal_temp_fallback_entity",
                 "external_temp_entity", "weather_entity",
                 "control_entity", "heating_entity", "cop_entity", "flow_entity",
                 "thermal_power_entity", "flow_temp_entity", "return_temp_entity",
                 "compressor_entity")
BOOL_FIELDS = ("configured", "tariff_optimise", "notify_external_commands")


def _clean_entity(value):
    """Blank / non-string means "not set" (None), not an empty-string
    entity_id that would then fail every lookup."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def clean_requirements(raw):
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        entity = _clean_entity(item.get("entity"))
        state = item.get("state")
        if entity is None or state is None or str(state).strip() == "":
            continue
        out.append({"entity": entity, "state": str(state).strip()})
    return out


def merge_settings(saved):
    """DEFAULTS overlaid with whatever the dashboard saved, every field
    coerced and clamped so a hand-edited or half-written file can't
    produce a nonsense zone (an unparseable number falls back to its
    default, never to zero)."""
    out = copy.deepcopy(DEFAULTS)
    saved = saved if isinstance(saved, dict) else {}
    for key in BOOL_FIELDS:
        if key in saved:
            out[key] = bool(saved[key])
    name = saved.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()[:60]
    for key in ENTITY_FIELDS:
        if key in saved:
            out[key] = _clean_entity(saved[key])
    if "heating_entity_state" in saved:
        hs = saved["heating_entity_state"]
        out["heating_entity_state"] = (str(hs).strip() or "on") if hs is not None else "on"
    for key, (lo, hi) in NUMERIC_BOUNDS.items():
        if key in saved:
            try:
                out[key] = min(hi, max(lo, float(saved[key])))
            except (TypeError, ValueError):
                pass
    if "control_requires" in saved:
        out["control_requires"] = clean_requirements(saved["control_requires"])
    if str(saved.get("heat_pricing", "")).lower() in ("battery", "cop", "tariff"):
        out["heat_pricing"] = str(saved["heat_pricing"]).lower()
    if str(saved.get("comfort_priority", "")).lower() in ("cop", "balanced", "comfort"):
        out["comfort_priority"] = str(saved["comfort_priority"]).lower()
    if out["flow_max"] < out["flow_min"]:
        out["flow_max"] = out["flow_min"]
    # The hot water zone rides along in the same file under its own key.
    out["hot_water"] = merge_hot_water(saved.get("hot_water"))
    return out


def to_zone_cfg(settings):
    """The apps.yaml-shaped zone dict the engine's existing zone builder
    already understands — so a dashboard zone and a yaml zone are the
    same thing downstream, one code path."""
    return {
        "name": settings["name"],
        "internal_temp_entity": settings["internal_temp_entity"],
        "internal_temp_fallback_entity": settings["internal_temp_fallback_entity"],
        "target_temp_constant": settings["target_temp"],
        "external_temp_entity": settings["external_temp_entity"],
        "weather_entity": settings["weather_entity"],
        "heating_entity": settings["heating_entity"],
        "heating_entity_state": settings["heating_entity_state"],
        "cop_entity": settings["cop_entity"],
        "control_entity": settings["control_entity"],
        "control_safety_min_temp": settings["control_safety_min_temp"],
        "control_max_off_hours": settings["control_max_off_hours"],
        "control_requires": settings["control_requires"],
        "heat_loss_degrees": settings["heat_loss_degrees"],
        "heat_loss_watts": settings["heat_loss_watts"],
        "heat_gain_static": settings["heat_gain_static"],
        "heat_max_power": settings["heat_max_power"],
        "heat_min_power": settings["heat_min_power"],
        "heat_share": 1.0,
        "heating_cop": settings["heating_cop"],
        "thermal_mass_wh_per_c": settings["thermal_mass_wh_per_c"],
        "hysteresis": settings["hysteresis"],
        "hysteresis_off": settings["hysteresis_off"],
        "flow_entity": settings["flow_entity"],
        "flow_min": settings["flow_min"],
        "flow_max": settings["flow_max"],
        "flow_max_step": settings["flow_max_step"],
        "flow_fallback": settings["flow_fallback"],
        "water_dt": settings["water_dt"],
        "emitter_rated_w_at_dt50": settings["emitter_rated_w_at_dt50"],
        "cop_eta": settings["cop_eta"],
        "comfort_below": settings["comfort_below"],
        "comfort_above": settings["comfort_above"],
        "tariff_optimise": settings["tariff_optimise"],
        "heat_pricing": settings["heat_pricing"],
        "comfort_priority": settings["comfort_priority"],
        "solar_gain_w_per_kw_pv": settings["solar_gain_w_per_kw_pv"],
        "thermal_power_entity": settings["thermal_power_entity"],
        "flow_temp_entity": settings["flow_temp_entity"],
        "return_temp_entity": settings["return_temp_entity"],
        "compressor_entity": settings["compressor_entity"],
    }

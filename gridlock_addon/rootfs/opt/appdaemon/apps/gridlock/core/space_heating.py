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
    # The one room thermometer the model treats as "the house". A
    # climate.* entity's current_temperature is read automatically.
    "internal_temp_entity": "climate.bedroom_4_qing_thermo",
    # GridLock owns the target itself — there's deliberately no
    # thermostat in the loop. Set from the GridWarm tab's ✏️.
    "target_temp": 20.0,
    "external_temp_entity": "sensor.openweathermap_temperature",
    "weather_entity": "weather.openweathermap",
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
    "heat_volume": 123.0,
    "hysteresis": 0.5,
    "hysteresis_off": 0.1,
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
    "heat_volume": (1.0, 5000.0),
    "hysteresis": (0.0, 5.0),
    "hysteresis_off": (0.0, 5.0),
}

ENTITY_FIELDS = ("internal_temp_entity", "external_temp_entity", "weather_entity",
                 "control_entity", "heating_entity", "cop_entity")


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
    if "configured" in saved:
        out["configured"] = bool(saved["configured"])
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
    return out


def to_zone_cfg(settings):
    """The apps.yaml-shaped zone dict the engine's existing zone builder
    already understands — so a dashboard zone and a yaml zone are the
    same thing downstream, one code path."""
    return {
        "name": settings["name"],
        "internal_temp_entity": settings["internal_temp_entity"],
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
        "heat_volume": settings["heat_volume"],
        "hysteresis": settings["hysteresis"],
        "hysteresis_off": settings["hysteresis_off"],
    }

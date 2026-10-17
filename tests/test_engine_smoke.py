"""Smoke test of the engine's GridWarm tick path with a fake AppDaemon
base -- exercises the real _publish_thermal_forecast / _control_thermal_zone
/ _plan_flow code against stubbed HA state, so a KeyError or bad
service call in that path fails here rather than on the live heat pump."""
import json
import os
import sys
import types
from datetime import datetime, timezone

import pytest


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # A stand-in for appdaemon.plugins.hass.hassapi.Hass -- just enough
    # surface for the methods under test.
    fake_hass = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        def __init__(self):
            self.states = {}
            self.published = {}
            self.calls = []
            self.logs = []

        def set_state(self, entity_id, *args, **kwargs):
            self.published[entity_id] = {"state": kwargs.get("state"),
                                         "attributes": kwargs.get("attributes") or {}}
            self.states[entity_id] = {"state": kwargs.get("state"),
                                      "attributes": kwargs.get("attributes") or {}}

        def get_state(self, entity_id=None, attribute=None, **kwargs):
            obj = self.states.get(entity_id)
            if obj is None:
                return None
            if attribute == "all":
                return obj
            if attribute:
                return obj["attributes"].get(attribute)
            return obj["state"]

        def entity_exists(self, entity_id):
            return entity_id in self.states

        def call_service(self, service, **kwargs):
            self.calls.append((service, kwargs))

        def log(self, msg, level="INFO"):
            self.logs.append((level, msg))

        def get_now(self):
            return datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)

    fake_hass.Hass = Hass
    pkg = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    monkeypatch.setitem(sys.modules, "appdaemon", pkg)
    monkeypatch.setitem(sys.modules, "appdaemon.plugins", plugins)
    monkeypatch.setitem(sys.modules, "appdaemon.plugins.hass", hassmod)
    monkeypatch.setitem(sys.modules, "appdaemon.plugins.hass.hassapi", fake_hass)
    sys.modules.pop("gridlock", None)
    import gridlock  # noqa: E402 -- after the fake base is in place

    g = gridlock.GridLock.__new__(gridlock.GridLock)
    Hass.__init__(g)
    # Keep every persisted file in tmp, never in the repo.
    g._site_slug = "test"
    monkeypatch.setattr(g, "_state_path", lambda fn: str(tmp_path / f"test__{fn}"))
    monkeypatch.setattr(g, "_dashboard_path", lambda fn: str(tmp_path / fn))
    g.thermal_zones = []
    g._ui_zone_cache = (None, None, None)
    g._thermal_forecast_warned = set()
    g._thermal_control_state = {}
    g.thermal_learning_state = {}
    g.decision_log = []
    g.notify_service = None
    g.default_import = 0.25
    g.ent_gridwarm_mode = "input_select.gridlock_gridwarm_mode"
    g.build_slots = lambda now: []
    g.get_float_state = lambda eid, default=0.0: _as_float(g.get_state(eid), default)
    g.registry = None
    return g, gridlock


def _as_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _seed_house(g, room_temp="18.4", mode="active"):
    s = g.states
    s["input_select.gridlock_gridwarm_mode"] = {"state": mode, "attributes": {}}
    s["climate.bedroom_4_qing_thermo"] = {"state": "heat", "attributes": {"current_temperature": room_temp}}
    s["sensor.openweathermap_temperature"] = {"state": "4.0", "attributes": {}}
    s["switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"] = {"state": "off", "attributes": {}}
    s["sensor.loft_heatpump_controller_active_state"] = {"state": "Idle", "attributes": {}}
    s["sensor.loft_heatpump_controller_coefficient_of_performance"] = {"state": "3.6", "attributes": {}}
    s["switch.loft_heatpump_controller_parameter_setting_1_supports_room_thermostat"] = {"state": "off", "attributes": {}}
    s["switch.loft_heatpump_controller_weather_compensation_zone_1"] = {"state": "off", "attributes": {}}
    s["select.loft_heatpump_controller_operational_mode"] = {"state": "Heat", "attributes": {}}
    s["number.loft_heatpump_controller_set_water_temperature_t1s_zone_1"] = {"state": "45.0", "attributes": {}}
    s["sensor.loft_heatpump_controller_ability_of_hydraulic_module"] = {"state": "0.0", "attributes": {}}
    s["sensor.loft_heatpump_controller_total_water_outlet_temperature_t1"] = {"state": "20.0", "attributes": {}}
    s["sensor.loft_heatpump_controller_water_inlet_temperature"] = {"state": "19.0", "attributes": {}}
    s["sensor.loft_heatpump_controller_compressor_operating_frequency"] = {"state": "0", "attributes": {}}


def test_dashboard_zone_plans_flow_and_commands_the_heat_pump(engine):
    g, _ = engine
    _seed_house(g)
    g._publish_thermal_forecast(g.get_now())
    pub = g.published["sensor.gridlock_gridwarm"]["attributes"]
    assert pub["space_heating_settings"]["name"] == "House"
    zones = {z["name"]: z for z in pub["zones"]}
    house = zones["House"]
    assert house["current_temp"] == 18.4
    assert house["flow"]["plan_status"] in ("optimal", "fallback")
    assert house["flow"]["required_kw"] > 0
    assert 35.0 <= house["flow"]["flow_setpoint_wanted"] <= 50.0
    assert len(house["flow"]["plan"]) == 48
    ctrl = house["control"]
    assert ctrl["paused"] is False and ctrl["blocked"] == []
    services = [c[0] for c in g.calls]
    assert "number/set_value" in services and "switch/turn_on" in services
    # Setpoint written before the switch is turned on.
    assert services.index("number/set_value") < services.index("switch/turn_on")
    sp = next(kw for svc, kw in g.calls if svc == "number/set_value")
    assert sp["target"]["entity_id"] == "number.loft_heatpump_controller_set_water_temperature_t1s_zone_1"
    assert 35.0 <= sp["value"] <= 50.0
    assert ctrl["flow_setpoint"] == sp["value"]
    assert any("GridWarm: House" in e["state"] for e in g.decision_log)


def test_no_room_reading_runs_weather_compensation_not_just_on(engine):
    g, _ = engine
    _seed_house(g)
    g.states["climate.bedroom_4_qing_thermo"] = {"state": "unavailable", "attributes": {}}
    # 4 °C outside: the house needs real heat -> on, flow capped low.
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["no_reading"] is True and house["current_temp"] is None
    sp = next(kw for svc, kw in g.calls if svc == "number/set_value")
    assert 35.0 <= sp["value"] <= 38.0
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"}}) in g.calls
    assert "weather compensation" in g.decision_log[-1]["reason"]
    # A mild day: almost no demand -> switched off, never held on for ever.
    g.calls.clear()
    g.states["sensor.openweathermap_temperature"]["state"] = "19.0"
    g._publish_thermal_forecast(g.get_now())
    assert ("switch/turn_off", {"target": {"entity_id": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"}}) in g.calls


def test_unmet_precondition_blocks_without_writing(engine):
    g, _ = engine
    _seed_house(g)
    g.states["switch.loft_heatpump_controller_weather_compensation_zone_1"]["state"] = "on"
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["control"]["blocked"] == [
        "switch.loft_heatpump_controller_weather_compensation_zone_1 is on (needs off)"]
    assert house["control"]["blocked_items"][0]["actual"] == "on"
    assert not any(svc.startswith(("switch/", "number/")) for svc, _ in g.calls)
    assert any(c[0] == "persistent_notification/create" for c in g.calls)


def test_read_only_mode_never_writes(engine):
    g, _ = engine
    _seed_house(g, mode="read_only")
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["control"]["paused"] is True
    assert not any(svc.startswith(("switch/", "number/")) for svc, _ in g.calls)


def test_reverted_switch_is_resent_and_flagged(engine):
    g, _ = engine
    _seed_house(g)
    now = g.get_now()
    g._publish_thermal_forecast(now)
    # Device accepted nothing: the switch still reads off on the next tick.
    g.calls.clear()
    g._publish_thermal_forecast(now)
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["control"]["rejected"] is True
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"}}) in g.calls
    # Device now reports on: the flag clears.
    g.states["switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"]["state"] = "on"
    g._publish_thermal_forecast(now)
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["control"]["rejected"] is False


def test_hidden_dashboard_zone_still_publishes_settings(engine, tmp_path):
    g, _ = engine
    _seed_house(g)
    (tmp_path / "gridwarm_settings.json").write_text(json.dumps({"configured": False}))
    g._publish_thermal_forecast(g.get_now())
    pub = g.published["sensor.gridlock_gridwarm"]["attributes"]
    assert pub["zones"] == []
    assert pub["space_heating_settings"]["configured"] is False

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

        def listen_state(self, cb, entity, **kwargs):
            self.listeners = getattr(self, "listeners", {})
            self.listeners[entity] = cb

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
    # Mirrors core.registry.HASensorRegistry.find_all's real signature
    # (keyword-only contains/domain) -- a wrong kwarg there was swallowed
    # as "no weather entity" on a live install.
    def _find_all(*, contains=None, domain=None):
        return [e for e in g.states
                if (domain is None or e.startswith(domain + ".")) and (contains is None or contains in e)]
    g.registry = types.SimpleNamespace(find_all=_find_all)
    g._thermal_weather_ok = None
    g._thermal_forecast_warned = {}
    g._control_listeners = {}
    g._last_plan = None
    g.load_provider = types.SimpleNamespace(planned_circuit_kwh={}, slot_key=lambda dt: int(dt.timestamp()) // 1800)
    g.cheap_rate = 0.10
    g.degradation = 0.01
    g.cfg = types.SimpleNamespace(efficiency=0.9)
    return g, gridlock


def _as_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _seed_house(g, room_temp="13.4", mode="active"):
    s = g.states
    s["input_select.gridlock_gridwarm_mode"] = {"state": mode, "attributes": {}}
    s["sensor.loft_heatpump_controller_room_temperature_ta"] = {"state": room_temp, "attributes": {}}
    s["climate.bedroom_4_qing_thermo"] = {"state": "unavailable", "attributes": {}}
    # No outdoor sensor configured by default: the weather entity's own
    # reading is used (its forecast service is not available in the fake).
    s["weather.forecast_home"] = {"state": "cloudy", "attributes": {"temperature": "4.0"}}
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
    assert house["current_temp"] == 13.4
    assert not house.get("reading_from_fallback")
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


def test_fallback_room_sensor_is_used_when_the_primary_is_unavailable(engine):
    g, _ = engine
    _seed_house(g)
    g.states["sensor.loft_heatpump_controller_room_temperature_ta"] = {"state": "unavailable", "attributes": {}}
    g.states["climate.bedroom_4_qing_thermo"] = {"state": "heat", "attributes": {"current_temperature": "22"}}
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["current_temp"] == 22.0
    assert house["reading_from_fallback"] == "climate.bedroom_4_qing_thermo"
    assert house["flow"] is not None  # a real plan, not the no-reading stub


def test_no_room_reading_runs_weather_compensation_not_just_on(engine):
    g, _ = engine
    _seed_house(g)
    g.states["sensor.loft_heatpump_controller_room_temperature_ta"] = {"state": "unavailable", "attributes": {}}
    # 4 °C outside: the house needs real heat -> on, flow capped low.
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["no_reading"] is True and house["current_temp"] is None
    sp = next(kw for svc, kw in g.calls if svc == "number/set_value")
    assert 35.0 <= sp["value"] <= 38.0
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"}}) in g.calls
    assert any("weather compensation" in e["reason"] for e in g.decision_log
               if e["state"].startswith("GridWarm: House"))
    # A mild day: almost no demand -> switched off, never held on for ever.
    g.calls.clear()
    g.states["weather.forecast_home"]["attributes"]["temperature"] = "19.0"
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
    (tmp_path / "gridwarm_settings.json").write_text(
        json.dumps({"configured": False, "hot_water": {"configured": False}}))
    g._publish_thermal_forecast(g.get_now())
    pub = g.published["sensor.gridlock_gridwarm"]["attributes"]
    assert pub["zones"] == []
    assert pub["space_heating_settings"]["configured"] is False


def _seed_tank(g):
    s = g.states
    s["sensor.loft_heatpump_controller_water_tank_temperature_t5"] = {"state": "42.0", "attributes": {}}
    s["number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"] = {"state": "53.0", "attributes": {}}
    s["number.loft_heatpump_controller_dt5_on"] = {"state": "10.0", "attributes": {}}
    s["switch.loft_heatpump_controller_power_dhw_t5s"] = {"state": "off", "attributes": {}}
    s["switch.loft_heatpump_controller_parameter_setting_1_enable_water_heating"] = {"state": "on", "attributes": {}}


def test_hot_water_zone_plans_a_window_and_powers_the_circuit_when_it_starts(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    g._publish_thermal_forecast(g.get_now())
    pub = g.published["sensor.gridlock_gridwarm"]["attributes"]
    assert pub["hot_water_settings"]["name"] == "Hot water"
    hw = {z["name"]: z for z in pub["zones"]}["Hot water"]
    assert hw["is_tank"] is True and hw["target_editable"] is False
    d = hw["dhw"]
    assert d["window_start"] and d["window_end"]
    assert len(d["slots"]) == 48
    # Flat rates and a constant 4 °C -> every slot scores the same, so the
    # cheapest window is the first one: it starts now and commits.
    assert d["committed"] is True and d["heat_now"] is True
    assert d["will_heat"] is True   # 42 <= 53 - 10
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_power_dhw_t5s"}}) in g.calls
    # The unit's own timer flips it off mid-window: put straight back.
    g.calls.clear()
    g.listeners["switch.loft_heatpump_controller_power_dhw_t5s"](
        "switch.loft_heatpump_controller_power_dhw_t5s", None, "on", "off", {})
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_power_dhw_t5s"}}) in g.calls


def test_hot_water_window_completes_and_is_not_rerun_until_the_interval(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    now = g.get_now()
    g._publish_thermal_forecast(now)
    later = now + timedelta(hours=2)
    # The unit heated the tank during the window -- nothing to do now,
    # even though on a flat tariff "now" is as cheap as any other slot.
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "52.0"
    g.calls.clear()
    g._publish_thermal_forecast(later)
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["committed"] is False
    assert not hw["dhw"].get("needed")
    assert hw["dhw"]["hours_since_last"] == 1.0
    assert hw["dhw"]["deadline_hours"] == 23.0
    assert ("switch/turn_off", {"target": {"entity_id": "switch.loft_heatpump_controller_power_dhw_t5s"}}) in g.calls


def test_hot_water_floor_forces_a_run_outside_the_window(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    now = g.get_now()
    g._publish_thermal_forecast(now)
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "52.0"
    g._publish_thermal_forecast(now + timedelta(hours=2))   # window done, tank warm
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "38.0"
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(hours=3))
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_power_dhw_t5s"}}) in g.calls


def test_battery_plan_pricing_flattens_the_heating_price_where_the_battery_covers_it(engine):
    g, _ = engine
    _seed_house(g)
    from datetime import timedelta
    now = g.get_now()
    # A fake solved battery plan: a cheap 7p charge slot, then peak-rate
    # slots the battery covers, then one where it runs flat.
    starts = [now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=30 * i) for i in range(96)]
    slots = [{"start": st, "imp": 0.07 if i == 0 else 0.30, "exp": 0.15, "export": 0.0} for i, st in enumerate(starts)]
    ct = [{"grid_in": 2.0 if i == 0 else (1.0 if i == 20 else 0.0), "charge_in": 2.0 if i == 0 else 0.0,
           "battery_kwh": 0.0 if i in (0, 20) else 1.0} for i in range(96)]
    g._last_plan = {"slots": slots, "cost_trace": ct, "ts": now.isoformat()}
    g.build_slots = lambda now_: slots
    g._publish_thermal_forecast(now)
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    plan = house["flow"]["plan"]
    assert house["flow"]["pricing_mode"] == "battery"
    batt_p = (0.07 / 0.9 + 0.03) * 100   # 7p charge rate grossed up for losses, plus the 3p/kWh wear default
    # Slots 1-19 are battery-covered but the battery runs flat at slot 20,
    # so an extra kWh there is really paid at 30p; after slot 20 the
    # battery covers again at its own cost.
    assert plan[5]["price"] == pytest.approx(30.0, abs=0.05) and plan[5]["grid_price"] == pytest.approx(30.0, abs=0.05)
    assert plan[25]["price"] == pytest.approx(batt_p, abs=0.05) and plan[25]["grid_price"] == pytest.approx(30.0, abs=0.05)


def test_heat_pump_plan_is_handed_to_the_battery_load_forecast(engine):
    g, gridlock = engine
    _seed_house(g)
    _seed_tank(g)
    g.states["sensor.loft_heatpump_controller_active_state"]["state"] = "Heating"
    g.states["sensor.loft_heatpump_controller_ability_of_hydraulic_module"]["state"] = "5.0"
    g.states["sensor.loft_heatpump_controller_total_water_outlet_temperature_t1"]["state"] = "40.0"
    now = g.get_now()
    g._publish_thermal_forecast(now)
    planned = g.load_provider.planned_circuit_kwh[gridlock.HEATPUMP_POWER_ENTITY]
    assert planned, "space heating + hot water electricity should be planned per slot"
    first = min(planned)
    assert first == g.load_provider.slot_key(now)
    # Cold room, cold tank: the first slot carries both the heating and the DHW run.
    assert planned[first] > 0.5
    pub = g.published[gridlock.HEATPUMP_POWER_ENTITY]
    assert pub["attributes"]["device_class"] == "power"
    # 5 kW thermal at 40 °C flow, 4 °C outside -> roughly 1.5-2 kW electrical
    assert 1000 < float(pub["state"]) < 2500

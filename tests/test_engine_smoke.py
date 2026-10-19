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
    g.grid_connection_off = lambda: None
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


TADO = ["climate.smart_thermostat_x", "climate.master_bedroom_master_bedroom_thermo", "climate.bedroom_4_qing_thermo"]


def _set_room(g, temp):
    for e in TADO:
        g.states[e] = ({"state": "unavailable", "attributes": {}} if temp == "unavailable"
                       else {"state": "heat", "attributes": {"current_temperature": temp}})
    g.states["sensor.loft_heatpump_controller_room_temperature_ta"]["state"] = temp


def _seed_house(g, room_temp="13.4", mode="active"):
    s = g.states
    s["input_select.gridlock_gridwarm_mode"] = {"state": mode, "attributes": {}}
    s["sensor.loft_heatpump_controller_room_temperature_ta"] = {"state": room_temp, "attributes": {}}
    for e in TADO:
        s[e] = {"state": "heat", "attributes": {"current_temperature": room_temp}}
    s["climate.bedroom_4_qing_thermo"] = {"state": "unavailable", "attributes": {}}
    # The heat pump's own outdoor sensor is the default outdoor reading;
    # the weather entity only supplies the forecast (not available in the fake).
    s["sensor.loft_heatpump_controller_outdoor_ambient_temperature"] = {"state": "4.0", "attributes": {}}
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
    s["sensor.loft_heatpump_controller_water_flow"] = {"state": "0.0", "attributes": {}}
    s["sensor.loft_heatpump_controller_outdoor_unit_voltage"] = {"state": "240", "attributes": {}}
    s["sensor.loft_heatpump_controller_outdoor_unit_current"] = {"state": "0", "attributes": {}}
    s["binary_sensor.loft_heatpump_controller_status_bit_1_defrosting"] = {"state": "off", "attributes": {}}


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
    for e in TADO:
        g.states[e] = {"state": "unavailable", "attributes": {}}
    g.states["sensor.loft_heatpump_controller_room_temperature_ta"]["state"] = "22"
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["current_temp"] == 22.0
    assert house["reading_from_fallback"] == "sensor.loft_heatpump_controller_room_temperature_ta"
    assert house["flow"] is not None  # a real plan, not the no-reading stub


def test_no_room_reading_runs_weather_compensation_not_just_on(engine):
    g, _ = engine
    _seed_house(g)
    _set_room(g, "unavailable")
    # A cold day (-3 °C): the house needs real heat -> on, flow capped low.
    g.states["sensor.loft_heatpump_controller_outdoor_ambient_temperature"]["state"] = "-3.0"
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["no_reading"] is True and house["current_temp"] is None
    sp = next(kw for svc, kw in g.calls if svc == "number/set_value")
    assert 25.0 <= sp["value"] <= 38.0    # the unit's 25 C floor up to the no-reading cap
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"}}) in g.calls
    assert any("weather compensation" in e["reason"] for e in g.decision_log
               if e["state"].startswith("GridWarm: House"))
    # A mild day: almost no demand -> switched off, never held on for ever.
    g.calls.clear()
    g.states["sensor.loft_heatpump_controller_outdoor_ambient_temperature"]["state"] = "19.0"
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
    s["switch.loft_heatpump_controller_function_setting_silent_mode"] = {"state": "off", "attributes": {}}
    s["switch.loft_heatpump_controller_function_setting_disinfect"] = {"state": "off", "attributes": {}}


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
    # Ordinary windows only -- the legionella cycle has its own tests.
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False, "max_interval_hours": 24, "tank_litres": 150}}, f)
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
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False, "control_safety_min_temp": 40}}, f)
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


def test_forecast_points_are_found_under_the_websocket_response_shape(engine):
    g, gridlock = engine
    pts = [{"datetime": "2026-10-17T09:00:00+00:00", "temperature": 7.1}]
    bare = {"weather.x": {"forecast": pts}}
    assert gridlock.GridLock._forecast_points(bare, "weather.x") == pts
    assert gridlock.GridLock._forecast_points({"response": bare}, "weather.x") == pts
    assert gridlock.GridLock._forecast_points({"result": {"response": bare}}, "weather.x") == pts
    assert gridlock.GridLock._forecast_points({"result": bare}, "weather.x") == pts
    assert gridlock.GridLock._forecast_points({"response": {}}, "weather.x") == []
    assert gridlock.GridLock._forecast_points(None, "weather.x") == []


def test_weather_curve_uses_the_service_response(engine):
    g, gridlock = engine
    _seed_house(g)
    from datetime import timedelta
    now = g.get_now()
    pts = [{"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": 5.0 + h} for h in range(12)]
    def call_service(service, **kw):
        g.calls.append((service, kw))
        if service == "weather/get_forecasts":
            return {"response": {kw["entity_id"]: {"forecast": pts}}}
        return None
    g.call_service = call_service
    curve = g._thermal_weather_curve(None, now, 12 * 12, 5, 4.0)
    assert curve[0] == 5.0 and curve[12 * 6] == 11.0   # 6 h in -> 11 C
    assert g._thermal_weather_ok == "weather.forecast_home"


def test_house_plan_uses_the_auto_detected_forecast(engine):
    g, gridlock = engine
    _seed_house(g)
    from datetime import timedelta
    now = g.get_now()
    pts = [{"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": 4.0 + h} for h in range(24)]
    real_call = g.call_service
    def call_service(service, **kw):
        if service == "weather/get_forecasts":
            return {"response": {kw["entity_id"]: {"forecast": pts}}}
        return real_call(service, **kw)
    g.call_service = call_service
    g._publish_thermal_forecast(now)
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    ext = [p["external"] for p in house["flow"]["plan"]]
    assert ext[0] == 4.0 and ext[12] == 10.0, ext[:14]   # 6 h in -> 10 C: the forecast, not a flat line
    assert any("outdoor forecast from weather.forecast_home" in m for _, m in g.logs)


def test_battery_pricing_falls_back_to_the_tariff_before_the_first_battery_solve(engine):
    g, _ = engine
    _seed_house(g)
    now = g.get_now()
    from datetime import timedelta
    starts = [now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=30 * i) for i in range(96)]
    slots = [{"start": st, "imp": 0.34 if i < 5 else 0.0666, "exp": 0.04, "export": 0.0} for i, st in enumerate(starts)]
    g.build_slots = lambda now_: slots
    g._last_plan = None
    curve = g._heating_rate_curve(slots, "battery")
    assert curve[0] == 0.34 and curve[6 * 6] == 0.0666     # the tariff, not a flat battery cost
    assert g._heating_rate_curve(slots, "cop")[0] == g._heating_rate_curve(slots, "cop")[40]  # cop mode: flat by design


def test_legionella_cycle_raises_holds_and_restores_the_setpoint(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    g.states["binary_sensor.loft_heatpump_controller_load_output_electric_heater_tbh"] = {"state": "off", "attributes": {}}
    from datetime import timedelta
    tank = "sensor.loft_heatpump_controller_water_tank_temperature_t5"
    sp = "number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"
    now = g.get_now()
    # Never done -> due; flat prices -> the window starts now and the cycle begins.
    g._publish_thermal_forecast(now)
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["legionella"]["phase"] == "heating"
    assert ("number/set_value", {"target": {"entity_id": sp}, "value": 60.0}) in g.calls
    g.states[sp]["state"] = "60.0"
    # Tank reaches 59 (whole-degree sensor, unit stops just short) an hour later -> holding.
    g.states[tank]["state"] = "59.0"
    g._publish_thermal_forecast(now + timedelta(hours=1))
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["legionella"]["phase"] == "holding"
    assert hw["control"]["commanded"] is True
    # 61 minutes later -> done, setpoint back to 53.
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(hours=2, minutes=1))
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["legionella"]["phase"] == "idle"
    assert hw["dhw"]["legionella"]["last_done"]
    assert ("number/set_value", {"target": {"entity_id": sp}, "value": 53.0}) in g.calls


def test_legionella_cycle_is_abandoned_and_setpoint_restored_after_the_cap(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    sp = "number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"
    now = g.get_now()
    g._publish_thermal_forecast(now)
    g.states[sp]["state"] = "60.0"
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(hours=6))   # window over, tank never got there
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["legionella"]["last_failed"]
    assert ("number/set_value", {"target": {"entity_id": sp}, "value": 53.0}) in g.calls


def test_hot_water_slots_carry_a_predicted_tank_temperature(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    g._publish_thermal_forecast(g.get_now())
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    tanks = [s["tank"] for s in hw["dhw"]["slots"]]
    assert len(tanks) == 48 and all(t is not None for t in tanks)
    assert max(tanks) >= 59.0          # legionella night: climbs to 60
    assert tanks[-1] < max(tanks)      # and cools afterwards


def test_legionella_never_holds_the_circuit_past_the_window_or_retries_the_same_night(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    sw = "switch.loft_heatpump_controller_power_dhw_t5s"
    now = g.get_now()
    g._publish_thermal_forecast(now)                         # cycle starts, 3 h window
    g.states[sw]["state"] = "on"
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(hours=3, minutes=5))   # window over, never reached 60
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["legionella"]["last_failed"]
    assert hw["dhw"]["legionella"]["phase"] != "heating"
    assert ("switch/turn_off", {"target": {"entity_id": sw}}) in g.calls
    # Five minutes later it must NOT start another cycle the same night.
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(hours=3, minutes=10))
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["legionella"]["phase"] in ("idle", "due") and hw["dhw"]["legionella"]["due"] is not True


def test_legionella_window_is_sized_from_the_tank_not_a_fixed_three_hours(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"tank_litres": 150}}, f)
    from datetime import timedelta
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "57.0"
    now = g.get_now()
    g._publish_thermal_forecast(now)
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    start, end = (g._iso(hw["dhw"]["window_start"]), g._iso(hw["dhw"]["window_end"]))
    # 3 C on the immersion (~16 min) + 60 min hold + 10 min margin -> 1.5 h
    assert (end - start) <= timedelta(hours=1, minutes=30)


def test_warm_tank_is_not_forced_into_a_peak_slot_by_a_timer(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False}}, f)
    from datetime import timedelta
    tank = "sensor.loft_heatpump_controller_water_tank_temperature_t5"
    g.states[tank]["state"] = "57.0"
    now = g.get_now()
    for h in range(0, 30, 2):   # a day and more of a warm tank: never a run
        g.calls.clear()
        g._publish_thermal_forecast(now + timedelta(hours=h))
        hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
        assert hw["dhw"]["status"] == "not_needed", (h, hw["dhw"]["status"])
        assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_power_dhw_t5s"}}) not in g.calls


def test_heating_keeps_its_minimum_run_and_rest_times(engine):
    g, _ = engine
    _seed_house(g)
    from datetime import timedelta
    room = "sensor.loft_heatpump_controller_room_temperature_ta"
    sw = ("switch/turn_off", {"target": {"entity_id": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"}})
    now = g.get_now()
    g._publish_thermal_forecast(now)                       # cold room -> on
    g.states["switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"]["state"] = "on"
    _set_room(g, "25.0")                                    # suddenly warm
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(minutes=10))
    assert sw not in g.calls                                # held on: 10 < 30 min
    g._publish_thermal_forecast(now + timedelta(minutes=35))
    assert sw in g.calls                                    # minimum run done -> off


def test_power_cut_holds_heating_off_until_the_house_is_cold(engine):
    g, _ = engine
    _seed_house(g, room_temp="13.0")
    _seed_tank(g)
    g.grid_connection_off = lambda: "inverter reports off-grid"
    on = ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"}})
    g._publish_thermal_forecast(g.get_now())
    assert on not in g.calls                                # 13 C: battery saved
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_power_dhw_t5s"}}) not in g.calls
    _set_room(g, "9.5")
    g.calls.clear()
    from datetime import timedelta
    g._publish_thermal_forecast(g.get_now() + timedelta(minutes=5))
    assert on in g.calls
    sp = next(kw for svc, kw in g.calls if svc == "number/set_value"
              and kw["target"]["entity_id"].endswith("t1s_zone_1"))
    assert sp["value"] == 25.0


def test_dashboard_saves_only_what_differs_from_the_defaults():
    from core.space_heating import diff_from_defaults, merge_settings
    assert diff_from_defaults(merge_settings({})) == {}
    d = diff_from_defaults(merge_settings({"target_temp": 16, "hot_water": {"run_minutes": 45}}))
    assert d == {"target_temp": 16.0, "hot_water": {"run_minutes": 45.0}}


def test_cool_tank_waits_for_the_night_and_only_a_cold_one_tops_up_by_day(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False}}, f)
    now = g.get_now()   # 07:00 UTC
    start = now.replace(minute=0, second=0, microsecond=0)
    # Daytime peak, cheap 23:00-05:30 local: a 35 C tank waits for the night.
    slots = [{"start": start + timedelta(minutes=30 * i),
              "imp": 0.0666 if 22 <= (start + timedelta(minutes=30 * i)).hour or (start + timedelta(minutes=30 * i)).hour < 4 else 0.34,
              "exp": 0.04, "export": 0.0, "pv": 0.0} for i in range(96)]
    g.build_slots = lambda now_: slots
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "35.0"
    g._publish_thermal_forecast(now)
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["status"] == "waiting"
    assert g._iso(hw["dhw"]["window_start"]).hour in (22, 23, 0, 1, 2, 3)
    # Below 25 C by day: topped up within a couple of hours, not left for the night.
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "23.0"
    g._publish_thermal_forecast(now + timedelta(minutes=5))
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert g._iso(hw["dhw"]["window_start"]) <= now + timedelta(hours=3)


def test_compressor_minimum_output_is_learned_from_steady_low_speed_running(engine):
    g, _ = engine
    _seed_house(g)
    from datetime import timedelta
    st = g.states
    st["sensor.loft_heatpump_controller_active_state"]["state"] = "Heating"
    st["sensor.loft_heatpump_controller_compressor_operating_frequency"]["state"] = "22"
    st["sensor.loft_heatpump_controller_ability_of_hydraulic_module"]["state"] = "1.2"
    st["sensor.loft_heatpump_controller_total_water_outlet_temperature_t1"]["state"] = "30.0"
    st["sensor.loft_heatpump_controller_water_inlet_temperature"]["state"] = "26.0"
    now = g.get_now()
    for m in range(0, 120, 5):
        g._publish_thermal_forecast(now + timedelta(minutes=m))
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["min_output_learned"] == "yes"
    assert 1150 < house["learned_min_output_w"] < 1500       # from the 2000 default toward 1200


def test_a_30c_tank_in_the_evening_gets_a_long_enough_run_before_the_morning(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False}}, f)
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "30.0"
    now = g.get_now()
    g._publish_thermal_forecast(now)
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    start, end = g._iso(hw["dhw"]["window_start"]), g._iso(hw["dhw"]["window_end"])
    # 23 C x 174 Wh/C = 4 kWh at 3.5 kW ~ 69 min + 10 -> 1.5 h, not a flat hour
    assert end - start >= timedelta(hours=1, minutes=30)
    assert hw["dhw"]["status"] in ("waiting", "running")


def test_hot_water_runs_switch_silent_mode_on_and_back_off(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False}}, f)
    q = "switch.loft_heatpump_controller_function_setting_silent_mode"
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "30.0"
    now = g.get_now()
    g._publish_thermal_forecast(now)                     # flat prices: run starts now
    assert ("switch/turn_on", {"target": {"entity_id": q}}) in g.calls
    g.states[q]["state"] = "on"
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "53.0"
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(hours=3))  # run over
    assert ("switch/turn_off", {"target": {"entity_id": q}}) in g.calls


def test_a_measured_heating_run_calibrates_the_cop_factor(engine):
    g, _ = engine
    _seed_house(g)
    from datetime import timedelta
    st = g.states
    def set_running(on):
        st["sensor.loft_heatpump_controller_active_state"]["state"] = "Heating" if on else "Idle"
        st["sensor.loft_heatpump_controller_compressor_operating_frequency"]["state"] = "35" if on else "0"
        st["sensor.loft_heatpump_controller_water_flow"]["state"] = "1.2" if on else "0"
        st["sensor.loft_heatpump_controller_total_water_outlet_temperature_t1"]["state"] = "35.0"
        st["sensor.loft_heatpump_controller_water_inlet_temperature"]["state"] = "31.0"     # 4 C split -> 5.58 kW
        st["sensor.loft_heatpump_controller_outdoor_unit_current"]["state"] = "6" if on else "0"  # 1440 + 70 W
    now = g.get_now()
    set_running(True)
    for m in range(0, 65, 5):
        g._publish_thermal_forecast(now + timedelta(minutes=m))
    set_running(False)
    g._publish_thermal_forecast(now + timedelta(minutes=70))
    runs = g.thermal_learning_state["House"]["cop_runs"]
    assert len(runs) == 1 and runs[0]["mode"] == "heating"
    assert 3.5 < runs[0]["cop"] < 3.9          # 5.58 kW / 1.51 kW = 3.70
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["learned_cop_eta"] != 0.45      # calibrated away from the default
    assert any("measured heating run" in e["state"] for e in g.decision_log)


def test_the_units_own_disinfection_is_cancelled_but_gridwarms_cycle_is_not(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    dis = "switch.loft_heatpump_controller_function_setting_disinfect"
    g._publish_thermal_forecast(g.get_now())          # loads zones, registers the listener
    cb = g.listeners[dis]
    # GridWarm's own cycle running (first tick starts it): leave the unit alone.
    g.calls.clear()
    cb(dis, None, "off", "on", {})
    assert ("switch/turn_off", {"target": {"entity_id": dis}}) not in g.calls
    # No cycle of ours: the unit's scheduled disinfection is switched off.
    g.thermal_learning_state["Hot water"]["legionella"]["phase"] = None
    g.calls.clear()
    cb(dis, None, "off", "on", {})
    assert ("switch/turn_off", {"target": {"entity_id": dis}}) in g.calls
    assert any("disinfection cancelled" in e["state"] for e in g.decision_log)


def test_house_temperature_averages_every_reporting_room(engine):
    g, _ = engine
    _seed_house(g)
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"internal_temp_entities": ["climate.smart_thermostat_x",
                                              "climate.master_bedroom_master_bedroom_thermo",
                                              "sensor.climate_sensor_w100_temperature"]}, f)
    g.states["climate.smart_thermostat_x"] = {"state": "heat", "attributes": {"current_temperature": "18.0"}}
    g.states["climate.master_bedroom_master_bedroom_thermo"] = {"state": "heat", "attributes": {"current_temperature": "17.0"}}
    g.states["sensor.climate_sensor_w100_temperature"] = {"state": "unavailable", "attributes": {}}
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["current_temp"] == 17.5 and house["rooms_averaged"] == 2

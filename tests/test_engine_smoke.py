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
    # A fallback has to be chosen now (none by default -- the unit's Ta
    # reads a fixed 25 °C once its flag is off).
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"internal_temp_entity": "sensor.loft_heatpump_controller_room_temperature_ta"}, f)
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
    # The window mechanics are tested at any hour; once-a-night has its own tests.
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"night_only": False}}, f)


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
        json.dump({"hot_water": {"legionella_enabled": False, "max_interval_hours": 24, "tank_litres": 150,
                                 "night_only": False}}, f)
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
    g.states["sensor.loft_heatpump_controller_outdoor_unit_current"]["state"] = "6"
    g.states["sensor.loft_heatpump_controller_compressor_operating_frequency"]["state"] = "35"
    now = g.get_now()
    g._publish_thermal_forecast(now)
    planned = g.load_provider.planned_circuit_kwh[gridlock.HEATPUMP_POWER_ENTITY]
    assert planned, "space heating + hot water electricity should be planned per slot"
    first = min(planned)
    assert first == g.load_provider.slot_key(now)
    # Cold room, cold tank: the first slot carries the tank's run -- the unit
    # does one or the other, so the house waits until the tank is done.
    assert planned[first] > 0.2
    assert sum(planned.values()) > planned[first]   # the house's heat follows later
    pub = g.published[gridlock.HEATPUMP_POWER_ENTITY]
    assert pub["attributes"]["device_class"] == "power"
    # measured: 240 V x 6 A + 70 W pump
    assert float(pub["state"]) == 1510.0


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
        json.dump({"hot_water": {"legionella_enabled": False, "ready_by_times": ["06:30"]}}, f)
    now = g.get_now()   # 07:00 UTC
    start = now.replace(minute=0, second=0, microsecond=0)
    # Daytime peak, cheap 23:00-05:30 local: a 42 C tank waits for the night.
    slots = [{"start": start + timedelta(minutes=30 * i),
              "imp": 0.0666 if 22 <= (start + timedelta(minutes=30 * i)).hour or (start + timedelta(minutes=30 * i)).hour < 4 else 0.34,
              "exp": 0.04, "export": 0.0, "pv": 0.0} for i in range(96)]
    g.build_slots = lambda now_: slots
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "42.0"
    g._publish_thermal_forecast(now)
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["status"] == "waiting"
    assert g._iso(hw["dhw"]["window_start"]).hour in (22, 23, 0, 1, 2, 3)
    # Below 25 C with the night far off: topped up within a couple of
    # hours, to the normal setpoint only.
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "23.0"
    g._publish_thermal_forecast(now + timedelta(minutes=5))
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert g._iso(hw["dhw"]["window_start"]) <= now + timedelta(hours=3)
    if hw["dhw"]["committed"]:
        assert hw["dhw"]["daily_target"] == 53.0      # the unit's setpoint, not a day's worth


def _peak_and_night_slots(g, start):
    from datetime import timedelta
    return [{"start": start + timedelta(minutes=30 * i),
             "imp": 0.0666 if (start + timedelta(minutes=30 * i)).astimezone().hour >= 23
             or (start + timedelta(minutes=30 * i)).astimezone().hour < 5 else 0.3447,
             "exp": 0.04, "export": 0.0, "pv": 0.0} for i in range(96)]


def test_a_warm_tank_is_never_heated_at_peak_for_the_evening_showers(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False, "ready_by_times": ["19:30@45"]}}, f)
    g.states["number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"]["state"] = "45.0"
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "46.0"
    # Learned: showers around 19:00 (the draw the ready-by is for).
    learn = g.thermal_learning_state.setdefault("Hot water", {})
    learn["dhw_profile"] = [0.0] * 38 + [1300.0, 800.0] + [0.0] * 8
    now = g.get_now()
    t1901 = now.astimezone().replace(hour=19, minute=1, second=0, microsecond=0).astimezone(now.tzinfo)
    g.build_slots = lambda now_: _peak_and_night_slots(g, now_.replace(minute=0, second=0, microsecond=0))
    g.calls.clear()
    g._publish_thermal_forecast(t1901)
    d = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]["dhw"]
    assert d["committed"] is False and d["heat_now"] is False
    assert ("switch/turn_on", {"target": {"entity_id": "switch.loft_heatpump_controller_power_dhw_t5s"}}) not in g.calls


def test_the_tank_is_heated_once_a_night_to_53(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    from datetime import timedelta
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False, "ready_by_times": ["19:30@45"]}}, f)
    g.states["number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"]["state"] = "45.0"
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "23.0"
    now = g.get_now()
    t20 = now.astimezone().replace(hour=20, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
    g.build_slots = lambda now_: _peak_and_night_slots(g, now_.replace(minute=0, second=0, microsecond=0))
    # 20:00, below 25 after the showers -- but the night is close, so it waits.
    g._publish_thermal_forecast(t20)
    d = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]["dhw"]
    assert d["committed"] is False
    start = g._iso(d["window_start"]).astimezone()
    assert start.hour >= 23 or start.hour < 5
    # 23:00: the night's run starts, sized to still be 45 at 19:30.
    t23 = t20 + timedelta(hours=3)
    g._publish_thermal_forecast(t23)
    d = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]["dhw"]
    if not d["committed"]:
        # The cheapest night slot may be later -- step through the night.
        for m in range(30, 390, 30):
            g._publish_thermal_forecast(t23 + timedelta(minutes=m))
            d = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]["dhw"]
            if d["committed"]:
                break
    assert d["committed"] is True
    assert d["daily_target"] == 53.0
    run_end = g._iso(d["window_end"])
    # After that run ends, the same night never starts another, even with a cool tank.
    g._publish_thermal_forecast(run_end + timedelta(minutes=5))
    d = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]["dhw"]
    assert d["committed"] is False


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
        json.dump({"hot_water": {"legionella_enabled": False, "night_only": False}}, f)
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


def test_house_is_planned_around_the_hot_water_run(engine):
    g, _ = engine
    _seed_house(g, room_temp="15.0")
    _seed_tank(g)
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"target_temp": 19, "hot_water": {"legionella_enabled": False, "night_only": False}}, f)
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "30.0"
    g._publish_thermal_forecast(g.get_now())     # flat prices: tank run starts now
    zones = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}
    assert zones["Hot water"]["dhw"]["status"] == "running"
    plan = zones["House"]["flow"]["plan"]
    busy = [p for p in plan if p["hot_water"] == "yes"]
    assert busy and all(p["heat_kwh"] < 1e-6 for p in busy)
    assert any(p["heat_kwh"] > 0.1 for p in plan if p["hot_water"] == "no")


def test_heat_loss_learning_is_not_fooled_by_sensor_steps(engine):
    g, gridlock = engine
    from datetime import timedelta
    zone = g._build_thermal_zone({"name": "Room", "internal_temp_entity": "x", "heat_loss_degrees": 0.010,
                                  "heat_loss_watts": 170.0, "heat_gain_static": 300.0, "thermal_mass_wh_per_c": 8000})
    now = g.get_now()
    # A house cooling a true 0.06 C/h at 9 C above outdoors, read by a
    # sensor with 0.1 C steps, sampled every 5 minutes for two days.
    true_k = 0.010
    for i in range(0, 48 * 12):
        t = now + timedelta(minutes=5 * i)
        truth = 19.5 - (true_k * 9.0 + (170 - 300) / 8000.0) * (i * 5 / 60.0)
        reading = round(truth, 1)
        g._update_thermal_learning(zone, t, reading, reading - 9.0, False)
    learned = zone["params"].heat_loss_degrees
    assert 0.006 < learned < 0.016, learned      # stays near the truth, no ratchet


def test_heat_pump_power_is_the_measured_draw_when_available(engine):
    g, gridlock = engine
    _seed_house(g)
    g.states["sensor.loft_heatpump_controller_outdoor_unit_current"]["state"] = "5"
    g.states["sensor.loft_heatpump_controller_compressor_operating_frequency"]["state"] = "40"
    g._publish_thermal_forecast(g.get_now())
    assert float(g.published[gridlock.HEATPUMP_POWER_ENTITY]["state"]) == 240 * 5 + 70
    g.states["sensor.loft_heatpump_controller_outdoor_unit_current"]["state"] = "0"
    g.states["sensor.loft_heatpump_controller_compressor_operating_frequency"]["state"] = "0"
    g._publish_thermal_forecast(g.get_now())
    assert float(g.published[gridlock.HEATPUMP_POWER_ENTITY]["state"]) == 0.0


def test_hot_water_heats_just_enough_to_be_45_by_1930(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False, "ready_by_times": ["19:30@45"], "night_only": False}}, f)
    g.states["number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"]["state"] = "45.0"
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "44.0"
    g._publish_thermal_forecast(g.get_now())     # 07:00 UTC, 19:30 local is hours away
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    d = hw["dhw"]
    assert d["reason"] == "ready_by" and d["ready_target"]["temp"] == 45.0
    assert d["ready_target"]["predicted"] < 44.5          # would miss it without heating
    assert 45 < d["daily_target"] <= 55                   # heats a little above 45 to still be there
    assert d["ready_target"]["label"] == "19:30"
    assert max(s["tank"] for s in d["slots"]) >= d["daily_target"] - 0.5   # the chart heats to it too
    # A tank that will still be at 45 by then needs nothing (fresh state,
    # not the run the first check just committed).
    g._thermal_control_state.pop("Hot water", None)
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "55.0"
    g._publish_thermal_forecast(g.get_now())
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    assert hw["dhw"]["status"] == "not_needed"


def test_a_hard_run_is_never_learned_as_the_minimum_output(engine):
    g, _ = engine
    _seed_house(g)
    from datetime import timedelta
    st = g.states
    st["sensor.loft_heatpump_controller_active_state"]["state"] = "Heating"
    st["sensor.loft_heatpump_controller_compressor_operating_frequency"]["state"] = "60"
    st["sensor.loft_heatpump_controller_ability_of_hydraulic_module"]["state"] = "6.8"
    st["sensor.loft_heatpump_controller_total_water_outlet_temperature_t1"]["state"] = "40.0"
    st["sensor.loft_heatpump_controller_water_inlet_temperature"]["state"] = "35.0"
    now = g.get_now()
    for m in range(0, 90, 5):
        g._publish_thermal_forecast(now + timedelta(minutes=m))
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["learned_min_output_w"] == 2000.0      # untouched by a 60 Hz run


def test_a_restart_mid_run_still_puts_the_tank_setpoint_back(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False}}, f)
    g.states["number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"]["state"] = "45.0"
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "23.0"
    now = g.get_now()
    g._publish_thermal_forecast(now)               # daytime top-up commits and runs
    assert g.thermal_learning_state["Hot water"].get("dhw_base_sp") in (None, 45.0)
    # Simulate a run that had raised it to 53, then a restart (in-memory state lost).
    g.thermal_learning_state["Hot water"]["dhw_base_sp"] = 45.0
    g.states["number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"]["state"] = "53.0"
    g._thermal_control_state.clear()
    g.calls.clear()
    from datetime import timedelta
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "53.0"
    g._publish_thermal_forecast(now + timedelta(minutes=5))
    sets = [c for c in g.calls if c[0] == "number/set_value"]
    assert any(c[1].get("value") in (45, 45.0) for c in sets), sets


def test_a_high_compressor_minimum_does_not_push_the_flow_up(engine):
    g, _ = engine
    _seed_house(g)
    # Real numbers: the compressor bottoms out at ~5.6 kW, the house loses ~1 kW.
    g.thermal_learning_state["House"] = {"min_output_w": 5605.0, "emitter_w_at_dt50": 27215.0}
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["learned_min_output_w"] == 5605.0
    flows = [p["flow"] for p in house["flow"]["plan"] if p.get("flow")]
    # (The first slot or two may catch up a cold start.) Steady heating is low and slow.
    steady = flows[2:]
    assert steady and max(steady) <= 30.0, flows


def test_a_planned_zone_heats_to_the_set_target_not_a_nudged_one(engine):
    g, _ = engine
    _seed_house(g, room_temp="19.0")
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    targets = {p["target"] for p in house["flow"]["plan"]}
    assert targets == {house["target_temp"]}, sorted(targets)


def test_heating_stands_down_when_the_unit_holds_its_compressor_off(engine):
    g, _ = engine
    _seed_house(g, room_temp="17.5")
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"target_temp": 20}, f)
    from datetime import timedelta
    sw = "switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"
    now = g.get_now()
    g._publish_thermal_forecast(now)
    assert ("switch/turn_on", {"target": {"entity_id": sw}}) in g.calls
    # The unit takes the switch, runs its pump, but never starts the compressor.
    g.states[sw]["state"] = "on"
    g.states["number.loft_heatpump_controller_set_water_temperature_t1s_zone_1"]["state"] = \
        str(g._thermal_control_state["House"]["flow_setpoint"])
    g.states["sensor.loft_heatpump_controller_total_water_outlet_temperature_t1"]["state"] = "18.0"
    for m in (5, 15):
        g._publish_thermal_forecast(now + timedelta(minutes=m))
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(minutes=30))
    assert ("switch/turn_off", {"target": {"entity_id": sw}}) in g.calls
    g.states[sw]["state"] = "off"
    # ...and it stays down for the hour rather than flapping straight back on.
    g.calls.clear()
    g._publish_thermal_forecast(now + timedelta(minutes=45))
    assert ("switch/turn_on", {"target": {"entity_id": sw}}) not in g.calls


def _run_heating(g, now, minutes, flow_at, ret_at, kw="5.2", hz="29"):
    from datetime import timedelta
    st = g.states
    st["sensor.loft_heatpump_controller_active_state"]["state"] = "Heating"
    st["switch.loft_heatpump_controller_water_flow_temperature_control_zone_1"]["state"] = "on"
    st["sensor.loft_heatpump_controller_compressor_operating_frequency"]["state"] = hz
    st["sensor.loft_heatpump_controller_ability_of_hydraulic_module"]["state"] = kw
    st["sensor.loft_heatpump_controller_water_flow"]["state"] = "1.0"
    for m in range(0, minutes + 1, 5):
        st["sensor.loft_heatpump_controller_total_water_outlet_temperature_t1"]["state"] = str(flow_at(m))
        st["sensor.loft_heatpump_controller_water_inlet_temperature"]["state"] = str(ret_at(m))
        g._publish_thermal_forecast(now + timedelta(minutes=m))
    return {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]


def test_radiators_arent_learned_while_the_system_is_still_warming_up(engine):
    g, _ = engine
    _seed_house(g, room_temp="18.8")
    now = g.get_now()
    before = None
    g._publish_thermal_forecast(now)
    before = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]["learned_emitter_w_at_dt50"]
    # Cold start: the water climbs 1 °C every 5 minutes the whole time.
    house = _run_heating(g, now, 30, lambda m: 14 + m / 5, lambda m: 10 + m / 5)
    assert house["learned_emitter_w_at_dt50"] == before


def test_radiators_are_learned_once_the_water_has_settled(engine):
    g, _ = engine
    _seed_house(g, room_temp="18.8")
    now = g.get_now()
    g._publish_thermal_forecast(now)
    before = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]["learned_emitter_w_at_dt50"]
    house = _run_heating(g, now, 40, lambda m: 37, lambda m: 32)
    assert house["learned_emitter_w_at_dt50"] != before


def test_recent_cop_comes_from_the_units_counters_not_its_lifetime_ratio(engine):
    g, _ = engine
    _seed_house(g)
    from datetime import timedelta
    st = g.states
    st["sensor.loft_heatpump_controller_power_output"] = {"state": "1000", "attributes": {}}
    st["sensor.loft_heatpump_controller_electricity_consumption"] = {"state": "300", "attributes": {}}
    now = g.get_now()
    g._publish_thermal_forecast(now)
    st["sensor.loft_heatpump_controller_power_output"]["state"] = "1050"
    st["sensor.loft_heatpump_controller_electricity_consumption"]["state"] = "310"
    g._publish_thermal_forecast(now + timedelta(days=2))
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    b = house["cop_breakdown"]
    assert b["week"] == 5.0                    # 50 kWh heat / 10 kWh electricity
    assert b["lifetime"] == round(1050 / 310, 2)


def test_an_impossible_run_cop_is_hidden_not_shown(engine):
    g, _ = engine
    _seed_house(g, room_temp="18.8")
    now = g.get_now()
    g.states["sensor.loft_heatpump_controller_outdoor_unit_current"]["state"] = "1"   # under-reads
    house = _run_heating(g, now, 40, lambda m: 31, lambda m: 26, kw="5.8")
    b = house["cop_breakdown"]
    assert b["run"] is None and b["run_unmeasurable"] is True


def test_an_under_reading_current_sensor_doesnt_shrink_the_heat_pump_load(engine):
    g, _ = engine
    _seed_house(g, room_temp="18.8")
    now = g.get_now()
    g.states["sensor.loft_heatpump_controller_outdoor_unit_current"]["state"] = "1"   # reads 1 A at 5.8 kW heat
    _run_heating(g, now, 10, lambda m: 31, lambda m: 26, kw="5.8")
    w = float(g.published["sensor.gridlock_heatpump_power"]["state"])
    assert w > 600, w            # the heat-output estimate, not 240 V x 1 A + pump (~310 W)


def test_a_believable_current_reading_is_still_used(engine):
    g, _ = engine
    _seed_house(g, room_temp="18.8")
    now = g.get_now()
    g.states["sensor.loft_heatpump_controller_outdoor_unit_current"]["state"] = "5"   # 1.2 kW + pump
    _run_heating(g, now, 10, lambda m: 31, lambda m: 26, kw="5.8")
    w = float(g.published["sensor.gridlock_heatpump_power"]["state"])
    assert abs(w - (240 * 5 + 70)) < 1, w


def test_the_battery_plan_gets_the_full_nightly_hot_water_fill(engine):
    g, _ = engine
    _seed_house(g)
    _seed_tank(g)
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"hot_water": {"legionella_enabled": False}}, f)        # once a night, 53 °C fill
    g.states["number.loft_heatpump_controller_set_dhw_tank_temperature_t5s"]["state"] = "45.0"
    g.states["sensor.loft_heatpump_controller_water_tank_temperature_t5"]["state"] = "31.0"
    g._publish_thermal_forecast(g.get_now())
    hw = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["Hot water"]
    d = hw["dhw"]
    assert d["daily_target"] == 53.0 and d["status"] == "waiting"
    from gridlock import HEATPUMP_POWER_ENTITY
    planned = sum(g.load_provider.planned_circuit_kwh[HEATPUMP_POWER_ENTITY].values())
    mass_kwh_per_c = 250 * 1.163 / 1000
    fill = (53 - 31) * mass_kwh_per_c / d["window_cop"]
    assert planned >= fill * 0.95, (planned, fill)     # not sized to the unit's own 45 °C


def test_the_battery_plan_gets_two_days_of_heating(engine):
    g, _ = engine
    _seed_house(g, room_temp="19.0")
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"target_temp": 20}, f)              # 4 °C outside: heating both days
    now = g.get_now()
    g._publish_thermal_forecast(now)
    from gridlock import HEATPUMP_POWER_ENTITY
    planned = g.load_provider.planned_circuit_kwh[HEATPUMP_POWER_ENTITY]
    first = g.load_provider.slot_key(g._slot_floor(now))
    day2 = sum(v for k, v in planned.items() if 48 <= k - first < 96)
    assert day2 > 1.0, day2
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert len(house["flow"]["plan"]) == 48          # the dashboard keeps its 24 h view


def test_the_house_target_follows_qings_thermostat(engine):
    g, _ = engine
    _seed_house(g, room_temp="18.5")
    with open(g._dashboard_path("gridwarm_settings.json"), "w") as f:
        json.dump({"target_temp": 19}, f)
    q = g.states["climate.bedroom_4_qing_thermo"]
    q["attributes"]["temperature"] = 20.5
    g._publish_thermal_forecast(g.get_now())
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["target_temp"] == 20.5 and house["target_editable"] is True
    # Thermostat switched off: the dashboard's own target stands in.
    q["state"] = "off"
    from datetime import timedelta
    g._publish_thermal_forecast(g.get_now() + timedelta(minutes=5))
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["target_temp"] == 19.0


def test_turning_qings_dial_replans_within_the_30s_watch(engine):
    g, _ = engine
    _seed_house(g, room_temp="18.5")
    q = g.states["climate.bedroom_4_qing_thermo"]
    q["attributes"]["temperature"] = 19.0
    g._publish_thermal_forecast(g.get_now())
    g._watch_dashboard_settings({})           # first look: remembers what's there
    q["attributes"]["temperature"] = 20.0      # the dial turned
    g._watch_dashboard_settings({})
    house = {z["name"]: z for z in g.published["sensor.gridlock_gridwarm"]["attributes"]["zones"]}["House"]
    assert house["target_temp"] == 20.0

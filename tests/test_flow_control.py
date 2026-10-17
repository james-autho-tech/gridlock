from core.thermal import ThermalParams
from core.flow_control import (emitter_output_w, mean_water_temp_for, implied_emitter_rating,
                               flow_setpoint_for_power, cop_estimate, implied_eta,
                               implied_thermal_mass, steady_state_loss_w, plan_heating,
                               decide_flow_command)


def _params(**o):
    d = dict(heat_loss_degrees=0.03, heat_loss_watts=100, heat_gain_static=200,
             heat_max_power=7000, heat_min_power=2000, heating_cop=3.5,
             thermal_mass_wh_per_c=8000)
    d.update(o)
    return ThermalParams(**d)


def test_emitter_curve_is_rated_at_dt50_and_zero_below_room_temp():
    assert emitter_output_w(70.0, 20.0, 10000) == 10000
    assert emitter_output_w(20.0, 20.0, 10000) == 0.0
    assert emitter_output_w(15.0, 20.0, 10000) == 0.0
    # (25/50)^1.3 ~ 0.406 -- radiators give up far less than half at half the dT
    assert 4000 < emitter_output_w(45.0, 20.0, 10000) < 4200


def test_mean_water_temp_inverts_the_emitter_curve():
    for p in (500, 2000, 6000):
        mean = mean_water_temp_for(p, 20.0, 10000)
        assert abs(emitter_output_w(mean, 20.0, 10000) - p) < 1e-6
    assert mean_water_temp_for(0, 20.0, 10000) == 20.0


def test_implied_emitter_rating_recovers_the_rating_from_a_live_reading():
    p = emitter_output_w(42.5, 20.0, 9000)
    assert abs(implied_emitter_rating(p, 42.5, 20.0) - 9000) < 1e-6
    assert implied_emitter_rating(3000, 23.0, 20.0) is None  # too flat to say


def test_flow_setpoint_adds_half_the_water_dt_clamps_and_rounds_up():
    mean = mean_water_temp_for(3000, 20.0, 10000)
    sp = flow_setpoint_for_power(3000, 20.0, 10000, water_dt=5.0, min_flow=30, max_flow=55, step=1.0)
    assert sp >= mean + 2.5
    assert sp - (mean + 2.5) < 1.0
    assert flow_setpoint_for_power(0, 20.0, 10000, min_flow=35, max_flow=55) == 35
    assert flow_setpoint_for_power(50000, 20.0, 10000, min_flow=35, max_flow=55) == 55


def test_cop_falls_with_flow_temperature_and_rises_with_outdoor_temperature():
    assert cop_estimate(35.0, 7.0) > cop_estimate(45.0, 7.0) > cop_estimate(55.0, 7.0)
    assert cop_estimate(40.0, 12.0) > cop_estimate(40.0, 0.0)
    assert 1.5 <= cop_estimate(60.0, -15.0) <= 6.0
    assert cop_estimate(21.0, 20.0) <= 6.0  # tiny lift can't blow up


def test_implied_eta_round_trips_through_cop_estimate():
    cop = cop_estimate(42.0, 5.0, eta=0.47)
    assert abs(implied_eta(cop, 42.0, 5.0) - 0.47) < 1e-9
    assert implied_eta(None, 42.0, 5.0) is None
    assert implied_eta(3.0, 22.0, 20.0) is None


def test_implied_thermal_mass_from_a_heating_interval():
    # 4 kW in, 1 kW lost, half an hour -> 1.5 kWh net into the fabric; room rose 0.25C
    assert abs(implied_thermal_mass(4000, 1000, 0.25, 0.5) - 6000) < 1e-6
    assert implied_thermal_mass(4000, 1000, 0.0, 0.5) is None
    assert implied_thermal_mass(500, 1000, 0.3, 0.5) is None


def test_steady_state_loss_matches_the_thermal_model():
    p = _params()
    # 0.03 C/h/C * 15C * 8000 Wh/C = 3600 W, + 100 W fixed, - 200 W gains
    assert abs(steady_state_loss_w(20.0, 5.0, p) - 3500.0) < 1e-6


def test_plan_holds_the_room_in_the_band_on_a_flat_tariff():
    p = _params()
    n = 48
    plan, status = plan_heating(20.0, [5.0] * n, [20.0] * n, [20.0] * n, p,
                                comfort_below=1.0, comfort_above=1.0)
    assert status in ("optimal", "fallback")
    assert len(plan) == n
    # Tracks the target, not the bottom of the band -- a flat tariff
    # gives nothing to exploit by letting the room drift.
    for s in plan:
        assert 19.7 <= s["temp"] <= 20.3
    # Roughly the steady-state loss each half hour (3.5 kW * 0.5 h)
    total = sum(s["heat_kwh"] for s in plan)
    assert 0.8 * 3.5 * 0.5 * n <= total <= 1.2 * 3.5 * 0.5 * n


def test_plan_shifts_heat_into_cheap_slots_when_mass_allows():
    # Big thermal mass, modest loss (UA = 0.0125 * 20000 = 250 W/K -> 3.75 kW at 15 K)
    p = _params(thermal_mass_wh_per_c=20000, heat_loss_degrees=0.0125)
    n = 48
    rates = [10.0] * n
    for i in range(16, 24):   # an expensive 4h block
        rates[i] = 40.0
    plan, status = plan_heating(20.0, [5.0] * n, rates, [20.0] * n, p,
                                comfort_below=1.0, comfort_above=1.0)
    if status != "optimal":
        return  # no solver on this machine -- the fallback is a hold, not a shift
    cheap_before = sum(s["heat_kwh"] for s in plan[8:16])
    expensive = sum(s["heat_kwh"] for s in plan[16:24])
    assert cheap_before > expensive
    # Pre-heats above target before the peak, coasts down through it, and
    # never leaves the comfort band.
    assert max(s["temp"] for s in plan[8:16]) > 20.2
    assert min(s["temp"] for s in plan[16:24]) < 19.9
    assert min(s["temp"] for s in plan[16:24]) >= 18.9


def test_plan_prefers_low_flow_heat_and_only_pushes_harder_when_the_price_gap_pays():
    p = _params()
    n = 8
    plan, status = plan_heating(20.0, [5.0] * n, [20.0] * n, [20.0] * n, p,
                                low_flow_power_w=3000, cop_low_curve=[4.0] * n,
                                cop_high_curve=[2.5] * n)
    if status != "optimal":
        return
    # Steady state needs 3.5 kW; low-flow covers 3 kW, so some high-flow heat
    # is unavoidable -- but the blended COP stays well above the high-flow one.
    assert all(s["cop"] > 3.0 for s in plan)


def test_plan_falls_back_gracefully_on_empty_input():
    assert plan_heating(20.0, [], [], [], _params()) == ([], "empty")


def test_decide_flow_command_hysteresis_and_rate_limit():
    # Off, small demand: stays off. Off, demand >= half of min output: on.
    assert decide_flow_command(500, False, 2000, None, 38)[0] is False
    assert decide_flow_command(1000, False, 2000, None, 38)[0] is True
    # On, demand drops below 20% of min output: off; between 20% and 50%: stays on.
    assert decide_flow_command(300, True, 2000, 38, 36)[0] is False
    assert decide_flow_command(700, True, 2000, 38, 36)[0] is True
    # Setpoint moves at most max_step per tick.
    assert decide_flow_command(5000, True, 2000, 38, 50, max_step=3)[1] == 41
    assert decide_flow_command(1000, True, 2000, 45, 35, max_step=3)[1] == 42
    assert decide_flow_command(1000, True, 2000, None, 35, max_step=3)[1] == 35

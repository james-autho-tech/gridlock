from core.thermal import ThermalParams
from core.flow_control import (heat_segments, emitter_output_w, mean_water_temp_for, implied_emitter_rating,
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
    assert 1.5 <= cop_estimate(60.0, -15.0) <= 3.0
    assert cop_estimate(21.0, 20.0) < 15.0  # tiny lift can't blow up
    # The gradient that makes low-and-slow pay: every 5 C of flow matters,
    # even on a mild day, instead of a hard ceiling flattening it.
    c25, c30, c35, c45 = (cop_estimate(f, 10.0) for f in (25.0, 30.0, 35.0, 45.0))
    assert c25 > c30 > c35 > c45
    assert 5.0 < c25 < 6.5 and 4.2 < c30 < 5.2 and 3.5 < c35 < 4.5 and 2.5 < c45 < 3.3


def test_implied_eta_round_trips_through_cop_estimate():
    cop = cop_estimate(42.0, 5.0, eta=0.47)
    assert abs(implied_eta(cop, 42.0, 5.0) - 0.47) < 1e-9
    assert implied_eta(None, 42.0, 5.0) is None
    assert implied_eta(3.0, 22.0, 20.0) is None
    assert implied_eta(20.0, 42.0, 5.0) is None   # above the parasitic limit


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
    assert max(s["temp"] for s in plan[8:16]) > 20.1
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


def test_plan_recovers_from_a_setback_by_ramping_not_bursting():
    # Room half a degree below target (about 4 kWh short): without
    # smoothing the cheapest plan slams a full-power slot to catch up;
    # with it, output ramps and the first slot is well under full power.
    p = _params(thermal_mass_wh_per_c=8000, heat_loss_degrees=0.03)
    n = 24
    burst, s1 = plan_heating(19.5, [5.0] * n, [20.0] * n, [20.0] * n, p,
                             smoothing_cost_per_kwh_change=0.0, heat_kwh_prev=0.0)
    smooth, s2 = plan_heating(19.5, [5.0] * n, [20.0] * n, [20.0] * n, p,
                              smoothing_cost_per_kwh_change=0.02, heat_kwh_prev=0.0)
    if s1 != "optimal" or s2 != "optimal":
        return
    max_slot_kwh = 7000 * 0.5 / 1000
    assert burst[0]["heat_kwh"] > 0.8 * max_slot_kwh
    assert smooth[0]["heat_kwh"] < 0.7 * max_slot_kwh
    # ...and the largest step between consecutive slots is smaller.
    step = lambda plan: max(abs(plan[i]["heat_kwh"] - plan[i - 1]["heat_kwh"]) for i in range(1, n))
    assert step(smooth) < step(burst)
    # Still gets the room back near target within the horizon.
    assert smooth[-1]["temp"] > 19.6


def test_solar_gain_curve_reduces_the_heat_the_plan_delivers():
    p = _params()
    n = 24
    base, s1 = plan_heating(20.0, [8.0] * n, [20.0] * n, [20.0] * n, p)
    sunny, s2 = plan_heating(20.0, [8.0] * n, [20.0] * n, [20.0] * n, p,
                             gain_curve_w=[1500.0] * n)
    assert sum(x["heat_kwh"] for x in sunny) < 0.6 * sum(x["heat_kwh"] for x in base)
    assert all(19.7 <= x["temp"] <= 20.3 for x in sunny)   # still on target, just needing less


def test_heat_segments_marginal_cop_falls_with_each_flow_step():
    segs = heat_segments(15.0, 10.0, 10000, 0.45, 25.0, 50.0)
    assert len(segs) == 6
    cops = [c for _, c in segs]
    assert all(a > b for a, b in zip(cops, cops[1:]))
    assert all(w > 0 for w, _ in segs)


def test_staircase_plan_runs_flat_instead_of_bursting():
    # A day's heat at 10 C outside: with the staircase the cheapest plan is
    # a steady trickle -- most slots in a narrow band, no block at high flow.
    p = _params(heat_loss_degrees=0.0125, thermal_mass_wh_per_c=8000, heat_gain_static=0.0, heat_loss_watts=0.0)
    n = 24
    segs = [heat_segments(15.0, 10.0, 10000, 0.45, 25.0, 50.0)] * n
    plan, status = plan_heating(15.0, [10.0] * n, [10.0] * n, [15.0] * n, p, segment_curve=segs,
                                heat_kwh_prev=0.25)
    if status != "optimal":
        return
    heats = [x["heat_kwh"] for x in plan]
    # 0.0125 * 8000 * 5 K = 500 W loss -> 0.25 kWh per slot, delivered evenly
    assert max(heats) - min(heats) < 0.1, heats
    assert all(14.5 <= x["temp"] <= 15.5 for x in plan)


def test_minimum_output_means_no_trickle_slots():
    p = _params(heat_loss_degrees=0.003, thermal_mass_wh_per_c=8000, heat_gain_static=0.0, heat_loss_watts=0.0)
    n = 24
    segs = [heat_segments(15.0, 10.0, 10000, 0.45, 25.0, 50.0)] * n
    plan, status = plan_heating(15.0, [10.0] * n, [10.0] * n, [15.0] * n, p, segment_curve=segs,
                                min_power_w=1500.0)
    if status != "optimal":
        return
    for x in plan:
        # 120 W loss: never a trickle -- off, or at least the radiators' full
        # output at 25 C flow (below the 1.5 kW minimum, so that's the floor).
        assert x["heat_kwh"] < 1e-6 or x["heat_kwh"] >= 0.35, x["heat_kwh"]


def test_integer_plan_falls_back_to_the_continuous_one_if_the_solver_gives_up():
    import pulp
    p = _params()
    n = 24
    segs = [heat_segments(15.0, 5.0, 10000, 0.45, 25.0, 50.0)] * n
    class GivesUp(pulp.PULP_CBC_CMD):
        def actualSolve(self, lp, **kw):
            if any(v.cat == pulp.LpInteger for v in lp.variables()):
                lp.status = pulp.LpStatusNotSolved
                return lp.status
            return super().actualSolve(lp, **kw)
    plan, status = plan_heating(15.0, [5.0] * n, [10.0] * n, [15.0] * n, p, segment_curve=segs,
                                min_power_w=2000.0, solver=GivesUp(msg=False))
    assert status == "optimal"
    assert sum(x["heat_kwh"] for x in plan) > 5.0     # still heats the house


def test_measured_thermal_and_electric_power_and_run_cop():
    from core.flow_control import measured_thermal_w, measured_electric_w, run_cop
    assert abs(measured_thermal_w(1.3, 26.0, 23.0) - 4535) < 5      # matches the unit's own ~4.5 kW
    assert measured_thermal_w(0.0, 30.0, 25.0) is None
    assert measured_electric_w(240.0, 4.0) == 1030.0
    assert abs(run_cop(4000.0, 1000.0) - 4.0) < 1e-9
    assert run_cop(4000.0, 100.0) is None      # too small to trust
    assert run_cop(10000.0, 1000.0) is None    # implausible


def test_blocked_slots_get_no_space_heat_and_the_plan_works_around_them():
    p = _params()
    n = 12
    segs = [heat_segments(18.0, 5.0, 10000, 0.45, 25.0, 50.0)] * n
    plan, status = plan_heating(18.0, [5.0] * n, [10.0] * n, [18.0] * n, p, segment_curve=segs,
                                blocked_slots=[0, 1, 2, 3])
    assert all(plan[i]["heat_kwh"] < 1e-6 for i in range(4))
    assert sum(x["heat_kwh"] for x in plan[4:]) > 1.0   # catches up once the tank's done

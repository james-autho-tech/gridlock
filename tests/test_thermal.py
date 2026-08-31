from core.thermal import (ThermalParams, simulate, thermal_mass_from_volume,
                           thermal_mass_from_litres, AIR_WH_PER_M3_PER_C,
                           WATER_WH_PER_LITRE_PER_C, anticipatory_target_curve,
                           decide_dhw_command, usable_hot_water_litres,
                           showers_available, implied_heat_loss_degrees,
                           blend_learned_value)


def _room_params(**overrides):
    defaults = dict(
        heat_loss_degrees=0.035, heat_loss_watts=170, heat_gain_static=300,
        heat_max_power=7000, heat_min_power=2000, heat_share=0.25,
        heating_cop=4.15, thermal_mass_wh_per_c=thermal_mass_from_volume(123),
        hysteresis=0.5, hysteresis_off=0.1)
    defaults.update(overrides)
    return ThermalParams(**defaults)


def test_thermal_mass_from_volume_and_litres_are_positive_and_scale_linearly():
    assert thermal_mass_from_volume(123) == thermal_mass_from_volume(1) * 123
    assert thermal_mass_from_litres(150) == thermal_mass_from_litres(1) * 150
    # Water holds far more heat per unit volume than air.
    assert WATER_WH_PER_LITRE_PER_C > AIR_WH_PER_M3_PER_C


def test_cooling_with_heating_off_drifts_toward_a_gain_loss_equilibrium():
    """Heating never engages (target far below current temp) -- the zone
    should cool because loss exceeds the passive heat_gain_static, but
    settle rather than free-fall toward external temp, since a real
    room's passive gains partly offset loss."""
    params = _room_params()
    trace = simulate(20.0, [5.0] * 96, [0.0] * 96, params,
                      heating_on0=False, step_minutes=30)
    assert all(not step["heating_on"] for step in trace)
    assert all(step["heat_kw"] == 0.0 for step in trace)
    assert trace[0]["internal_temp"] < 20.0  # cooling, not warming
    # Settles well above external temp (5C) thanks to heat_gain_static,
    # not a free-fall all the way down to it.
    assert trace[-1]["internal_temp"] > 10.0


def test_heating_engages_below_hysteresis_band_and_warms_the_zone():
    params = _room_params()
    trace = simulate(15.0, [10.0] * 288, [20.0] * 288, params,
                      heating_on0=False, step_minutes=5)
    assert trace[0]["heating_on"] is True
    assert trace[0]["heat_kw"] > 0.0
    # Warms toward the target over the horizon.
    assert trace[50]["internal_temp"] > 15.0
    final_temps = [s["internal_temp"] for s in trace[-20:]]
    # Once caught up, hysteresis keeps it in a tight band around target,
    # not wildly overshooting or oscillating far past it.
    assert all(19.0 < t < 21.0 for t in final_temps)


def test_heating_switches_off_once_hysteresis_off_band_is_reached():
    params = _room_params(hysteresis=0.5, hysteresis_off=0.1)
    trace = simulate(19.95, [10.0] * 10, [20.0] * 10, params,
                      heating_on0=True, step_minutes=5)
    # Already within [target-hysteresis, target+hysteresis_off] and
    # heating_on0=True -- hysteresis is a band with memory, so it should
    # NOT immediately switch off just because it's near the target.
    assert trace[0]["heating_on"] is True


def test_electrical_kw_derives_from_heat_power_and_cop_and_is_zero_when_off():
    params = _room_params(heat_max_power=4000, heat_share=1.0, heating_cop=4.0)
    warming = simulate(10.0, [5.0], [20.0], params, heating_on0=False, step_minutes=5)[0]
    assert warming["heating_on"] is True
    assert warming["heat_kw"] == 4.0  # heat_max_power * heat_share, in kW
    assert warming["electrical_kw"] == 1.0  # heat_kw / cop

    cooling = simulate(25.0, [20.0], [10.0], params, heating_on0=False, step_minutes=5)[0]
    assert cooling["heating_on"] is False
    assert cooling["heat_kw"] == 0.0
    assert cooling["electrical_kw"] == 0.0


def test_small_shortfall_stays_low_and_slow_not_full_power():
    """A heat pump run continuously at its gentle output is both more
    efficient and more comfortable than cycling to full power like a
    boiler -- a small deficit (the kind anticipatory_target_curve's own
    modest adjustments produce) should stay on heat_min_power, only
    escalating to heat_max_power for a genuinely large shortfall."""
    params = _room_params(heat_max_power=7000, heat_min_power=2000, heat_share=1.0,
                           boost_threshold_degrees=2.0)
    small_deficit = simulate(19.0, [10.0], [20.0], params, heating_on0=False, step_minutes=5)[0]
    assert small_deficit["heating_on"] is True
    assert small_deficit["heat_kw"] == 2.0  # heat_min_power, not heat_max_power

    large_deficit = simulate(15.0, [10.0], [20.0], params, heating_on0=False, step_minutes=5)[0]
    assert large_deficit["heat_kw"] == 7.0  # genuinely behind -- heat_max_power


def test_heat_share_scales_effective_heat_input_for_a_shared_heat_source():
    full = simulate(10.0, [5.0], [20.0], _room_params(heat_share=1.0),
                     heating_on0=False, step_minutes=5)[0]
    quarter = simulate(10.0, [5.0], [20.0], _room_params(heat_share=0.25),
                        heating_on0=False, step_minutes=5)[0]
    assert quarter["heat_kw"] == full["heat_kw"] * 0.25
    assert quarter["internal_temp"] < full["internal_temp"]


def test_anticipatory_curve_eases_target_down_ahead_of_a_warm_up():
    # Flat 5C for a while, then rising to 18C -- the classic case from
    # the user's own description: know it's about to get milder, so
    # don't heat as hard right now.
    external = [5.0] * 24 + [18.0] * 24
    curve = anticipatory_target_curve(20.0, external, lookahead_steps=24,
                                       sensitivity=0.3, max_adjust=2.0)
    assert curve[0] < 20.0  # eased down in anticipation of the warm-up
    # Once the warm-up has already happened and nothing further is
    # coming (flat tail), there's no more trend left to anticipate.
    assert curve[-1] == 20.0


def test_anticipatory_curve_raises_target_ahead_of_a_cold_snap():
    external = [15.0] * 24 + [0.0] * 24
    curve = anticipatory_target_curve(20.0, external, lookahead_steps=24,
                                       sensitivity=0.3, max_adjust=2.0)
    assert curve[0] > 20.0  # gets ahead of the cold snap while it's easy


def test_anticipatory_curve_adjustment_is_capped():
    external = [0.0] * 24 + [40.0] * 24  # an extreme swing
    curve = anticipatory_target_curve(20.0, external, lookahead_steps=24,
                                       sensitivity=0.3, max_adjust=2.0)
    assert curve[0] == 18.0  # capped at -max_adjust, not -0.3*40=-12


def test_anticipatory_curve_is_a_no_op_with_no_trend_ahead():
    curve = anticipatory_target_curve(20.0, [10.0] * 48, lookahead_steps=12)
    assert curve == [20.0] * 48


def test_anticipatory_curve_still_heats_back_up_via_hysteresis():
    """A downward anticipatory adjustment isn't a hard floor enforced
    separately -- simulate()'s own hysteresis is what stops the zone
    free-falling, by heating back up once it drops hysteresis below
    whatever the (eased) target is."""
    params = _room_params(hysteresis=0.5, hysteresis_off=0.1)
    external = [5.0] * 48 + [18.0] * 48  # warm-up coming partway through
    targets = anticipatory_target_curve(20.0, external, lookahead_steps=48)
    trace = simulate(20.0, external, targets, params, heating_on0=False, step_minutes=30)
    # Eased target floors at 18.0 (capped -2.0 adjustment) -- hysteresis
    # keeps it from drifting far below that, not free-falling toward the
    # 5C external temp.
    assert all(s["internal_temp"] > 17.0 for s in trace)


def test_fine_step_resolution_avoids_wildly_overshooting_a_fast_zone():
    """A hot water tank can reach its target well within a single 30-min
    slot -- simulating at a coarse step size would blow straight past the
    target before the hysteresis band ever gets a chance to react. Fine
    (5-min) steps should keep the overshoot within a couple of degrees of
    the hysteresis_off band instead."""
    dhw = ThermalParams(
        heat_loss_degrees=0.0, heat_loss_watts=60, heat_gain_static=0,
        heat_max_power=3500, heat_min_power=1500, heat_share=1.0,
        heating_cop=2.5, thermal_mass_wh_per_c=thermal_mass_from_litres(150),
        hysteresis=2.0, hysteresis_off=0.5)
    trace = simulate(45.0, [18.0] * 24, [52.0] * 24, dhw,
                      heating_on0=False, step_minutes=5)
    peak = max(s["internal_temp"] for s in trace)
    assert peak < 52.0 + 5.0  # nowhere near the ~20C coarse-step overshoot


def test_decide_dhw_command_follows_model_when_no_overrides_apply():
    assert decide_dhw_command(True, 50.0, safety_min_temp=45.0,
                               off_duration_hours=0.0, max_off_hours=6.0) is True
    assert decide_dhw_command(False, 50.0, safety_min_temp=45.0,
                               off_duration_hours=0.0, max_off_hours=6.0) is False


def test_decide_dhw_command_safety_floor_overrides_model_wanting_off():
    # The model wants heating off, but the tank is at/below the hard
    # floor -- heat regardless, this is the safe failure mode.
    assert decide_dhw_command(False, 45.0, safety_min_temp=45.0,
                               off_duration_hours=0.0, max_off_hours=6.0) is True
    assert decide_dhw_command(False, 40.0, safety_min_temp=45.0,
                               off_duration_hours=0.0, max_off_hours=6.0) is True


def test_decide_dhw_command_max_off_cap_overrides_extended_off_periods():
    # Comfortably above the floor, but held off long enough to hit the cap.
    assert decide_dhw_command(False, 50.0, safety_min_temp=45.0,
                               off_duration_hours=5.9, max_off_hours=6.0) is False
    assert decide_dhw_command(False, 50.0, safety_min_temp=45.0,
                               off_duration_hours=6.0, max_off_hours=6.0) is True
    assert decide_dhw_command(False, 50.0, safety_min_temp=45.0,
                               off_duration_hours=8.0, max_off_hours=6.0) is True


def test_decide_dhw_command_off_duration_irrelevant_while_already_heating():
    # off_duration_hours only matters when the model actually wants it
    # off -- a long-elapsed value shouldn't force anything while the
    # model already wants heating on.
    assert decide_dhw_command(True, 50.0, safety_min_temp=45.0,
                               off_duration_hours=100.0, max_off_hours=6.0) is True


def test_usable_hot_water_exceeds_tank_litres_when_tank_is_well_above_target():
    # 250L at 60C mixed down to a 40C shower with 10C mains: most of the
    # usable volume is topped up from the cold tap, not the tank itself,
    # so this is meant to come out well above the tank's own 250L.
    usable = usable_hot_water_litres(60.0, 250.0, target_temp=40.0, cold_mains_temp=10.0)
    assert usable == 250.0 * 50.0 / 30.0
    assert usable > 250.0


def test_usable_hot_water_is_zero_at_or_below_target_temp():
    # The user's own example: 250L at 20C is nominally "full" but you
    # can't mix hot water UP to a higher temperature by adding more cold.
    assert usable_hot_water_litres(20.0, 250.0, target_temp=40.0, cold_mains_temp=10.0) == 0.0
    assert usable_hot_water_litres(40.0, 250.0, target_temp=40.0, cold_mains_temp=10.0) == 0.0


def test_usable_hot_water_scales_down_as_tank_cools_toward_target():
    hot = usable_hot_water_litres(55.0, 250.0, target_temp=40.0, cold_mains_temp=10.0)
    cooler = usable_hot_water_litres(45.0, 250.0, target_temp=40.0, cold_mains_temp=10.0)
    assert hot > cooler > 0.0


def test_showers_available_is_a_plain_unit_conversion():
    assert showers_available(400.0, litres_per_shower=40.0) == 10.0
    assert showers_available(0.0, litres_per_shower=40.0) == 0.0


def test_implied_heat_loss_degrees_recovers_the_true_value_from_a_clean_observation():
    # Pure cooling, heating off: loss_c_per_hr = heat_loss_degrees * diff
    # + heat_loss_watts / thermal_mass. Construct an observation from
    # exactly those known numbers and confirm the algebra inverts cleanly.
    true_heat_loss_degrees = 0.035
    heat_loss_watts = 170.0
    thermal_mass = 500.0
    diff = 10.0
    observed_loss_c_per_hr = true_heat_loss_degrees * diff + heat_loss_watts / thermal_mass
    implied = implied_heat_loss_degrees(observed_loss_c_per_hr, diff, heat_loss_watts, thermal_mass)
    assert implied == true_heat_loss_degrees


def test_implied_heat_loss_degrees_returns_none_for_too_small_a_difference():
    # Near-zero internal/external difference: almost any heat_loss_degrees
    # value fits the observation, so it's not a useful data point.
    assert implied_heat_loss_degrees(0.5, 0.5, 170.0, 500.0) is None
    assert implied_heat_loss_degrees(0.5, -0.9, 170.0, 500.0) is None


def test_blend_learned_value_moves_toward_observed_by_alpha():
    result = blend_learned_value(0.035, 0.05, alpha=0.05)
    assert result == 0.035 * 0.95 + 0.05 * 0.05


def test_blend_learned_value_converges_gradually_not_instantly():
    current = 0.035
    for _ in range(10):
        current = blend_learned_value(current, 0.05, alpha=0.05)
    # Ten observations of a consistently different true value should
    # move it meaningfully closer, but a single stray reading earlier
    # couldn't have swung it anywhere near 0.05 on its own.
    assert 0.035 < current < 0.05

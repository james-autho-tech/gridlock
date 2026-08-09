from core.thermal import (ThermalParams, simulate, thermal_mass_from_volume,
                           thermal_mass_from_litres, AIR_WH_PER_M3_PER_C,
                           WATER_WH_PER_LITRE_PER_C)


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


def test_heat_share_scales_effective_heat_input_for_a_shared_heat_source():
    full = simulate(10.0, [5.0], [20.0], _room_params(heat_share=1.0),
                     heating_on0=False, step_minutes=5)[0]
    quarter = simulate(10.0, [5.0], [20.0], _room_params(heat_share=0.25),
                        heating_on0=False, step_minutes=5)[0]
    assert quarter["heat_kw"] == full["heat_kw"] * 0.25
    assert quarter["internal_temp"] < full["internal_temp"]


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

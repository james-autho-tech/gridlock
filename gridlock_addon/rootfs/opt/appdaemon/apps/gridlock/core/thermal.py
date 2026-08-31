"""Thermal model — predicts a heated zone's temperature trajectory and the
electrical cost of keeping it there, from a simple heat-loss/heat-input
physics model. Phase 1: advisory only. Nothing in this module, or anything
that calls it, ever writes to a climate/heating entity — it only forecasts,
for display alongside the actual measured temperature.

One zone shape, `simulate()`, covers two real uses:
- A room, heated by a shared heat pump whose thermal output varies with
  outdoor temperature — heat is lost toward the (changing) outdoor
  temperature, and only a fraction of the heat pump's total output
  actually reaches any one room when several rooms share it (`heat_share`).
- A hot water tank, heated by the same heat pump's separate DHW circuit —
  heat is lost toward a roughly-constant indoor ambient temperature
  instead of the outdoors, and gets the heat pump's full output while
  it's actively running in DHW mode (`heat_share=1.0`).

Both are just "a heated mass loses heat toward some reference temperature,
and gains heat whenever the heat source is running, gated by a
target-temperature hysteresis band" — parameterised differently, not two
separate models.

Heat loss is split into two independently-measurable terms, both taken
directly from the site's own real numbers rather than invented here:
- `heat_loss_degrees`: °C lost per hour per °C of (internal - external)
  difference — the dominant term, derived by timing an actual
  heating-off cooldown, so it's already in the temperature domain and
  needs no thermal-mass conversion at all.
- `heat_loss_watts`: a smaller, non-diff-scaled fixed loss (background
  ventilation/inefficiency), which — unlike the term above — is a power
  figure and does need `thermal_mass_wh_per_c` to convert into a
  temperature effect.
"""

from dataclasses import dataclass

AIR_WH_PER_M3_PER_C = 1200.0 / 3600.0  # volumetric heat capacity of air
WATER_WH_PER_LITRE_PER_C = 4186.0 / 3600.0  # specific heat of water


@dataclass
class ThermalParams:
    heat_loss_degrees: float
    heat_loss_watts: float = 0.0
    heat_gain_static: float = 0.0
    heat_max_power: float = 0.0
    heat_min_power: float = 0.0
    heat_share: float = 1.0
    heating_cop: float = 3.0
    thermal_mass_wh_per_c: float = 500.0
    hysteresis: float = 0.5
    hysteresis_off: float = 0.1
    boost_threshold_degrees: float = 2.0


def thermal_mass_from_volume(volume_m3, fabric_factor=4.0):
    """A room's effective thermal mass is dominated by its furniture,
    walls and floor, not just the air in it — `fabric_factor` (Wh per m3
    per C) is a rough typical-construction multiplier over plain air, not
    a measured figure. Meant as a starting default only: refine by
    comparing the dashboard's predicted-vs-actual temperature curve."""
    return volume_m3 * fabric_factor


def thermal_mass_from_litres(litres):
    """Water's specific heat is a real physical constant — unlike a
    room, a tank's thermal mass doesn't need a fudge factor, just its
    capacity in litres (~1kg/litre)."""
    return litres * WATER_WH_PER_LITRE_PER_C


def usable_hot_water_litres(tank_temp, tank_litres, target_temp, cold_mains_temp=10.0):
    """Litres of target_temp water obtainable by mixing the tank's full
    contents down with cold mains — deliberately NOT just tank_litres.
    A 250L tank at 60C mixed down to a 40C shower gives MORE than 250L of
    usable water (250 * (60-10)/(40-10) = ~417L), since most of that
    volume is topped up from the cold tap, not the tank itself. A tank at
    or below target_temp gives zero: you can't mix hot water UP to a
    higher temperature by adding cold. Simple energy-conservation mixing
    (Vh*Th + Vc*Tc)/(Vh+Vc) = target, solved for total usable volume —
    same water-physics reasoning as thermal_mass_from_litres above, no
    fudge factor needed."""
    if tank_temp <= target_temp or target_temp <= cold_mains_temp:
        return 0.0
    return tank_litres * (tank_temp - cold_mains_temp) / (target_temp - cold_mains_temp)


def showers_available(usable_litres, litres_per_shower=40.0):
    """A plain unit conversion — litres_per_shower is a rough UK-average
    default (~8 minutes at ~5L/min), meant to be overridden with a site's
    own real figure if known."""
    return usable_litres / litres_per_shower if litres_per_shower > 0 else 0.0


def anticipatory_target_curve(base_target, external_temp_curve, lookahead_steps=36,
                               sensitivity=0.3, max_adjust=2.0):
    """A plain weather-compensation curve only ever reacts to the
    CURRENT outdoor temperature. This looks ahead instead: if it's
    about to get milder, ease the target down now (passive warming
    will do some of the work, so less active heating is needed to stay
    comfortable); if it's about to turn colder, nudge the target up now
    (get ahead of the cold snap while it's still easy/efficient,
    instead of playing catch-up once already cold).

    This adjusts the target this model works from, not a literal flow
    temperature -- the model has no separate flow-temp variable to
    simulate -- but the real effect is the same: heat less now if
    warmth is coming, a bit more now if cold is coming.

    external_temp_curve: forecast, one entry per simulate() step.
    lookahead_steps: how far ahead to look for the trend (in steps, not
    minutes -- the caller's step_minutes decides what that means).
    sensitivity: °C of target adjustment per °C of anticipated swing.
    max_adjust: caps the adjustment so a big forecast swing can't push
    the target further than this in either direction.
    """
    n = len(external_temp_curve)
    out = []
    for i in range(n):
        future_i = min(i + lookahead_steps, n - 1)
        trend = external_temp_curve[future_i] - external_temp_curve[i]
        adjust = max(-max_adjust, min(max_adjust, -sensitivity * trend))
        out.append(base_target + adjust)
    return out


def simulate(internal_temp0, external_temp_curve, target_temp_curve, params,
             heating_on0=False, step_minutes=30):
    """Step the zone forward one entry per (external_temp, target_temp)
    pair. Returns a list of dicts, one per step:
    {internal_temp, external_temp, target_temp, heating_on, heat_kw,
    electrical_kw} — internal_temp/heat_kw/electrical_kw are this step's
    predicted values, not the input to it (so trace[0] is one step ahead
    of internal_temp0, matching how the battery SoC trace already works).
    """
    n = min(len(external_temp_curve), len(target_temp_curve))
    hours_per_step = step_minutes / 60.0
    temp = internal_temp0
    heating_on = heating_on0
    out = []
    for i in range(n):
        ext = external_temp_curve[i]
        target = target_temp_curve[i]

        # Hysteresis band around the target -- switches on once hysteresis
        # below it, off once hysteresis_off above it, matching a real
        # thermostat's on/off deadband rather than an idealised instant
        # on/off exactly at the setpoint.
        if temp <= target - params.hysteresis:
            heating_on = True
        elif temp >= target + params.hysteresis_off:
            heating_on = False

        if heating_on:
            # Low and slow by default -- a heat pump run continuously at
            # its gentle output level is both more efficient (better COP
            # at partial load) and more comfortable than cycling to full
            # power and back like a boiler. Only escalate to heat_max_power
            # once genuinely behind (a big shortfall, e.g. after being off
            # a while, or a real cold snap) -- the small adjustments
            # anticipatory_target_curve makes shouldn't trigger it.
            heat_power_w = (params.heat_max_power
                             if (target - temp) > params.boost_threshold_degrees
                             else params.heat_min_power) * params.heat_share
        else:
            heat_power_w = 0.0

        diff = temp - ext
        loss_c_per_hr = (params.heat_loss_degrees * diff
                          + params.heat_loss_watts / params.thermal_mass_wh_per_c)
        gain_c_per_hr = (heat_power_w + params.heat_gain_static) / params.thermal_mass_wh_per_c
        temp = temp + (gain_c_per_hr - loss_c_per_hr) * hours_per_step

        out.append({
            "internal_temp": temp,
            "external_temp": ext,
            "target_temp": target,
            "heating_on": heating_on,
            "heat_kw": heat_power_w / 1000.0,
            "electrical_kw": heat_power_w / params.heating_cop / 1000.0,
        })
    return out


def implied_heat_loss_degrees(observed_loss_c_per_hr, diff, heat_loss_watts,
                               thermal_mass_wh_per_c):
    """Solve heat_loss_degrees from one real observation of an actual
    cooling period (heating off the whole interval): the realised
    cooling rate, and the actual internal-minus-external temperature
    difference over it, with the OTHER loss term (heat_loss_watts, a
    fixed figure not being learned here) held constant. Same algebra as
    simulate()'s own loss formula, solved in reverse -- this is exactly
    the "time an actual heating-off cooldown" calibration method
    described at the top of this module, run continuously against real
    data instead of once by hand.

    Returns None when diff is too small to solve reliably -- a near-zero
    internal/external difference makes almost any heat_loss_degrees
    value fit the observation equally well, so it's not a useful data
    point to learn from (not an error, just not informative)."""
    if abs(diff) < 1.0:
        return None
    return (observed_loss_c_per_hr - heat_loss_watts / thermal_mass_wh_per_c) / diff


def blend_learned_value(current, observed, alpha=0.05):
    """EMA blend toward one newly observed value -- same 95%/5% per-
    observation blend as the load forecast's own learned profile
    (core/forecast.py), so a single noisy reading can't swing a learned
    parameter on its own, but a real, consistent drift in reality shows
    up within a couple of weeks rather than never at all."""
    return current * (1 - alpha) + observed * alpha


def fit_heat_loss_params(observations, thermal_mass_wh_per_c):
    """Separates heat_loss_degrees and heat_loss_watts properly, unlike
    implied_heat_loss_degrees above (which solves one equation per point
    with the other term assumed fixed, silently absorbing any error in
    that "fixed" term into the one being solved for). Ordinary least-
    squares line fit over many real cooling-period observations:
    rate = heat_loss_degrees * diff + (heat_loss_watts / thermal_mass)
    -- slope is heat_loss_degrees, intercept converts to heat_loss_watts.

    observations: list of {"diff", "rate"} pairs (see
    implied_heat_loss_degrees for what these mean) accumulated over
    many real cooling periods.

    Returns None below 8 observations (too few to fit two parameters
    reliably) or when the diffs are too clustered together (under 2C of
    spread) -- a fit is mathematically possible but not meaningfully
    constrained without genuinely different conditions to fit against,
    e.g. every observation happening to come from similar mild nights.
    Returns (heat_loss_degrees, heat_loss_watts) otherwise."""
    n = len(observations)
    if n < 8:
        return None
    xs = [o["diff"] for o in observations]
    ys = [o["rate"] for o in observations]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    variance_x = sum((x - mean_x) ** 2 for x in xs) / n
    if variance_x < 4.0:  # std dev < 2C of spread across observations
        return None
    covariance_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / n
    heat_loss_degrees = covariance_xy / variance_x
    intercept = mean_y - heat_loss_degrees * mean_x
    heat_loss_watts = intercept * thermal_mass_wh_per_c
    return heat_loss_degrees, heat_loss_watts


def decide_dhw_command(desired_on, current_temp, safety_min_temp, off_duration_hours,
                        max_off_hours):
    """The actual on/off command to send to real hardware, layering two
    safety overrides on top of the model's own desired_on decision
    (simulate()'s first-step heating_on, already anticipation-aware).
    Both overrides bias toward heating rather than withholding it, since
    commanding heat ON is always the safe failure mode here -- holding
    it OFF is not:

    - safety_min_temp: a hard floor -- never withhold heat below this,
      regardless of what the model or plan wants.
    - max_off_hours: caps how long this can continuously hold heating
      off, as a conservative margin against unconfirmed interactions
      with the heat pump's own internal cycles (e.g. anti-legionella
      disinfection, which runs on its own separate schedule/logic this
      model has no visibility into) -- better to occasionally heat
      "unnecessarily" than to risk silently suppressing a safety cycle
      for an extended, indefinite stretch.
    """
    if current_temp <= safety_min_temp:
        return True
    if not desired_on and off_duration_hours >= max_off_hours:
        return True
    return desired_on

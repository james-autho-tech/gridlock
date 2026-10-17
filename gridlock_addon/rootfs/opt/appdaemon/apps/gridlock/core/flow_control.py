"""Dynamic flow-temperature control for a heat pump's space heating — the
part that turns "keep the house at 20°C as cheaply as possible" into a
flow-temperature setpoint, the way a Homely-style controller does.

Why flow temperature and not on/off: a heat pump's efficiency (COP) is
set almost entirely by how hot it has to make the water. Every degree
of flow temperature it doesn't need costs roughly 2-3% of COP, and
cycling on and off at a fixed high flow temperature is the worst of
both worlds (high flow temperature, plus start-up losses). So the
controller's job each tick is: work out how much heat the house needs
over the next few hours, deliver it at the LOWEST flow temperature the
radiators can give it up at, and -- on a time-of-use tariff -- shift
some of that heat into cheap half-hours when the house's own thermal
mass makes that worthwhile after the COP penalty of pushing harder.

Three pieces, all pure functions with no HA knowledge:

1. An emitter (radiator) curve: how many watts the radiators give up at
   a given mean water temperature above room temperature. Standard
   radiator physics -- output scales with (mean water - room)^1.3,
   rated at a 50°C difference ("ΔT50", what every radiator datasheet
   quotes). The one site-specific number, rated_w_at_dt50, is learned
   from the heat pump's own measured thermal output while running.

2. A COP model: Carnot efficiency scaled by a real-world factor (eta),
   so the plan knows that heat delivered at 45°C flow on a 0°C day
   costs more electricity per kWh than heat at 35°C flow on a 10°C
   day. eta is learned from the heat pump's own live COP sensor.

3. A plan: a small linear programme over the next day or two of
   half-hour slots -- how much heat to deliver in each slot to keep the
   room inside a comfort band around the target at least cost (price /
   COP), given the forecast outdoor temperature and the house's learned
   heat loss and thermal mass. Heat in each slot is split into a cheap
   "low flow" portion and a dearer "high flow" portion so the plan only
   pre-heats in a cheap slot when the saving beats the COP it gives up.
   The plan's first slot is what the controller delivers right now; it
   re-plans every tick, so it's model-predictive control in the plain
   sense: optimise the whole horizon, act on the first step, repeat.
"""

import math

from core import thermal as core_thermal

EMITTER_EXPONENT = 1.3
KELVIN = 273.15

# Comfort-band violations are allowed (the LP must always have a
# solution -- a cold snap the heat pump can't keep up with is a real
# possibility) but priced far above any plausible energy cost, so the
# plan only ever breaches the band when it genuinely can't help it.
COMFORT_PENALTY_PER_DEGREE_SLOT = 100.0

# Inside the band, every °C the room sits away from the target costs
# this much (£ per °C per slot) in the objective. Without it the plan
# would park the house at the bottom of the band for ever -- that's
# where losses are lowest -- which is a thermostat set 1°C low, not
# comfort flex. With it, the plan tracks the target on a flat tariff
# and only dips (or pre-heats) when the price difference it exploits is
# worth more than the deviation: for a typical house that's a real
# peak/off-peak gap, not a fraction of a penny.
COMFORT_COST_PER_DEGREE_SLOT = 0.02

# Heat pumps like low and slow: a long gentle run at low flow beats
# bursts at high flow (better COP at part load, no start-up losses, no
# cycling). The COP split above already prices gentle heat below hard
# heat, but after a setback the comfort cost alone would still reward
# recovering fast. This charges every kWh of change in heat output
# between consecutive slots, so the plan ramps rather than steps. £ per
# kWh of change -- about half a typical kWh-of-heat cost, enough to
# smooth, not enough to stop a genuine shift into a cheap slot.
SMOOTHING_COST_PER_KWH_CHANGE = 0.02

# How hard the plan tracks the target inside the band (£ per °C per
# slot). "cop": nearly free to drift anywhere in the band to catch the
# warmest hours and the lowest flow -- the band edges are what keep the
# house heated. "comfort": hugs the target. Leaving the band is always
# penalised heavily (COMFORT_PENALTY_PER_DEGREE_SLOT) whatever this is.
COMFORT_COST_BY_PRIORITY = {"cop": 0.005, "balanced": COMFORT_COST_PER_DEGREE_SLOT, "comfort": 0.05}


def emitter_output_w(mean_water_temp, room_temp, rated_w_at_dt50, exponent=EMITTER_EXPONENT):
    """Radiator output at a given mean water temperature. Zero at or
    below room temperature -- a radiator can't heat a room it's cooler
    than."""
    dt = mean_water_temp - room_temp
    if dt <= 0 or rated_w_at_dt50 <= 0:
        return 0.0
    return rated_w_at_dt50 * (dt / 50.0) ** exponent


def mean_water_temp_for(power_w, room_temp, rated_w_at_dt50, exponent=EMITTER_EXPONENT):
    """The emitter curve inverted: the mean water temperature needed to
    give up power_w into a room at room_temp."""
    if power_w <= 0 or rated_w_at_dt50 <= 0:
        return room_temp
    return room_temp + 50.0 * (power_w / rated_w_at_dt50) ** (1.0 / exponent)


def implied_emitter_rating(thermal_power_w, mean_water_temp, room_temp, exponent=EMITTER_EXPONENT):
    """One observation of rated_w_at_dt50 from the heat pump's measured
    thermal output while running steadily: the emitter curve solved for
    its rating. None when the water isn't meaningfully warmer than the
    room (the curve is too flat there to say anything) or the output
    reading is nonsense."""
    dt = mean_water_temp - room_temp
    if dt < 5.0 or thermal_power_w <= 0:
        return None
    return thermal_power_w / (dt / 50.0) ** exponent


def flow_setpoint_for_power(power_w, room_temp, rated_w_at_dt50, water_dt=5.0,
                            min_flow=30.0, max_flow=55.0, step=1.0):
    """Flow (outlet) setpoint that delivers power_w: the mean water
    temperature the emitter curve asks for, plus half the flow/return
    split (the outlet runs water_dt/2 above the mean), clamped to the
    allowed range and rounded UP to the setpoint's step -- rounding down
    would under-deliver by design."""
    mean = mean_water_temp_for(power_w, room_temp, rated_w_at_dt50)
    flow = mean + water_dt / 2.0
    flow = min(max_flow, max(min_flow, flow))
    if step > 0:
        flow = math.ceil(flow / step - 1e-9) * step
    return min(max_flow, flow)


def cop_estimate(flow_temp, outdoor_temp, eta=0.45, floor=1.5, ceiling=6.0):
    """Carnot heating COP (T_hot / (T_hot - T_cold), in kelvin) scaled by
    eta, the fraction of the ideal a real machine achieves -- 0.4-0.5
    for a decent air-source unit, and learned from the live COP sensor
    where one exists (learn_eta). Clamped to a plausible range so a tiny
    flow/outdoor difference can't produce an absurd figure."""
    lift = max(2.0, flow_temp - outdoor_temp)
    cop = eta * (flow_temp + KELVIN) / lift
    return min(ceiling, max(floor, cop))


def implied_eta(live_cop, flow_temp, outdoor_temp):
    """cop_estimate solved for eta from one live reading. None when the
    reading can't be trusted (the unit idle, a tiny lift)."""
    lift = flow_temp - outdoor_temp
    if live_cop is None or live_cop <= 0.5 or lift < 5.0:
        return None
    return live_cop * lift / (flow_temp + KELVIN)


def implied_thermal_mass(thermal_power_w, loss_w, temp_rise_c, elapsed_hours):
    """Wh per °C from one heating interval: the net heat that went into
    the fabric divided by how much the room warmed. None when the room
    didn't warm measurably (the maths would divide by noise) or the
    interval is odd."""
    if elapsed_hours <= 0 or temp_rise_c < 0.1:
        return None
    net_wh = (thermal_power_w - loss_w) * elapsed_hours
    if net_wh <= 0:
        return None
    return net_wh / temp_rise_c


def steady_state_loss_w(internal_temp, external_temp, params):
    """Heat the house is losing right now, in watts -- the thermal
    model's own loss formula (core/thermal.py simulate) converted back
    from °C/h to W via the thermal mass, net of static gains."""
    loss_c_per_hr = (params.heat_loss_degrees * (internal_temp - external_temp)
                     + params.heat_loss_watts / params.thermal_mass_wh_per_c)
    return loss_c_per_hr * params.thermal_mass_wh_per_c - params.heat_gain_static


def plan_heating(temp0, external_curve, rate_curve, target_curve, params, *,
                 comfort_below=1.0, comfort_above=1.0, slot_hours=0.5,
                 low_flow_power_w=None, cop_low_curve=None, cop_high_curve=None,
                 max_power_w=None, solver=None,
                 comfort_cost_per_degree_slot=COMFORT_COST_PER_DEGREE_SLOT,
                 smoothing_cost_per_kwh_change=SMOOTHING_COST_PER_KWH_CHANGE,
                 heat_kwh_prev=None):
    """The heating plan: heat to deliver per slot (kWh) over the horizon
    at least cost, keeping the room inside [target - comfort_below,
    target + comfort_above]. Linear programme (PuLP) -- see the module
    docstring for why it's shaped this way.

    external_curve / rate_curve (p/kWh electricity) / target_curve: one
    entry per slot. cop_low_curve / cop_high_curve: the COP expected
    for heat delivered gently (at the lowest flow temperature, up to
    low_flow_power_w) and for heat delivered pushing harder (higher
    flow) in each slot -- the split is what lets the plan weigh
    "pre-heat now while it's cheap" against the COP that costs.

    Returns a list of per-slot dicts {heat_kwh, temp, price, cop, cost}
    plus a status string. Falls back to a plain steady-state plan if the
    solver isn't available or fails, so the controller never has
    nothing to act on."""
    n = min(len(external_curve), len(rate_curve), len(target_curve))
    if n == 0:
        return [], "empty"
    mass = max(params.thermal_mass_wh_per_c, 1.0)
    max_power_w = max_power_w if max_power_w is not None else (params.heat_max_power or 7000.0)
    low_w = low_flow_power_w if low_flow_power_w is not None else max_power_w
    low_w = min(max(low_w, 0.0), max_power_w)
    cop_low_curve = cop_low_curve or [params.heating_cop] * n
    cop_high_curve = cop_high_curve or [params.heating_cop * 0.8] * n

    def fallback():
        out = []
        temp = temp0
        for i in range(n):
            loss_w = steady_state_loss_w(temp, external_curve[i], params)
            # Hold the target: loss plus a gentle correction toward it.
            need_w = loss_w + (target_curve[i] - temp) * mass / max(slot_hours, 1e-6) * 0.5
            need_w = min(max_power_w, max(0.0, need_w))
            temp = temp + ((need_w - loss_w) * slot_hours) / mass
            kwh = need_w * slot_hours / 1000.0
            cop = cop_low_curve[i] if need_w <= low_w else cop_high_curve[i]
            out.append({"heat_kwh": kwh, "temp": temp, "price": rate_curve[i], "cop": cop,
                        "cost": kwh / max(cop, 0.1) * rate_curve[i] / 100.0})
        return out, "fallback"

    try:
        import pulp  # noqa: PLC0415 — optional at import time, same as core/optimizer.py
    except ImportError:
        return fallback()

    try:
        prob = pulp.LpProblem("gridwarm_heating", pulp.LpMinimize)
        q_lo = [pulp.LpVariable(f"q_lo_{i}", 0, low_w * slot_hours / 1000.0) for i in range(n)]
        q_hi = [pulp.LpVariable(f"q_hi_{i}", 0, max(0.0, (max_power_w - low_w)) * slot_hours / 1000.0)
                for i in range(n)]
        temp = [pulp.LpVariable(f"temp_{i}") for i in range(n)]
        under = [pulp.LpVariable(f"under_{i}", 0) for i in range(n)]
        over = [pulp.LpVariable(f"over_{i}", 0) for i in range(n)]
        dev_lo = [pulp.LpVariable(f"dev_lo_{i}", 0) for i in range(n)]
        dev_hi = [pulp.LpVariable(f"dev_hi_{i}", 0) for i in range(n)]
        ramp = [pulp.LpVariable(f"ramp_{i}", 0) for i in range(n)]
        cost_terms = []
        prev = temp0
        for i in range(n):
            # Dynamics, linear in the previous slot's temperature:
            # T_i = T_{i-1} + (heat_in - loss) * h / mass, with the loss
            # evaluated at T_{i-1} (explicit Euler, same as simulate()).
            loss_w_expr = (params.heat_loss_degrees * (prev - external_curve[i]) * mass
                           + params.heat_loss_watts - params.heat_gain_static)
            heat_w_expr = (q_lo[i] + q_hi[i]) * 1000.0 / slot_hours
            prob += temp[i] == prev + (heat_w_expr - loss_w_expr) * slot_hours / mass
            prob += temp[i] >= target_curve[i] - comfort_below - under[i]
            prob += temp[i] <= target_curve[i] + comfort_above + over[i]
            prob += dev_lo[i] >= target_curve[i] - temp[i]
            prob += dev_hi[i] >= temp[i] - target_curve[i]
            price = rate_curve[i] / 100.0  # p -> £
            cost_terms.append(q_lo[i] / max(cop_low_curve[i], 0.1) * price)
            cost_terms.append(q_hi[i] / max(cop_high_curve[i], 0.1) * price)
            cost_terms.append((under[i] + over[i]) * COMFORT_PENALTY_PER_DEGREE_SLOT)
            cost_terms.append((dev_lo[i] + dev_hi[i]) * comfort_cost_per_degree_slot)
            # |heat_i - heat_{i-1}| <= ramp_i, charged in the objective.
            # heat_kwh_prev anchors slot 0 to what's being delivered now,
            # so a re-plan doesn't itself cause a step.
            q_i = q_lo[i] + q_hi[i]
            q_prev = (q_lo[i - 1] + q_hi[i - 1]) if i > 0 else heat_kwh_prev
            if q_prev is not None:
                prob += ramp[i] >= q_i - q_prev
                prob += ramp[i] >= q_prev - q_i
                cost_terms.append(ramp[i] * smoothing_cost_per_kwh_change)
            prev = temp[i]
        # Don't let the plan coast to the bottom of the band right at the
        # horizon's end just because nothing after it is priced.
        prob += temp[n - 1] >= target_curve[n - 1] - under[n - 1]
        prob += pulp.lpSum(cost_terms)
        prob.solve(solver or pulp.PULP_CBC_CMD(msg=False, timeLimit=10))
        if pulp.LpStatus[prob.status] not in ("Optimal", "Not Solved"):
            return fallback()
        out = []
        for i in range(n):
            lo = pulp.value(q_lo[i]) or 0.0
            hi = pulp.value(q_hi[i]) or 0.0
            kwh = lo + hi
            cop = ((lo * cop_low_curve[i] + hi * cop_high_curve[i]) / kwh) if kwh > 1e-9 else cop_low_curve[i]
            out.append({"heat_kwh": kwh, "temp": pulp.value(temp[i]) or temp0,
                        "price": rate_curve[i], "cop": cop,
                        "cost": kwh / max(cop, 0.1) * rate_curve[i] / 100.0})
        return out, "optimal"
    except Exception:  # noqa: BLE001 — the controller must always have a plan to act on
        return fallback()


def decide_flow_command(required_w, currently_on, min_power_w, prev_setpoint, new_setpoint,
                        max_step=3.0, on_fraction=0.5, off_fraction=0.2):
    """Turn a required thermal power into (heating_on, flow_setpoint),
    with the two guards a real heat pump needs:

    - Hysteresis on demand, so the compressor isn't started for a
      trickle it can't modulate down to: start once the house needs at
      least on_fraction of the unit's minimum output, stop only once it
      needs less than off_fraction of it. Between the two, leave it as
      it is.
    - Rate-limited setpoint moves (max_step °C per tick), so one noisy
      reading can't swing the flow temperature 15°C and provoke a
      defrost/overshoot cycle; the plan is re-run every tick anyway, so
      a genuine change still arrives within a few ticks.

    prev_setpoint None means no previous command -- the new setpoint is
    applied as-is."""
    min_power_w = max(min_power_w, 1.0)
    if currently_on:
        heating_on = required_w > min_power_w * off_fraction
    else:
        heating_on = required_w >= min_power_w * on_fraction
    if prev_setpoint is None or max_step <= 0:
        setpoint = new_setpoint
    else:
        setpoint = prev_setpoint + max(-max_step, min(max_step, new_setpoint - prev_setpoint))
    return heating_on, setpoint


blend = core_thermal.blend_learned_value

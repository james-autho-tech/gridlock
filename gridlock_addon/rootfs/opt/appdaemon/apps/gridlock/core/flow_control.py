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


# The part of a heat pump's electricity that doesn't scale with lift --
# circulation pump, fan, controller, defrost -- expressed as the COP the
# machine would have if the compressor were free. It's what stops the
# Carnot curve shooting to absurd figures at small lifts, and what gives
# the planner a REAL, monotonic reward for every degree of flow it can
# shave off: with a hard ceiling instead, 25 °C and 30 °C flow scored
# the same on a mild day and the plan saw no reason to prefer the
# lower, so it ramped instead of running low and slow.
COP_PARASITIC = 15.0


def cop_estimate(flow_temp, outdoor_temp, eta=0.45, floor=1.5, parasitic=COP_PARASITIC):
    """Heating COP: Carnot (T_hot / (T_hot - T_cold), kelvin) scaled by
    eta, the fraction of the ideal the compressor achieves (0.4-0.5 for
    a decent air-source unit), combined in series with a fixed parasitic
    loss (1/COP = 1/COP_carnot + 1/parasitic). For eta 0.45 at 10 °C
    outside that gives roughly 5.6 at 25 °C flow, 4.7 at 30, 4.0 at 35,
    2.9 at 45 -- the shape real units publish."""
    lift = max(2.0, flow_temp - outdoor_temp)
    carnot = eta * (flow_temp + KELVIN) / lift
    cop = 1.0 / (1.0 / carnot + 1.0 / parasitic)
    return max(floor, cop)


def implied_eta(live_cop, flow_temp, outdoor_temp, parasitic=COP_PARASITIC):
    """cop_estimate solved for eta from one live reading. None when the
    reading can't be trusted (the unit idle, a tiny lift, or a COP at or
    above the parasitic limit, which no eta can explain)."""
    lift = flow_temp - outdoor_temp
    if live_cop is None or live_cop <= 0.5 or lift < 5.0 or live_cop >= parasitic:
        return None
    carnot = 1.0 / (1.0 / live_cop - 1.0 / parasitic)
    return carnot * lift / (flow_temp + KELVIN)


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


def heating_solver(time_limit=8, gap=0.02):
    """Solver for the heating plan: the system GLPK where installed (the
    add-on image), else PuLP's bundled CBC -- with a time cap and a small
    optimality gap, because the minimum-output binaries make it an integer
    problem and a plan 2% from perfect now beats a perfect one late."""
    import shutil
    import pulp  # noqa: PLC0415
    path = shutil.which("glpsol")
    if path:
        return pulp.GLPK_CMD(path=path, msg=False, timeLimit=time_limit, options=["--mipgap", str(gap)])
    return pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, gapRel=gap)


def heat_segments(room_temp, outdoor_temp, rated_w_at_dt50, eta, flow_min, flow_max,
                  water_dt=5.0, step=5.0, max_power_w=None):
    """The heat pump's cost of heat as a staircase, one step per flow
    temperature: [(extra_watts, marginal_cop), ...]. Step k is the extra
    output going from flow f_{k-1} to f_k gives, priced at the COP that
    makes the electricity add up: (P_k - P_{k-1}) / (P_k/COP_k -
    P_{k-1}/COP_{k-1}). Marginal COP falls step by step, so the cheapest
    way to deliver a day's heat is the flattest -- low and slow -- and the
    planner sees exactly what each extra degree of flow costs, rather than
    a two-tier cliff that made bursts look as good as a steady trickle."""
    flows = []
    f = flow_min
    while f < flow_max - 1e-9:
        flows.append(f)
        f += step
    flows.append(flow_max)
    segs = []
    prev_p, prev_e = 0.0, 0.0
    for f in flows:
        p = emitter_output_w(f - water_dt / 2.0, room_temp, rated_w_at_dt50)
        if max_power_w is not None:
            p = min(p, max_power_w)
        if p <= prev_p + 1.0:
            continue
        e = p / cop_estimate(f, outdoor_temp, eta)
        marginal = (p - prev_p) / max(e - prev_e, 1e-6)
        segs.append((p - prev_p, max(marginal, 0.5)))
        prev_p, prev_e = p, e
    return segs


def plan_heating(temp0, external_curve, rate_curve, target_curve, params, *,
                 comfort_below=1.0, comfort_above=1.0, slot_hours=0.5,
                 low_flow_power_w=None, cop_low_curve=None, cop_high_curve=None,
                 max_power_w=None, solver=None,
                 comfort_cost_per_degree_slot=COMFORT_COST_PER_DEGREE_SLOT,
                 smoothing_cost_per_kwh_change=SMOOTHING_COST_PER_KWH_CHANGE,
                 heat_kwh_prev=None, gain_curve_w=None, segment_curve=None, min_power_w=None,
                 blocked_slots=None, floor_curve=None, ceiling_curve=None):
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
    # Passive gain per slot (W) on top of the static figure -- sunshine
    # through the windows, from the PV forecast. A sunny day can supply
    # the whole loss of a mild afternoon; without this the plan heats
    # houses that are warming themselves.
    gains = [params.heat_gain_static + (gain_curve_w[i] if gain_curve_w and i < len(gain_curve_w) else 0.0)
             for i in range(n)]
    mass = max(params.thermal_mass_wh_per_c, 1.0)
    max_power_w = max_power_w if max_power_w is not None else (params.heat_max_power or 7000.0)
    low_w = low_flow_power_w if low_flow_power_w is not None else max_power_w
    low_w = min(max(low_w, 0.0), max_power_w)
    cop_low_curve = cop_low_curve or [params.heating_cop] * n
    cop_high_curve = cop_high_curve or [params.heating_cop * 0.8] * n

    # Slots the heat pump is busy elsewhere (the hot water run -- the unit
    # does one or the other, tank first): no space heat can be planned there.
    blocked = set(i for i in (blocked_slots or []) if 0 <= i < n)

    def fallback():
        out = []
        temp = temp0
        for i in range(n):
            loss_w = steady_state_loss_w(temp, external_curve[i], params) - (gains[i] - params.heat_gain_static)
            # Hold the target: loss plus a gentle correction toward it.
            need_w = loss_w + (target_curve[i] - temp) * mass / max(slot_hours, 1e-6) * 0.5
            need_w = 0.0 if i in blocked else min(max_power_w, max(0.0, need_w))
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
        # Per-slot heat staircase: either the full per-flow staircase
        # (segment_curve, see heat_segments) or the legacy two tiers.
        if segment_curve:
            segs = [segment_curve[i] if i < len(segment_curve) and segment_curve[i]
                    else [(max_power_w, cop_low_curve[i])] for i in range(n)]
        else:
            segs = [[(low_w, cop_low_curve[i]), (max(0.0, max_power_w - low_w), cop_high_curve[i])]
                    for i in range(n)]
        q = [[pulp.LpVariable(f"q_{i}_{k}", 0, w * slot_hours / 1000.0) for k, (w, _) in enumerate(segs[i])]
             for i in range(n)]
        q_lo = [pulp.lpSum(q[i]) for i in range(n)]
        q_hi = [0 for _ in range(n)]
        for i in blocked:
            prob += q_lo[i] == 0
        # The compressor can't deliver a trickle: each slot is either off
        # or at least its minimum output (or everything the radiators can
        # take, if that's less). A binary per slot -- 48 of them, well
        # within the solver's time cap.
        if min_power_w:
            for i in range(n):
                cap_kwh = sum(w for w, _ in segs[i]) * slot_hours / 1000.0
                floor_kwh = min(min_power_w * slot_hours / 1000.0, cap_kwh)
                on = pulp.LpVariable(f"on_{i}", cat="Binary")
                prob += q_lo[i] <= cap_kwh * on
                prob += q_lo[i] >= floor_kwh * on
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
                           + params.heat_loss_watts - gains[i])
            heat_w_expr = (q_lo[i] + q_hi[i]) * 1000.0 / slot_hours
            prob += temp[i] == prev + (heat_w_expr - loss_w_expr) * slot_hours / mass
            # floor_curve: a per-slot minimum above the band's own floor --
            # "warm again by 06:00" after a night setback.
            band_floor = target_curve[i] - comfort_below
            if floor_curve and i < len(floor_curve) and floor_curve[i] is not None:
                band_floor = max(band_floor, floor_curve[i])
            prob += temp[i] >= band_floor - under[i]
            # ceiling_curve: a per-slot upper limit in place of the band's
            # own -- through a night setback the ceiling stays at the day
            # level, so the house can be warmed gradually ahead of the
            # morning instead of in a rush once the window ends.
            ceiling = target_curve[i] + comfort_above
            if ceiling_curve and i < len(ceiling_curve) and ceiling_curve[i] is not None:
                ceiling = ceiling_curve[i]
            prob += temp[i] <= ceiling + over[i]
            prob += dev_lo[i] >= target_curve[i] - temp[i]
            prob += dev_hi[i] >= temp[i] - target_curve[i]
            price = rate_curve[i] / 100.0  # p -> £
            for k, (_, seg_cop) in enumerate(segs[i]):
                cost_terms.append(q[i][k] / max(seg_cop, 0.1) * price)
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
        solved = pulp.LpStatus[prob.status] == "Optimal" and all(
            pulp.value(v) is not None for row in q for v in row)
        if not solved:
            if min_power_w:
                # The integer version didn't finish cleanly (timed out on a
                # slow box, say) -- never let that become "no heating":
                # re-plan without the minimum-output constraint.
                return plan_heating(temp0, external_curve, rate_curve, target_curve, params,
                                    comfort_below=comfort_below, comfort_above=comfort_above,
                                    slot_hours=slot_hours, low_flow_power_w=low_flow_power_w,
                                    cop_low_curve=cop_low_curve, cop_high_curve=cop_high_curve,
                                    max_power_w=max_power_w, solver=solver,
                                    comfort_cost_per_degree_slot=comfort_cost_per_degree_slot,
                                    smoothing_cost_per_kwh_change=smoothing_cost_per_kwh_change,
                                    heat_kwh_prev=heat_kwh_prev, gain_curve_w=gain_curve_w,
                                    segment_curve=segment_curve, min_power_w=None,
                                    blocked_slots=blocked_slots, floor_curve=floor_curve,
                                    ceiling_curve=ceiling_curve)
            return fallback()
        out = []
        for i in range(n):
            parts = [(pulp.value(q[i][k]) or 0.0, segs[i][k][1]) for k in range(len(segs[i]))]
            kwh = sum(v for v, _ in parts)
            elec = sum(v / max(c, 0.1) for v, c in parts)
            cop = (kwh / elec) if elec > 1e-9 else segs[i][0][1]
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



WATER_WH_PER_M3_PER_C = 1162.8   # 1000 kg/m3 x 4186 J/kgK / 3600


def measured_thermal_w(flow_m3h, flow_temp, return_temp):
    """Heat actually delivered: water flow x (flow - return) x water's heat
    capacity. None when any reading is missing or the flow has stopped."""
    if flow_m3h is None or flow_temp is None or return_temp is None or flow_m3h <= 0.05:
        return None
    return max(0.0, flow_m3h * WATER_WH_PER_M3_PER_C * (flow_temp - return_temp))


def measured_electric_w(voltage, current, pump_w=70.0):
    """Electricity in: the outdoor unit's V x A, plus the indoor pump and
    controller (not on the outdoor unit's meter)."""
    if voltage is None or current is None:
        return None
    return max(0.0, voltage * current) + pump_w


def run_cop(thermal_wh, electric_wh, min_electric_wh=200.0):
    """A run's measured COP, or None if it's too small to trust or the
    figure is implausible (a sensor dropout, a defrost misread)."""
    if electric_wh < min_electric_wh:
        return None
    cop = thermal_wh / electric_wh
    return cop if 1.0 < cop < 8.0 else None

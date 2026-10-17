"""What a kWh of electricity for heating really costs in each slot, given
the battery plan — the price the heat pump planners should see.

The import tariff is the wrong number for a house with a battery sized to
carry its heating. At 17:00 on a peak rate the battery is covering the
load with energy it bought at the overnight rate, so an extra kWh for
the heat pump costs the overnight rate plus losses and wear — unless the
plan expects the battery to run flat before the next cheap window (then
the extra kWh is really imported later, at that slot's rate), or to be
exporting (then it forgoes the export price). GridLock's own LP already
decides all of that per slot, so this reads the answer off the plan
rather than re-deriving it: a plain marginal-cost walk over the plan's
per-slot grid import, charge, battery discharge and export figures.

With a battery that covers the heating, the result is flat for most of
the day — and a flat price means the heating plan simply minimises
electricity, i.e. maximises COP, which is the point.
"""

EPS_KWH = 0.005


def battery_energy_cost_p(slots, cost_trace, cheap_rate_gbp, efficiency, degradation_gbp_per_kwh):
    """p/kWh a kWh out of the battery costs: the energy-weighted price the
    plan pays to charge it from the grid (falling back to the configured
    cheap rate), grossed up for round-trip losses, plus wear."""
    n = min(len(slots), len(cost_trace))
    kwh = 0.0
    spend = 0.0
    for i in range(n):
        charged = cost_trace[i].get("charge_in", 0.0) or 0.0
        if charged > EPS_KWH and (cost_trace[i].get("grid_in", 0.0) or 0.0) > EPS_KWH:
            kwh += charged
            spend += charged * slots[i]["imp"]
    avg_gbp = (spend / kwh) if kwh > EPS_KWH else cheap_rate_gbp
    eff = efficiency if 0.3 < efficiency <= 1.0 else 0.9
    return (avg_gbp / eff + max(0.0, degradation_gbp_per_kwh)) * 100.0


def effective_prices_p(slots, cost_trace, battery_cost_p):
    """Marginal p/kWh of extra load in each slot of the plan. slots carry
    imp/exp in £/kWh and the plan's export kWh; cost_trace carries
    grid_in / charge_in / battery_kwh per slot (core/optimizer.py)."""
    n = min(len(slots), len(cost_trace))
    imp = [slots[i]["imp"] * 100.0 for i in range(n)]
    exp = [slots[i]["exp"] * 100.0 for i in range(n)]
    grid_in = [cost_trace[i].get("grid_in", 0.0) or 0.0 for i in range(n)]
    charge = [cost_trace[i].get("charge_in", 0.0) or 0.0 for i in range(n)]
    batt = [cost_trace[i].get("battery_kwh", 0.0) or 0.0 for i in range(n)]
    export_kwh = [slots[i].get("export", 0.0) or 0.0 for i in range(n)]
    # For each slot, the rate of the first later slot that imports for
    # load before the battery is next charged -- that's where an extra
    # kWh taken from the battery now actually gets paid for.
    next_import = [None] * n
    pending = None
    for i in range(n - 1, -1, -1):
        next_import[i] = pending
        if charge[i] > EPS_KWH:
            pending = None          # a recharge resets the chain
        elif grid_in[i] > EPS_KWH:
            pending = imp[i]
    out = []
    for i in range(n):
        if grid_in[i] > EPS_KWH:
            price = imp[i]                      # importing anyway: extra load imports more
        elif export_kwh[i] > EPS_KWH:
            price = max(exp[i], battery_cost_p if batt[i] > EPS_KWH else 0.0)
        elif batt[i] > EPS_KWH:
            later = next_import[i]
            price = max(battery_cost_p, later) if later is not None else battery_cost_p
        else:
            price = battery_cost_p              # PV covering load; less PV to the battery
        out.append(price)
    return out

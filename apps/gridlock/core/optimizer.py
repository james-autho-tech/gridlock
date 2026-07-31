"""The LP optimiser — replaces the old hill-climbing simulate()/optimise()
pair with a genuine linear program (PuLP) over the whole horizon, solved
jointly rather than searched slot-by-slot.

Per slot the LP has two "decision" variables that map straight onto the
plan table's Action column — charge (grid->battery) and batt_export
(battery->grid) — plus five auxiliary flow variables (pv_to_load,
pv_to_batt, pv_to_grid, batt_to_load, grid_to_load) whose job is just to
express "PV serves load first, then charges the battery or gets sold,
self-consumption discharges to cover what's left" as *linear* balance
constraints instead of the old code's if/elif branches. The LP arrives at
the same greedy priority order on its own wherever that's actually
cost-optimal (serving load from free PV/battery is always at least as
cheap as importing), so it isn't hard-coded here — the objective
coefficients are what enforce it, and that's exactly what the "only
export when profitable" unit tests are checking.
"""

import math
import shutil
from dataclasses import dataclass, field

import pulp

from .config import Mode

EPS = 1e-6


def _val(var, default=0.0):
    """pulp.value(var) or 0.0 looks like a safe fallback but isn't one:
    `nan` is truthy in Python, so `float('nan') or 0.0` evaluates to
    `nan`, not 0.0 — a stray NaN from the solver (observed in practice on
    a genuinely tight/degenerate solve) then slips straight through into
    the published plan, and a NaN inside a value HA stores as an entity
    attribute can end up silently dropped rather than serialized,
    shortening that row and misaligning every column after it. Guard
    explicitly against both None and non-finite values instead."""
    v = pulp.value(var)
    if v is None or not math.isfinite(v):
        return default
    return v

# Penalty (£ per kWh of shortfall) on the on-peak reserve slack — see
# _solve_lp's reserve constraint below. Must dominate any realistic
# combination of import/export rate and degradation cost so the solver
# only ever dips into it when the reserve target is genuinely
# unreachable (battery already too depleted for the rest of a peak
# stretch, no more off-peak charging opportunity beforehand) — at that
# point the shortfall is physical reality, not a planning failure, and
# forcing the LP to report "no solution" instead of just importing more
# than planned would be strictly worse for a live battery controller.
RESERVE_PENALTY = 1000.0

# Big-M coefficient unlocking pv_to_grid once its slot's battery_full
# binary is 1 — see the PV-routing-priority constraint in _solve_lp.
# Only needs to dominate a realistic per-slot PV export (a few kW over a
# half-hour slot), not the objective's price coefficients, so a fixed
# constant unrelated to battery size or tariff rates is fine.
PV_ROUTING_BIG_M = 1.0e5


@dataclass
class PlanResult:
    slots: list           # input slots, with "charge"/"export" filled in
    trace: list            # SoC % after each slot
    cost_trace: list         # [{"delta","total","grid_in","charge_in"}, ...]
    grid_cost: float          # real import/export £ only (no degradation)
    total_cost: float          # objective value incl. degradation weighting
    infeasible: bool = False


def action(slot):
    if slot["charge"] > EPS:
        return "CHARGE"
    if slot["export"] > EPS:
        return "EXPORT"
    return "ECO"


def _solver():
    """Prefer a system GLPK binary (musl-safe on the Alpine add-on image;
    apk-installed, not PuLP's bundled glibc CBC) — falls back to PuLP's
    default bundled solver, which is fine on regular glibc dev/CI
    machines and the separate Debian-based AppDaemon add-on."""
    path = shutil.which("glpsol")
    if path:
        return pulp.GLPK_CMD(path=path, msg=False)
    return pulp.PULP_CBC_CMD(msg=False)


def _solve_lp(slots, soc0_kwh, cfg, *, export_cap_override=None):
    """One LP solve. export_cap_override: optional {slot_index: kwh} that
    overrides the normal per-slot battery-export upper bound — used by
    solve() for the balanced-mode daily-target cutoff and the
    min-export-block cleanup, both of which need a second, constrained
    pass rather than hand-editing the first pass's solved values (that
    would silently break the energy-balance equalities)."""
    n = len(slots)
    eff = cfg.efficiency
    cap = cfg.battery_kwh
    floor_kwh = cfg.floor_soc / 100.0 * cap
    max_c = cfg.charge_kw / 2.0
    max_d = cfg.discharge_kw / 2.0
    max_d_exp = cfg.export_rate_kw / 2.0
    degradation = 0.0 if cfg.mode == Mode.MAX_PROFIT else cfg.degradation
    # "Full enough to export" tolerance — a fixed fraction of capacity
    # rather than an absolute kWh so it scales sensibly across battery
    # sizes; see the PV-routing-priority constraint below.
    full_tol_kwh = max(0.01, 0.001 * cap)

    prob = pulp.LpProblem("gridlock", pulp.LpMinimize)

    charge = [pulp.LpVariable(f"charge_{i}", 0, max_c) for i in range(n)]
    pv_to_load = [pulp.LpVariable(f"pv_load_{i}", 0) for i in range(n)]
    pv_to_batt = [pulp.LpVariable(f"pv_batt_{i}", 0) for i in range(n)]
    pv_to_grid = [pulp.LpVariable(f"pv_grid_{i}", 0) for i in range(n)]
    batt_to_load = [pulp.LpVariable(f"batt_load_{i}", 0, max_d) for i in range(n)]
    grid_to_load = [pulp.LpVariable(f"grid_load_{i}", 0) for i in range(n)]
    soc = [pulp.LpVariable(f"soc_{i}", floor_kwh, cap) for i in range(n)]

    export_ub = [0.0 if cfg.mode == Mode.ECO else max_d_exp for _ in range(n)]
    if export_cap_override:
        for i, ub in export_cap_override.items():
            export_ub[i] = min(export_ub[i], max(0.0, ub))
    batt_to_export = [pulp.LpVariable(f"batt_exp_{i}", 0, export_ub[i]) for i in range(n)]

    grid_cost_terms = []
    degradation_terms = []
    reserve_penalty_terms = []

    for i, s in enumerate(slots):
        pv, load, imp, exp = s["pv"], s["load"], s["imp"], s["exp"]

        prob += pv_to_load[i] + pv_to_batt[i] + pv_to_grid[i] == pv
        prob += pv_to_load[i] + eff * batt_to_load[i] + grid_to_load[i] == load
        prob += batt_to_load[i] + batt_to_export[i] <= max_d

        # Hardware PV-routing priority: in self-consumption mode the
        # inverter's own firmware always routes surplus PV into the
        # battery until it's full before any of it is allowed to export
        # — that's fixed hardware behaviour, not an economic choice the
        # LP gets to make. A plain Big-M gate on soc[i] alone doesn't
        # work here: whenever soc[i] sits below the "full" threshold, the
        # gate's RHS goes negative, which conflicts with pv_to_grid's own
        # >=0 bound and makes the *entire* solve infeasible rather than
        # just disallowing export (confirmed — the first version of this
        # constraint broke every existing fixture that wasn't already at
        # 100% SoC). This is a genuine complementarity condition
        # (pv_to_grid > 0 implies headroom == 0), which a continuous LP
        # can't express — it needs a binary indicator, making this one
        # constraint per slot a small MILP rather than a pure LP. battery_full[i]
        # is free to be 0 even when soc[i] happens to be at cap (that
        # just leaves pv_to_grid gated shut in a case where it didn't
        # need to be — harmless), but it can only be 1 when soc[i] is
        # genuinely within full_tol_kwh of capacity, so the solver can't
        # cheat its way to unlocking export without actually filling the
        # battery first — there's nowhere else for the surplus PV to go
        # (pv_to_load + pv_to_batt + pv_to_grid == pv, no "waste PV"
        # variable exists), so it's forced into pv_to_batt instead.
        battery_full = pulp.LpVariable(f"batt_full_{i}", cat="Binary")
        prob += soc[i] >= (cap - full_tol_kwh) * battery_full
        prob += pv_to_grid[i] <= PV_ROUTING_BIG_M * battery_full

        prev = soc0_kwh if i == 0 else soc[i - 1]
        prob += soc[i] == prev + eff * (charge[i] + pv_to_batt[i]) \
            - (batt_to_load[i] + batt_to_export[i])

        # Grid-charging only in a genuinely cheap/off-peak/dispatch slot —
        # a hard rule (upper bound 0), not just a cost discouragement.
        if imp > cfg.cheap_rate:
            prob += charge[i] == 0
        else:
            # Off-peak, symmetric case: don't drain the battery for THIS
            # slot's own load either. Battery self-consumption only costs
            # degradation in the objective, and degradation is very often
            # *less* than a genuinely cheap import rate (e.g. 5p vs 10p) —
            # so left alone, the LP happily cycles the battery here for a
            # few pence of "savings" that are actually a pure loss: that
            # charge came from the grid at this same cheap rate a slot or
            # two ago (or will be topped up at it again shortly), so
            # routing this slot's load through the battery instead of
            # importing it directly adds a real round-trip efficiency
            # loss and degradation cost for zero benefit — importing
            # fresh at the same cheap rate is strictly cheaper than
            # cycling stored charge to avoid paying that exact same rate.
            # PV still serves load first regardless (pv_to_load is
            # untouched) — this only blocks the battery's own discharge.
            prob += batt_to_load[i] == 0

        # On-peak reserve: don't let this slot's export/self-consumption
        # eat into the SoC the rest of the current peak stretch needs to
        # reach the next off-peak window without hitting the floor.
        # future_deficit is "load beyond this slot, up to the next cheap
        # slot" (remaining_deficit already includes this slot's own
        # deficit, so subtract it back out) — same formula the old
        # heuristic used for its export_cap. Soft (a slack variable
        # absorbs any gap, heavily penalised in the objective) rather
        # than a hard inequality: soc[i]'s own upper bound is fixed by
        # soc0_kwh at i==0, so if the battery is already too depleted
        # for the rest of a long peak stretch (no more off-peak charging
        # opportunity beforehand — real physical exhaustion, the same
        # case the old heuristic's "Bypass" mode acknowledges), a hard
        # constraint here makes the *entire* LP infeasible and PuLP can
        # return meaningless variable values rather than erroring
        # cleanly — confirmed against a real depleted-battery fixture
        # during review. Soft keeps the LP always solvable and still
        # forces the solver to satisfy the reserve in full whenever it's
        # actually reachable (the penalty dwarfs any real price/
        # degradation trade-off), only accepting a shortfall when there
        # is truly no other option.
        next_cheap = s.get("next_cheap_idx")
        if next_cheap is not None and next_cheap > i:
            future_deficit = s.get("remaining_deficit", 0.0) - max(0.0, load - pv)
            future_deficit *= (1.0 + cfg.reserve_margin_pct)
            shortfall = pulp.LpVariable(f"reserve_shortfall_{i}", 0)
            prob += soc[i] + shortfall >= floor_kwh + future_deficit / eff
            reserve_penalty_terms.append(shortfall)

        grid_in = charge[i] + grid_to_load[i]
        grid_out = pv_to_grid[i] + eff * batt_to_export[i]
        grid_cost_terms.append(imp * grid_in - exp * grid_out)
        degradation_terms.append(degradation * (batt_to_load[i] + batt_to_export[i]))

    prob += (pulp.lpSum(grid_cost_terms) + pulp.lpSum(degradation_terms)
             + RESERVE_PENALTY * pulp.lpSum(reserve_penalty_terms))
    prob.solve(_solver())

    infeasible = pulp.LpStatus[prob.status] != "Optimal"

    out_slots = []
    trace, cost_trace = [], []
    grid_cost_total = 0.0
    for i, s in enumerate(slots):
        s = dict(s)
        s["charge"] = round(max(0.0, _val(charge[i])), 6)
        s["export"] = round(max(0.0, _val(batt_to_export[i])), 6)
        out_slots.append(s)

        grid_in = _val(charge[i]) + _val(grid_to_load[i])
        grid_out = _val(pv_to_grid[i]) + eff * _val(batt_to_export[i])
        delta = s["imp"] * grid_in - s["exp"] * grid_out
        grid_cost_total += delta
        soc_pct = _val(soc[i]) / cap * 100.0
        trace.append(round(soc_pct, 1))
        # Battery-side kWh actually discharged this slot (self-consumption
        # + export combined) — computed directly from the LP's own
        # variables, not reconstructed from a SoC delta against the
        # previous row. That reconstruction is exactly what caused real
        # confusion in practice: which slot a SoC change "belongs to"
        # depends on a same-row-is-the-end-of-this-slot convention that's
        # easy to misread by one row, making a good decision look
        # backwards. This number needs no such alignment — it's already
        # anchored to slot i.
        battery_kwh = _val(batt_to_load[i]) + _val(batt_to_export[i])
        cost_trace.append({"delta": round(delta, 4), "total": round(grid_cost_total, 4),
                            "grid_in": round(grid_in, 3), "charge_in": round(s["charge"], 3),
                            "battery_kwh": round(battery_kwh, 3)})

    total_cost = _val(prob.objective, grid_cost_total) if prob.objective is not None else grid_cost_total
    return out_slots, trace, cost_trace, round(grid_cost_total, 4), round(total_cost, 4), infeasible


def _apply_min_export_block_filter(slots, cost_trace, cfg):
    """Any contiguous export block below min_export_pct of battery
    capacity isn't worth the SoC it eats into, even if it technically
    shaved a fraction of a penny off cost — matches the old optimiser's
    same cleanup. Returns an override dict (slot_index -> 0.0) if any
    block needs dropping, else None."""
    min_export_kwh = cfg.min_export_pct / 100.0 * cfg.battery_kwh
    override = {}
    i = 0
    n = len(slots)
    while i < n:
        if slots[i]["export"] > EPS:
            j = i
            block_kwh = 0.0
            while j < n and slots[j]["export"] > EPS:
                block_kwh += slots[j]["export"]
                j += 1
            if block_kwh < min_export_kwh:
                for k in range(i, j):
                    override[k] = 0.0
            i = j
        else:
            i += 1
    return override or None


def _apply_daily_target_cutoff(slots, cost_trace, cfg, today_date):
    """BALANCED mode only: once today's cumulative real grid cost is at
    or below target_daily_net_cost, stop exporting for the rest of
    today — tomorrow gets its own fresh budget. Returns an override
    dict, or None if the target isn't in play or isn't reached."""
    if cfg.mode != Mode.BALANCED or cfg.target_daily_net_cost is None or today_date is None:
        return None
    cutoff = None
    for i, s in enumerate(slots):
        if s["start"].date() != today_date:
            continue
        if cost_trace[i]["total"] <= cfg.target_daily_net_cost:
            cutoff = i
            break
    if cutoff is None:
        return None
    override = {}
    for i, s in enumerate(slots):
        if s["start"].date() == today_date and i > cutoff and s["export"] > EPS:
            override[i] = 0.0
    return override or None


def solve(slots, soc0_pct, cfg, *, today_date=None):
    """Solve the LP for the given mode. slots: list from
    core.slots.build_slots(). soc0_pct: current battery SoC, 0-100.
    Returns a PlanResult."""
    cap = cfg.battery_kwh
    floor_kwh = cfg.floor_soc / 100.0 * cap
    soc0_kwh = max(floor_kwh, min(cap, soc0_pct / 100.0 * cap))

    out_slots, trace, cost_trace, grid_cost, total_cost, infeasible = _solve_lp(
        slots, soc0_kwh, cfg)

    # Both corrections only ever force an export slot's cap to 0, never
    # loosen one — so accumulating them across passes (rather than
    # passing only the latest pass's dict) is always safe, and is
    # required: a second pass re-solving with just its own new findings
    # would otherwise silently drop the first pass's correction instead
    # of layering on top of it.
    accumulated_override = {}
    for _pass in range(2):  # at most: one daily-target pass, one min-block pass
        new_override = {}
        cutoff = _apply_daily_target_cutoff(out_slots, cost_trace, cfg, today_date)
        if cutoff:
            new_override.update(cutoff)
        min_block = _apply_min_export_block_filter(out_slots, cost_trace, cfg)
        if min_block:
            new_override.update(min_block)
        if not new_override:
            break
        accumulated_override.update(new_override)
        out_slots, trace, cost_trace, grid_cost, total_cost, infeasible = _solve_lp(
            slots, soc0_kwh, cfg, export_cap_override=accumulated_override)

    return PlanResult(out_slots, trace, cost_trace, grid_cost, total_cost, infeasible)


def storm_decision(soc0_pct, *, storm_target_soc, discharge_kw, charge_kw,
                    ev_concurrent_charge_kw, ev_active):
    """Pure port of the live Storm Watch override — charge to
    storm_target_soc regardless of price/peak-block, then hold (no
    exports). Deliberately not run through the LP: storm duration is
    unpredictable (weather alerts have no fixed end time), so, same as
    before, this drives the live control command directly rather than
    the 48h plan."""
    disch = 0.0 if ev_active else discharge_kw
    chg = ev_concurrent_charge_kw if ev_active else charge_kw
    if soc0_pct < storm_target_soc - 1:
        return {"state": "Storm Watch — Charging", "disch_kw": disch,
                "charge_kw": chg, "charging": True}
    return {"state": "Storm Watch — Holding", "disch_kw": disch,
            "charge_kw": chg, "charging": False}

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

# Hard wall-clock cap (seconds) on a single solver invocation. solve()
# can call _solve_lp up to 3 times a tick (initial pass + two correction
# passes), so this bounds worst-case tick time to a multiple of this,
# never unbounded. Non-negotiable for a live battery controller: adding
# the PV-routing-priority binary below (one per slot) turned this from
# a pure LP into a MILP, and a MILP's branch-and-bound can — on some
# inputs — run far longer than a continuous LP ever would. Confirmed in
# production: with no time limit set, a solve on real data hung the
# single AppDaemon worker thread for over three hours with no exception
# or log output, silently freezing the whole app (tick() never
# returned, so nothing after it — including the next scheduled tick —
# could run). If the solver can't finish in time it returns whatever
# incumbent it has (or none), which surfaces as PlanResult.infeasible
# and falls back to safe self-consumption — exactly the existing
# soft-reserve safety net, just also covering "ran out of time" and not
# only "truly has no solution." 15s (not the original 8s) after a
# second real incident: on real production data an 8s limit was too
# tight for a genuinely-solvable-but-slower instance, and GLPK's own
# wrapper doesn't fail gracefully when --tmlim is hit before any
# feasible solution is found — it raises PulpSolverError rather than
# reporting a plain non-optimal status (see the try/except around
# prob.solve below). That's now handled correctly either way, but
# giving harder instances more headroom means falling back to safe
# self-consumption less often for cases that were never truly
# unsolvable, just slower than 8s. Worst case (3 solves a tick) is
# still well within AppDaemon's 5-minute tick interval.
SOLVER_TIME_LIMIT_SEC = 15


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
        return pulp.GLPK_CMD(path=path, msg=False, timeLimit=SOLVER_TIME_LIMIT_SEC)
    return pulp.PULP_CBC_CMD(msg=False, timeLimit=SOLVER_TIME_LIMIT_SEC)


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
    # Self-consumption (batt_to_load) and export (batt_to_export) get
    # separately configured £/kWh costs — see EXPORT_DEGRADATION_
    # OVERRIDES in config.py for why these differ per mode (eco: high
    # export bar but normal self-consumption; max_profit: near-zero
    # self-consumption cost but a small real export floor instead of
    # the old hard-0). Neither is special-cased here anymore — both
    # come straight from cfg, which already resolved the right default
    # per mode in SiteConfig.__post_init__.
    degradation = cfg.degradation
    export_degradation = cfg.export_degradation
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

    # No more mode-based hard block here — eco used to force this to 0
    # regardless of price; now every mode shares the same rate ceiling
    # and gets gated purely by export_degradation instead (eco's is set
    # high enough that it behaves the same in practice on ordinary
    # days, but a genuinely exceptional price can still clear it).
    export_ub = [max_d_exp for _ in range(n)]
    if export_cap_override:
        for i, ub in export_cap_override.items():
            export_ub[i] = min(export_ub[i], max(0.0, ub))
    batt_to_export = [pulp.LpVariable(f"batt_exp_{i}", 0, export_ub[i]) for i in range(n)]

    grid_cost_terms = []
    degradation_terms = []
    reserve_penalty_terms = []
    session_reward_terms = []
    # Indexed by slot, unlike reserve_penalty_terms (which is just a flat
    # list for the objective sum and doesn't preserve which slot each
    # shortfall belongs to) — needed to report per-slot shortfall in
    # cost_trace. None for any slot that never got a reserve constraint
    # at all (no next_cheap_idx, or this slot IS the cheap slot) — pulp.
    # value(None) raises AttributeError rather than returning None, so
    # this must be checked explicitly below, not passed through _val().
    reserve_shortfall_vars = [None] * n
    # Per slot list of (variable, £-per-kWh rate) pairs contributing to
    # that slot's Octoplus Power Down/Up reward — kept separate from
    # grid_cost_terms deliberately: these rewards are real money, but
    # not money reflected on the electricity bill (octopoints are
    # redeemed separately; Power Up credit isn't the same ledger as the
    # per-kWh tariff), so cost_trace's "real £" grid_cost/delta figures
    # must never include them — only the objective (decision-making)
    # should see this incentive. Reported to the caller as its own
    # session_reward_gbp field instead.
    session_reward_components = [[] for _ in range(n)]

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
        # Gate coefficient is pv itself, not an arbitrary large constant:
        # pv_to_grid[i] can never exceed pv[i] anyway (the balance
        # equation above already enforces that with all-nonnegative
        # terms), so this is the tightest valid bound available, not
        # just "big enough". An oversized Big-M here weakens the MILP's
        # LP relaxation at every branch-and-bound node (the binary can
        # sit fractional far longer before the solver is forced to
        # resolve it) — a well-known cause of MILP blow-up, and the
        # likely reason a fixed 1e5 constant took hours to solve on real
        # data. Using pv directly removes that risk without changing
        # what's actually allowed.
        battery_full = pulp.LpVariable(f"batt_full_{i}", cat="Binary")
        prob += soc[i] >= (cap - full_tol_kwh) * battery_full
        prob += pv_to_grid[i] <= pv * battery_full

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

        # EV concurrently charging this slot: mirrors the live "EV
        # Protection" override (gridlock.py's apply()) so the 48h plan
        # itself reflects what will actually be commanded, not just
        # the right-now slot. Previously this constraint didn't exist
        # here at all — a future dispatch slot could show the battery
        # charging at the full rate, or even discharging, neither of
        # which would really happen once that slot arrived live: the
        # battery never fights the EV for the same circuit (discharge
        # forced to 0), and any grid-charging shares the circuit at
        # ev_concurrent_charge_kw rather than the full charge_kw.
        # Dispatch slots already get treated as cheap pricing
        # (core/slots.py), so the cheap-rate branch above already
        # zeroes batt_to_load here too — only export and the charge
        # rate itself need this extra rule.
        if s.get("dispatch"):
            prob += charge[i] <= cfg.ev_concurrent_charge_kw / 2.0
            prob += batt_to_export[i] == 0

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
            # remaining_deficit is now a NET figure (can be negative —
            # see annotate_reserve) — floored here, once, on the final
            # total, not per-slot before summing. A stretch with a big
            # midday surplus ahead of an evening deficit should let that
            # surplus reduce how much reserve is needed now, since it'll
            # genuinely recharge the battery for free before the deficit
            # arrives — flooring each slot first (the old bug) discarded
            # that surplus instead of crediting it.
            future_deficit = max(0.0, s.get("remaining_deficit", 0.0) - (load - pv))
            future_deficit *= (1.0 + cfg.reserve_margin_pct)
            shortfall = pulp.LpVariable(f"reserve_shortfall_{i}", 0)
            prob += soc[i] + shortfall >= floor_kwh + future_deficit / eff
            reserve_penalty_terms.append(shortfall)
            reserve_shortfall_vars[i] = shortfall

        grid_in = charge[i] + grid_to_load[i]
        grid_out = pv_to_grid[i] + eff * batt_to_export[i]
        # EV charging draws straight off the grid alongside everything
        # else in this slot, at the same dispatch-adjusted rate already
        # baked into `imp` (see slots.py's cheap_floor clamp during an
        # IOG window) — folded in here rather than tracked as a separate
        # figure so cost_delta_p/total_gbp are the true total spend, not
        # an approximation that quietly excludes EV.
        grid_cost_terms.append(imp * (grid_in + s.get("ev_kwh", 0.0)) - exp * grid_out)

        # Octoplus Power Down (formerly Saving Sessions): reward for
        # importing LESS than the predicted per-slot baseline, paid in
        # octopoints. Power Up (formerly Free Electricity Sessions):
        # reward for importing MORE than baseline, credited at this
        # slot's own unit rate (i.e. the excess above baseline is
        # effectively free) — see gridlock.py for where these baseline
        # figures actually come from (Octopus's own predicted-consumption
        # sensor, a genuine forward-looking per-half-hour curve, not a
        # guess). Both are max(0, ...) of a *maximised* quantity, which a
        # continuous LP can't express safely: a plain "<=" tied straight
        # to grid_in risks exactly the same false-infeasibility bug as
        # the PV-routing constraint above whenever grid_in is forced past
        # the baseline by real load (Power Down) or naturally sits below
        # it, the routine case (Power Up) — confirmed by first testing
        # this in isolation against a standalone PuLP model before it
        # ever touched the real optimiser (both directions, including
        # the "can the solver claim reward without earning it" check).
        # Needs its own binary per session slot, same proven pattern as
        # battery_full above — only adds one for slots actually inside
        # an announced/joined session, not every slot in the horizon.
        # Big-M must cover BOTH the "release" direction (the binary's
        # opposite state must never force the credited amount negative
        # against its own >=0 bound — confirmed to fail in exactly the
        # way described above when this only accounted for one of the
        # two directions: a genuinely-forced-above-baseline slot came
        # back with the whole solve reported infeasible AND a negative
        # reward, caught by test_power_down_reward_stays_feasible_
        # when_forced_above_baseline) AND the "cap" direction (the
        # credited amount must be able to reach the full baseline/
        # excess when genuinely earned, not be artificially capped
        # below it). grid_in ranges [0, max_c+load] and baseline is an
        # independent forecast that could in principle sit outside that
        # range — max(baseline, max_c+load) safely dominates the true
        # gap in either direction for either variable, so the same
        # formula is correct for both.
        session_big_m = max(max_c + load, EPS)
        # Natural max of grid_out (pv_to_grid + eff*batt_to_export) —
        # the export-side counterpart of session_big_m above. Deliberately
        # separate rather than reusing session_big_m: on a large-PV/small-
        # load site pv + eff*max_d_exp can exceed max_c + load, and a
        # Big-M that's too small for the "cap" direction silently caps the
        # earned reward below what was actually earned (same class of bug
        # as the original session_big_m fix, just the other variable).
        session_export_big_m = max(pv + eff * max_d_exp, EPS)

        pd_baseline = s.get("power_down_baseline_kwh")
        pd_points_per_kwh = s.get("power_down_points_per_kwh") or 0.0
        if pd_baseline is not None and pd_points_per_kwh > 0:
            reward_rate = pd_points_per_kwh * cfg.octopoint_value_gbp
            pd_big_m = max(pd_baseline, session_big_m)
            pd_below = pulp.LpVariable(f"pd_below_{i}", cat="Binary")
            pd_reduction = pulp.LpVariable(f"pd_reduction_{i}", 0)
            prob += pd_reduction <= (pd_baseline - grid_in) + pd_big_m * (1 - pd_below)
            prob += pd_reduction <= pd_big_m * pd_below
            session_reward_terms.append(reward_rate * pd_reduction)
            session_reward_components[i].append((pd_reduction, reward_rate))

        pu_baseline = s.get("power_up_baseline_kwh")
        if pu_baseline is not None:
            reward_rate = imp  # excess above baseline is credited at this slot's own unit rate
            pu_big_m = max(pu_baseline, session_big_m)
            pu_above = pulp.LpVariable(f"pu_above_{i}", cat="Binary")
            pu_excess = pulp.LpVariable(f"pu_excess_{i}", 0)
            prob += pu_excess <= (grid_in - pu_baseline) + pu_big_m * (1 - pu_above)
            prob += pu_excess <= pu_big_m * pu_above
            session_reward_terms.append(reward_rate * pu_excess)
            session_reward_components[i].append((pu_excess, reward_rate))

        # Power Down export baseline: confirmed real via the user's own
        # HA entity data (a genuine, separately-forecast per-half-hour
        # export curve alongside the import baseline above, for the
        # same joined session) — modelled here as a reward for
        # exporting MORE than that predicted baseline, on the reasoning
        # that "reduce your net demand on the grid" extends naturally
        # to "push more back onto it", and it reuses the same session's
        # points_per_kwh (there's only one points currency per session,
        # not a separate rate for the export sensor). NOT modelling the
        # mirror case for Power Up's export baseline here: the sign of
        # that incentive is genuinely ambiguous (does Power Up — "use
        # more" — reward exporting less, or is export simply untouched
        # by it?) and I can't confirm Octopus's actual backend
        # calculation either way from client-side code alone, so
        # guessing risks steering the battery in the wrong direction on
        # real money — left out until confirmed rather than encoded as
        # a guess.
        pd_export_baseline = s.get("power_down_export_baseline_kwh")
        if pd_export_baseline is not None and pd_points_per_kwh > 0:
            export_reward_rate = pd_points_per_kwh * cfg.octopoint_value_gbp
            pd_exp_big_m = max(pd_export_baseline, session_export_big_m)
            pd_exp_above = pulp.LpVariable(f"pd_exp_above_{i}", cat="Binary")
            pd_exp_excess = pulp.LpVariable(f"pd_exp_excess_{i}", 0)
            prob += pd_exp_excess <= (grid_out - pd_export_baseline) + pd_exp_big_m * (1 - pd_exp_above)
            prob += pd_exp_excess <= pd_exp_big_m * pd_exp_above
            session_reward_terms.append(export_reward_rate * pd_exp_excess)
            session_reward_components[i].append((pd_exp_excess, export_reward_rate))

        degradation_terms.append(degradation * batt_to_load[i]
                                  + export_degradation * batt_to_export[i])

    prob += (pulp.lpSum(grid_cost_terms) + pulp.lpSum(degradation_terms)
             + RESERVE_PENALTY * pulp.lpSum(reserve_penalty_terms)
             - pulp.lpSum(session_reward_terms))
    try:
        prob.solve(_solver())
        infeasible = pulp.LpStatus[prob.status] != "Optimal"
    except pulp.PulpSolverError:
        # GLPK's own wrapper doesn't always fail gracefully on a timeout
        # the way CBC does: confirmed directly against a real glpsol
        # binary that when --tmlim cuts the search off *before* it ever
        # reaches a first feasible/optimal solution (as opposed to
        # finding one and then running out of time), glpsol exits
        # non-zero and PuLP raises this instead of just reporting a
        # non-optimal status — which would otherwise escape _solve_lp
        # entirely and hit tick()'s much broader "Engine error" handler
        # instead of the safe-fallback path this is actually meant to
        # take. Every variable is still None here (nothing was ever
        # solved) — _val() already treats that the same as any other
        # missing value, so the rest of this function works unchanged;
        # only the status needs forcing to infeasible.
        infeasible = True

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
        delta = s["imp"] * (grid_in + s.get("ev_kwh", 0.0)) - s["exp"] * grid_out
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
        # 0.0 for any slot with no reserve constraint at all (see
        # reserve_shortfall_vars' definition above) — genuinely
        # different from "constrained but satisfied with zero slack",
        # though both report as 0.0 here; that distinction doesn't
        # matter to a caller checking "is there a real shortfall",
        # only "was one needed and not fully covered".
        shortfall_var = reserve_shortfall_vars[i]
        reserve_shortfall_kwh = _val(shortfall_var) if shortfall_var is not None else 0.0
        # Real money, but never billed on the electricity account the
        # way grid_in/grid_out are — octopoints are redeemed separately,
        # Power Up credit isn't the per-kWh tariff — so this is reported
        # as its own figure, never folded into delta/total above.
        session_reward_gbp = sum(_val(var) * rate for var, rate in session_reward_components[i])
        cost_trace.append({"delta": round(delta, 4), "total": round(grid_cost_total, 4),
                            "grid_in": round(grid_in, 3), "charge_in": round(s["charge"], 3),
                            "battery_kwh": round(battery_kwh, 3),
                            "reserve_shortfall_kwh": round(reserve_shortfall_kwh, 4),
                            "session_reward_gbp": round(session_reward_gbp, 4)})

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
                    ev_concurrent_charge_kw, ev_active,
                    usable_kwh=None, expected_load_kwh=None):
    """Pure port of the live Storm Watch override — charge to
    storm_target_soc regardless of price/peak-block, then hold (no
    exports). Deliberately not run through the LP: storm duration is
    unpredictable (weather alerts have no fixed end time), so, same as
    before, this drives the live control command directly rather than
    the 48h plan.

    usable_kwh/expected_load_kwh (optional, both required together to take
    effect): energy already banked above the normal floor vs. what's
    forecast to be needed over the estimated outage window. When the
    reserve already covers it outright (no safety margin added — an
    explicit choice, not an oversight: the estimate itself, not an
    arbitrary buffer on top, is what's meant to decide this), Storm Watch
    has nothing left to protect against for this slot — "override": False
    tells the caller to run its own normal price-optimised action instead
    of forcing charge/hold against a risk that's already covered."""
    if (usable_kwh is not None and expected_load_kwh is not None
            and usable_kwh >= expected_load_kwh):
        disch = 0.0 if ev_active else discharge_kw
        return {"state": "Storm Watch — Reserve Sufficient", "disch_kw": disch,
                "charge_kw": 0.0, "charging": False, "override": False}
    disch = 0.0 if ev_active else discharge_kw
    if soc0_pct < storm_target_soc - 1:
        chg = ev_concurrent_charge_kw if ev_active else charge_kw
        return {"state": "Storm Watch — Charging", "disch_kw": disch,
                "charge_kw": chg, "charging": True, "override": True}
    # Once at/above target there's nothing left to charge TOWARD — holding
    # means genuinely holding, not "cap discharge but leave a nonzero
    # charge-rate allowance in place." Confirmed live: SoC 100%, state
    # already "Storm Watch — Holding", and the inverter was still pulling
    # ~1.7kW of grid import straight into an already-full battery, because
    # "Maximum Self Consumption" mode doesn't itself know the battery has
    # nowhere left to put that charge — only the commanded rate limit does.
    return {"state": "Storm Watch — Holding", "disch_kw": disch,
            "charge_kw": 0.0, "charging": False, "override": True}

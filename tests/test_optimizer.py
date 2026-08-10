from core.config import SiteConfig, Mode
from core import optimizer

from helpers import make_slots


class _FakeVar:
    """Minimal stand-in for an LpVariable/LpAffineExpression — pulp.value()
    just calls x.value(), so this is all _val needs to be exercised
    without a full LP solve."""
    def __init__(self, v):
        self._v = v

    def value(self):
        return self._v


def test_val_replaces_nan_and_inf_not_just_none():
    """`pulp.value(x) or 0.0` looks like a safe fallback but isn't: NaN is
    truthy in Python, so that pattern lets a stray NaN through unchanged.
    This is exactly the bug that shipped a corrupted plan_table to a real
    deployment — a NaN slot value produced a shortened, misaligned row
    once published. _val must catch NaN/inf explicitly, not just None."""
    assert optimizer._val(_FakeVar(None)) == 0.0
    assert optimizer._val(_FakeVar(float("nan"))) == 0.0
    assert optimizer._val(_FakeVar(float("inf"))) == 0.0
    assert optimizer._val(_FakeVar(float("-inf"))) == 0.0
    assert optimizer._val(_FakeVar(3.5)) == 3.5
    assert optimizer._val(_FakeVar(0.0)) == 0.0

CHEAP = 0.10
EPS_COST = 1e-6


def base_cfg(**overrides):
    defaults = dict(battery_kwh=10.0, efficiency=0.9, floor_soc=0.0,
                     charge_kw=20.0, discharge_kw=20.0, export_rate_kw=20.0,
                     cheap_rate=CHEAP, min_export_pct=0.0)
    defaults.update(overrides)
    return SiteConfig(**defaults)


def test_on_peak_import_zero_when_reserve_feasible():
    """One cheap slot followed by an on-peak stretch the fully-charged
    battery can cover in full — no grid import should occur on any
    on-peak slot (spec: GridImport = 0 for on-peak slots when normal /
    not storm)."""
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 1.0}] + \
        [{"imp": 0.30, "exp": 0.05, "load": 1.0} for _ in range(4)]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(slots, soc0_pct=100.0, cfg=cfg)
    assert not result.infeasible
    for i, s in enumerate(result.slots):
        if s["imp"] > CHEAP:
            assert result.cost_trace[i]["grid_in"] - s["charge"] < 1e-6, \
                f"slot {i} imported from the grid to serve load on an on-peak slot"


def test_eco_only_exports_on_a_genuinely_exceptional_margin():
    """eco used to hard-block battery export at any price (export_ub
    forced to 0 regardless of cfg) — replaced by a soft, much higher
    export-specific degradation cost (EXPORT_DEGRADATION_OVERRIDES),
    so a normal good export slot still doesn't sell (same as before in
    practice), but a genuinely exceptional one now can, rather than
    being hard-blocked no matter how good the price gets."""
    eff = 0.9
    export_degradation = 0.25
    # profit = 0.30*0.9 - 0.10/0.9 - 0.25 = 0.27 - 0.111 - 0.25 = -0.091
    ordinary = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.30, "load": 0.0} for _ in range(2)]
    # profit = 0.60*0.9 - 0.10/0.9 - 0.25 = 0.54 - 0.111 - 0.25 = +0.179
    exceptional = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.60, "load": 0.0} for _ in range(2)]

    cfg = base_cfg(mode=Mode.ECO, degradation=0.09,
                    export_degradation=export_degradation, efficiency=eff)

    slots_ordinary = make_slots(ordinary, CHEAP)
    result_ordinary = optimizer.solve(slots_ordinary, soc0_pct=0.0, cfg=cfg)
    assert all(s["export"] == 0 for s in result_ordinary.slots), \
        "eco should still hold at an ordinary good export price"

    slots_exceptional = make_slots(exceptional, CHEAP)
    result_exceptional = optimizer.solve(slots_exceptional, soc0_pct=0.0, cfg=cfg)
    assert any(s["export"] > 0 for s in result_exceptional.slots), \
        "eco should sell when the margin genuinely clears its (high) export bar"


def test_balanced_exports_only_when_profitable():
    """soc0=0 (nothing pre-charged to sell "for free") isolates a genuine
    buy-low-sell-high cycle: the battery must grid-charge at the cheap
    rate before it has anything to export, so the true cost basis per
    battery-kWh cycled is imp/eff (efficiency lost once going in),
    revenue is exp*eff (efficiency lost once coming out), and profit is
    exp*eff - imp/eff - degradation — the round-trip version of the
    spec's "only export if the margin beats the degradation cost" rule."""
    degradation = 0.05
    eff = 0.9
    # profit = 0.30*0.9 - 0.10/0.9 - 0.05 = 0.27 - 0.111 - 0.05 = +0.109
    profitable = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.30, "load": 0.0} for _ in range(2)]
    # profit = 0.15*0.9 - 0.10/0.9 - 0.05 = 0.135 - 0.111 - 0.05 = -0.026
    unprofitable = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.15, "load": 0.0} for _ in range(2)]

    cfg = base_cfg(mode=Mode.BALANCED, degradation=degradation, efficiency=eff)

    slots_p = make_slots(profitable, CHEAP)
    result_p = optimizer.solve(slots_p, soc0_pct=0.0, cfg=cfg)
    assert any(s["export"] > 0 for s in result_p.slots), \
        "balanced mode should export in a genuinely profitable slot"

    slots_u = make_slots(unprofitable, CHEAP)
    result_u = optimizer.solve(slots_u, soc0_pct=0.0, cfg=cfg)
    assert all(s["export"] == 0 for s in result_u.slots), \
        "balanced mode should not export when the margin is below the degradation cost"


def test_max_profit_exports_fully_where_balanced_would_not():
    """max_profit's export cost used to be hard-forced to 0 regardless
    of what was configured (sold at literally any positive margin) —
    replaced by a small real floor (EXPORT_DEGRADATION_OVERRIDES,
    0.03) instead, still much lower than balanced's, but a genuine
    number rather than a special-cased override."""
    degradation_balanced = 0.05
    export_degradation_max_profit = 0.03
    eff = 0.9
    # profit (balanced) = 0.17*0.9 - 0.10/0.9 - 0.05 = -0.008 (unprofitable)
    # profit (max_profit) = 0.17*0.9 - 0.10/0.9 - 0.03 = +0.012 (profitable)
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.17, "load": 0.0} for _ in range(2)]

    slots_bal = make_slots(rows, CHEAP)
    cfg_bal = base_cfg(mode=Mode.BALANCED, degradation=degradation_balanced, efficiency=eff)
    result_bal = optimizer.solve(slots_bal, soc0_pct=0.0, cfg=cfg_bal)
    assert all(s["export"] == 0 for s in result_bal.slots)

    slots_mp = make_slots(rows, CHEAP)
    cfg_mp = base_cfg(mode=Mode.MAX_PROFIT, export_degradation=export_degradation_max_profit,
                       efficiency=eff)
    result_mp = optimizer.solve(slots_mp, soc0_pct=0.0, cfg=cfg_mp)
    assert any(s["export"] > 0 for s in result_mp.slots), \
        "max_profit's small export floor should still find this margin worth selling"


def test_charging_blocked_outside_cheap_slots():
    rows = [{"imp": 0.30, "exp": 0.05, "load": 1.0} for _ in range(3)]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(mode=Mode.BALANCED)
    result = optimizer.solve(slots, soc0_pct=50.0, cfg=cfg)
    assert all(s["charge"] == 0.0 for s in result.slots)


def test_battery_never_drains_for_load_during_a_cheap_slot():
    """Real-world report: with a fully-charged battery and nothing better
    to do with the charge, the optimiser still drained it to serve load
    during genuinely cheap slots, because battery self-consumption only
    costs the degradation rate in the objective — and degradation
    (typically ~5p/kWh) is very often *less* than a cheap import rate
    (e.g. 10p), making the LP "prefer" cycling stored charge over
    importing fresh at that same cheap rate. That's a real net loss, not
    a saving: the charge came from the grid at this same cheap rate a
    slot or two ago (or will be topped up at it again shortly), so the
    round trip only adds efficiency loss and wear for nothing. Off-peak
    load must come straight from grid (or PV) whenever the battery isn't
    also being charged that slot, matching the old heuristic's own
    explicit "leave the battery alone and import instead" rule."""
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 1.0} for _ in range(3)] + \
        [{"imp": CHEAP, "exp": 0.05, "load": 0.0}]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(battery_kwh=20.0, mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(slots, soc0_pct=100.0, cfg=cfg)
    assert not result.infeasible
    assert all(t == 100.0 for t in result.trace[:3]), (
        "the battery should stay untouched — load should come straight "
        "from the grid at the same cheap rate, not cycle stored charge"
    )
    assert all(c["battery_kwh"] == 0.0 for c in result.cost_trace[:3])


def test_power_down_reward_incentivizes_reducing_below_baseline():
    """Octoplus Power Down (formerly Saving Sessions) pays octopoints for
    importing LESS than a predicted per-slot baseline. Set up a slot
    where, WITHOUT the reward, self-consuming from the battery instead
    of importing is a net loss (degradation costs more than the import
    it avoids) — so the LP should import normally. WITH a large enough
    reward (500 pts/kWh here, well within real session ranges), the
    reward per kWh reduced (500/800=0.625) comfortably beats that net
    loss (0.40-0.30=0.10/kWh), so the LP should drain the battery
    instead specifically to beat the baseline and claim it."""
    degradation = 0.40  # deliberately more than the 0.30 import rate
    baseline = 1.0
    points_per_kwh = 500  # 500/800 = 0.625 £/kWh reward
    rows_with = [{"imp": 0.30, "exp": 0.05, "load": 1.0,
                  "power_down_baseline_kwh": baseline,
                  "power_down_points_per_kwh": points_per_kwh}]
    rows_without = [{"imp": 0.30, "exp": 0.05, "load": 1.0}]
    cfg = base_cfg(battery_kwh=10.0, floor_soc=0.0, discharge_kw=20.0,
                    mode=Mode.BALANCED, degradation=degradation)

    result_without = optimizer.solve(make_slots(rows_without, CHEAP), soc0_pct=100.0, cfg=cfg)
    result_with = optimizer.solve(make_slots(rows_with, CHEAP), soc0_pct=100.0, cfg=cfg)
    assert not result_without.infeasible and not result_with.infeasible

    assert result_without.cost_trace[0]["grid_in"] > 0.99, \
        "without the reward, importing is cheaper than draining the battery — should just import"
    assert result_with.cost_trace[0]["grid_in"] < 0.5, \
        "with a reward that clears the net cost of discharging instead, should drain the battery below baseline"
    assert result_with.cost_trace[0]["session_reward_gbp"] > 0.5, \
        "should report real, non-trivial reward credit for beating the baseline"
    # The reward must NEVER leak into the real £ figures — those track
    # actual grid_in/grid_out at the real tariff rates only.
    real_delta = 0.30 * result_with.cost_trace[0]["grid_in"] - 0.05 * 0.0
    assert abs(result_with.cost_trace[0]["delta"] - real_delta) < 1e-3, \
        "delta must reflect only the real import/export cost, not the session reward"


def test_power_down_reward_stays_feasible_when_forced_above_baseline():
    """A predicted baseline is just a historical average — a genuinely
    higher-than-usual load can legitimately force grid_in above it with
    no way around it (no battery/PV available at all here). This must
    stay solvable (not infeasible) and report zero reward, never a
    negative one, for that slot."""
    rows = [{"imp": 0.30, "exp": 0.05, "load": 5.0,
             "power_down_baseline_kwh": 1.0, "power_down_points_per_kwh": 500}]
    cfg = base_cfg(battery_kwh=10.0, floor_soc=0.0, discharge_kw=0.0,
                    mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(make_slots(rows, CHEAP), soc0_pct=0.0, cfg=cfg)
    assert not result.infeasible
    assert abs(result.cost_trace[0]["grid_in"] - 5.0) < 1e-3, \
        "with no battery/PV available, all 5kWh of load must come from grid regardless of baseline"
    assert result.cost_trace[0]["session_reward_gbp"] == 0.0, \
        "genuinely forced above baseline should report zero reward, not a negative one"


def test_power_up_reward_credits_import_above_baseline():
    """Power Up (formerly Free Electricity Sessions) credits back the
    unit rate for consumption above a predicted baseline — literally
    "equal to the value of your current unit rate... multiplied by the
    increase in kWh consumed" per Octopus's own T&Cs. That makes it a
    break-even credit for the excess portion, not a flat profit margin
    the LP would necessarily choose to chase — so this verifies the
    *calculation* directly (grid_in forced to an exact known value via
    zero battery/PV availability) rather than an incentive the solver
    might be indifferent about, and checks the reward is isolated from
    the real £ figures exactly like the Power Down case."""
    rows = [{"imp": 0.20, "exp": 0.05, "load": 2.0, "power_up_baseline_kwh": 0.5}]
    cfg = base_cfg(battery_kwh=10.0, floor_soc=0.0, discharge_kw=0.0,
                    mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(make_slots(rows, CHEAP), soc0_pct=0.0, cfg=cfg)
    assert not result.infeasible
    assert abs(result.cost_trace[0]["grid_in"] - 2.0) < 1e-3, \
        "no battery/PV available -- all 2kWh of load must come from grid regardless of baseline"
    expected_reward = (2.0 - 0.5) * 0.20  # excess above baseline, credited at this slot's own rate
    assert abs(result.cost_trace[0]["session_reward_gbp"] - expected_reward) < 1e-3
    real_delta = 0.20 * 2.0 - 0.05 * 0.0
    assert abs(result.cost_trace[0]["delta"] - real_delta) < 1e-3, \
        "delta must reflect only the real import cost, not the Power Up credit"


def test_power_up_reward_stays_feasible_when_naturally_below_baseline():
    """Sitting below a Power Up baseline is the routine case (most
    slots, most days) — this must solve cleanly and report zero reward,
    never a negative one and never able to be gamed into claiming
    credit that wasn't earned."""
    rows = [{"imp": 0.20, "exp": 0.05, "load": 0.2, "power_up_baseline_kwh": 5.0}]
    cfg = base_cfg(battery_kwh=10.0, floor_soc=0.0, discharge_kw=0.0,
                    mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(make_slots(rows, CHEAP), soc0_pct=0.0, cfg=cfg)
    assert not result.infeasible
    assert result.cost_trace[0]["session_reward_gbp"] == 0.0


def test_power_up_reward_big_m_covers_baseline_exceeding_slot_capacity():
    """The Big-M gating pu_excess must cover the gap in BOTH directions:
    a plain "how far above baseline could grid_in go" bound (max_c+load)
    is NOT enough on its own if the baseline forecast itself is larger
    than that — a real possibility, since the baseline is an independent
    historical prediction, not derived from this slot's own charge/load
    capacity. Deliberately construct exactly that (baseline far exceeds
    what this slot could possibly import) to prove the fix covers this
    direction too, not just the one the first failing test caught."""
    rows = [{"imp": 0.20, "exp": 0.05, "load": 0.2, "power_up_baseline_kwh": 50.0}]
    cfg = base_cfg(battery_kwh=10.0, floor_soc=0.0, charge_kw=4.0, discharge_kw=0.0,
                    mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(make_slots(rows, CHEAP), soc0_pct=0.0, cfg=cfg)
    assert not result.infeasible
    assert result.cost_trace[0]["session_reward_gbp"] == 0.0


def test_power_down_export_reward_incentivizes_exporting_above_baseline():
    """Power Down's export-side baseline (confirmed real via a user's
    own HA entity data — a genuine, separately-forecast per-half-hour
    export curve alongside the import baseline, for the same joined
    session) is modelled as a reward for exporting MORE than that
    prediction, on the reasoning that "reduce your net demand on the
    grid" extends naturally to "push more back onto it" — it reuses the
    same session's points_per_kwh rather than a separate rate. Set up a
    slot where, without the reward, discharging to export is a net loss
    (export_degradation costs more than the export rate earns) — so the
    LP should keep the battery charged. With a large enough reward, it
    should discharge specifically to beat the export baseline."""
    export_degradation = 0.40  # deliberately more than the 0.05 export rate
    baseline = 1.0
    points_per_kwh = 500  # 500/800 = 0.625 £/kWh reward
    rows_with = [{"imp": 0.30, "exp": 0.05, "load": 0.0,
                  "power_down_export_baseline_kwh": baseline,
                  "power_down_points_per_kwh": points_per_kwh}]
    rows_without = [{"imp": 0.30, "exp": 0.05, "load": 0.0}]
    cfg = base_cfg(battery_kwh=10.0, floor_soc=0.0, discharge_kw=20.0,
                    mode=Mode.BALANCED, export_degradation=export_degradation)

    result_without = optimizer.solve(make_slots(rows_without, CHEAP), soc0_pct=100.0, cfg=cfg)
    result_with = optimizer.solve(make_slots(rows_with, CHEAP), soc0_pct=100.0, cfg=cfg)
    assert not result_without.infeasible and not result_with.infeasible

    assert result_without.slots[0]["export"] < 1e-3, \
        "without the reward, export_degradation exceeds the export rate — should not discharge to export"
    assert result_with.slots[0]["export"] > 1.0, \
        "with a reward that clears the net cost of discharging, should export above the baseline"
    assert result_with.cost_trace[0]["session_reward_gbp"] > 0.5, \
        "should report real, non-trivial reward credit for beating the export baseline"
    real_delta = -0.05 * (cfg.efficiency * result_with.slots[0]["export"])
    assert abs(result_with.cost_trace[0]["delta"] - real_delta) < 1e-3, \
        "delta must reflect only the real export revenue, not the session reward"


def test_power_down_export_reward_stays_feasible_when_naturally_below_baseline():
    """A predicted export baseline is just a historical average — with
    no PV and no discharge capability at all, grid_out can never reach
    it. This must stay solvable and report zero reward, never negative,
    for that slot."""
    rows = [{"imp": 0.30, "exp": 0.05, "load": 0.0, "pv": 0.0,
             "power_down_export_baseline_kwh": 5.0, "power_down_points_per_kwh": 500}]
    cfg = base_cfg(battery_kwh=10.0, floor_soc=0.0, discharge_kw=0.0,
                    mode=Mode.BALANCED)
    result = optimizer.solve(make_slots(rows, CHEAP), soc0_pct=50.0, cfg=cfg)
    assert not result.infeasible
    assert result.slots[0]["export"] < 1e-3, \
        "no discharge capability — export must stay at 0 regardless of baseline"
    assert result.cost_trace[0]["session_reward_gbp"] == 0.0, \
        "genuinely unable to reach baseline should report zero reward, not a negative one"


def test_power_down_export_reward_big_m_covers_pv_exceeding_import_range():
    """The export-side Big-M must be scoped to grid_out's own natural
    range (pv + eff*max_export_kw), not reused from the import-side
    session_big_m (max_c + load) — a small max_c/load combined with a
    large PV means the true achievable grid_out can comfortably exceed
    the import-side range, and an undersized M silently caps the earned
    reward below what was actually earned (same class of bug as the
    original Power Down/Up Big-M fix, just the other variable). With no
    load and a battery that's already full, all 20kWh of PV is forced
    onto pv_to_grid (nowhere else for it to go), so the true reward is
    ~0.625 * (20 - 0.5) ≈ 12.19 — an undersized M reusing session_big_m
    (max_c + load = 1.0 here) would instead cap it at ~0.625 * 1.0."""
    rows = [{"imp": 0.30, "exp": 0.05, "load": 0.0, "pv": 20.0,
             "power_down_export_baseline_kwh": 0.5, "power_down_points_per_kwh": 500}]
    cfg = base_cfg(battery_kwh=1.0, floor_soc=0.0, charge_kw=2.0, discharge_kw=0.0,
                    export_rate_kw=20.0, mode=Mode.BALANCED)
    result = optimizer.solve(make_slots(rows, CHEAP), soc0_pct=100.0, cfg=cfg)
    assert not result.infeasible
    assert result.cost_trace[0]["session_reward_gbp"] > 10.0, \
        "reward must reflect the true ~19.5kWh excess over baseline, not be capped by an undersized Big-M"


def test_ev_dispatch_slot_caps_charge_rate_and_blocks_export():
    """The 48h plan used to have zero EV-awareness at all — only the
    live, right-now slot (gridlock.py's "EV Protection" override)
    clamped battery discharge to 0 and capped grid-charging to
    ev_concurrent_charge_kw. A future dispatch slot in the plan could
    show the battery charging at the full rate, or even exporting,
    neither of which would actually happen once that slot arrived live
    — confirmed against a real plan showing exactly that (ev_kwh and a
    full-rate charge_kwh together in the same row). _solve_lp must
    mirror the same live constraint for every dispatch slot, not just
    the current one."""
    ev_kw = 4.0
    rows = [{"imp": CHEAP, "exp": 0.60, "load": 0.0, "dispatch": True, "ev_kwh": 3.7}]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(battery_kwh=20.0, charge_kw=20.0, discharge_kw=20.0,
                    export_rate_kw=20.0, mode=Mode.MAX_PROFIT,
                    export_degradation=0.01, ev_concurrent_charge_kw=ev_kw)

    result = optimizer.solve(slots, soc0_pct=50.0, cfg=cfg)
    assert not result.infeasible
    assert result.slots[0]["charge"] <= ev_kw / 2.0 + 1e-6, (
        "charge rate should be capped to the shared EV-concurrent rate, "
        "not the full charge_kw, during a dispatch slot"
    )
    assert result.slots[0]["export"] == 0.0, (
        "battery export should be blocked during a dispatch slot, matching "
        "the live 'EV Protection' override's discharge-clamped-to-0 rule, "
        "even though this export price is otherwise very profitable"
    )


def test_ev_charging_cost_is_folded_into_cost_trace_delta():
    """A real Plan table row showed grid_kwh 0.27, EV 3.70kWh, import 3.5p
    and a cost_delta of only +0.9p — 0.27 x 3.5p, with the 3.7kWh of EV
    charging silently excluded. The user's own read on it: EV charging is
    real grid draw at the same rate, so it belongs in the same £ figure,
    not a separate tracked column. With battery charging disabled here,
    grid_in is 0, so delta must equal imp * ev_kwh exactly."""
    rows = [{"imp": CHEAP, "exp": 0.0, "load": 0.0, "dispatch": True, "ev_kwh": 3.7}]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(battery_kwh=20.0, charge_kw=0.0, discharge_kw=0.0,
                    export_rate_kw=0.0, ev_concurrent_charge_kw=0.0)

    result = optimizer.solve(slots, soc0_pct=50.0, cfg=cfg)
    assert not result.infeasible
    expected_delta = CHEAP * 3.7
    assert abs(result.cost_trace[0]["delta"] - expected_delta) < 1e-3, (
        "delta must include EV charging cost (imp * ev_kwh), not just "
        "battery/load grid draw"
    )


def test_daily_target_cutoff_halts_further_export():
    from datetime import timezone
    degradation = 0.05
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.50, "load": 0.0} for _ in range(4)]
    slots = make_slots(rows, CHEAP)
    today = slots[0]["start"].date()
    cfg = base_cfg(mode=Mode.BALANCED, degradation=degradation,
                     target_daily_net_cost=-0.5)
    result = optimizer.solve(slots, soc0_pct=100.0, cfg=cfg, today_date=today)
    # once cumulative cost drops to/below the target, later slots that day
    # should stop exporting even though they'd otherwise be profitable
    hit_target = False
    for i, s in enumerate(result.slots):
        if hit_target:
            assert s["export"] == 0.0
        if result.cost_trace[i]["total"] <= cfg.target_daily_net_cost:
            hit_target = True
    assert hit_target, "fixture should have reached the target at some point"


def test_reserve_shortfall_degrades_gracefully_instead_of_infeasible():
    """A battery already too depleted to bridge a long on-peak stretch
    before the next cheap slot used to make the whole LP infeasible
    (soc[0]'s reserve requirement is fixed by the constant soc0, not a
    free variable) — PuLP then returns numeric garbage rather than
    erroring (observed: SoC trace jumping to 100% out of nowhere for a
    battery that was never charged). The reserve is a soft constraint
    (see RESERVE_PENALTY in optimizer.py) specifically so this stays
    solvable and the resulting plan stays physically sane."""
    rows = [{"imp": 0.30, "exp": 0.05, "load": 3.0} for _ in range(4)] + \
        [{"imp": CHEAP, "exp": 0.05, "load": 0.0}]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(battery_kwh=5.0, floor_soc=20.0, mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(slots, soc0_pct=25.0, cfg=cfg)
    assert not result.infeasible
    assert all(0.0 <= t <= 100.0 for t in result.trace)
    # SoC must be monotonically explainable — never jump upward without
    # a corresponding charge/PV-to-battery decision that slot.
    prev = 25.0
    for i, s in enumerate(result.slots):
        cap_kwh = cfg.battery_kwh
        max_gain_pct = (s["charge"] * cfg.efficiency) / cap_kwh * 100.0 + 1.0  # +1 rounding slack
        assert result.trace[i] <= prev + max_gain_pct, \
            f"slot {i} SoC rose more than its own charge decision can explain"
        prev = result.trace[i]
    # The genuinely-unreachable reserve must be visible in cost_trace too
    # (not just "the LP didn't crash") — this is what a "will tomorrow's
    # solar be enough" check reads to tell a real shortfall from a
    # perfectly normal low SoC.
    assert result.cost_trace[0]["reserve_shortfall_kwh"] > 0, (
        "a battery this depleted, this far from the next cheap slot, "
        "should show a real reported shortfall, not a silently-absorbed one"
    )
    # And it should shrink slot to slot as the reserve requirement eases
    # closer to the cheap slot, never grow.
    shortfalls = [c["reserve_shortfall_kwh"] for c in result.cost_trace]
    assert all(a >= b for a, b in zip(shortfalls, shortfalls[1:])), (
        "reserve shortfall should ease monotonically toward the cheap slot, not grow"
    )


def test_low_starting_soc_shows_zero_shortfall_when_solar_will_refill_it():
    """The whole point of reading the LP's own reserve_shortfall (rather
    than a naive "does tomorrow's forecast cover load" comparison) as a
    solar-deficit signal is that it must NOT false-positive on a battery
    that's deliberately low because abundant solar is coming and will
    refill it before it's actually needed — the normal, correct pattern
    on a good solar day (a real concern raised about this feature: a
    naive comparison would trigger constantly in summer, since the
    battery is routinely sold down low overnight trusting next-day sun
    to refill it). Confirmed against exactly that shape: battery starts
    at 5%, rate-limited overnight charging can't fill it, heavy midday
    PV, then a genuine evening reserve need — shortfall must come out
    genuinely zero throughout, not just "small"."""
    rows = ([{"imp": CHEAP, "exp": 0.05, "pv": 0.0, "load": 0.4} for _ in range(4)]
            + [{"imp": 0.28, "exp": 0.15, "pv": 0.0, "load": 0.4} for _ in range(3)]
            + [{"imp": 0.28, "exp": 0.15, "pv": 3.0, "load": 0.4} for _ in range(20)]
            + [{"imp": 0.28, "exp": 0.15, "pv": 0.0, "load": 1.0} for _ in range(10)])
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(battery_kwh=20.0, floor_soc=5.0, charge_kw=6.0, discharge_kw=20.0,
                    export_rate_kw=20.0, mode=Mode.BALANCED, reserve_margin_pct=0.15)
    result = optimizer.solve(slots, soc0_pct=5.0, cfg=cfg)
    assert not result.infeasible
    assert all(c["reserve_shortfall_kwh"] == 0.0 for c in result.cost_trace), (
        "abundant solar later in the horizon should fully satisfy the "
        "reserve with zero shortfall, even starting from a nearly-empty battery"
    )


def test_glpk_solver_error_on_timeout_is_treated_as_infeasible_not_raised():
    """Confirmed directly against a real glpsol binary: GLPK's own PuLP
    wrapper doesn't always fail gracefully on a timeout the way CBC
    does. When --tmlim cuts the search off *before* it ever reaches a
    first feasible/optimal solution (as opposed to finding one and then
    running out of time), glpsol exits non-zero and PuLP raises
    PulpSolverError instead of just reporting a non-optimal status —
    which, uncaught, escaped _solve_lp entirely and hit gridlock.py's
    much broader "Engine error" handler instead of the safe,
    reserve-infeasible fallback this is actually meant to take (seen
    live in production immediately after the solver time limit was
    first added). _solve_lp must catch this and report infeasible=True,
    the same as any other non-optimal status — not raise."""
    class _RaisingSolver:
        def actualSolve(self, lp):
            raise optimizer.pulp.PulpSolverError("simulated glpsol timeout failure")

    rows = [{"imp": CHEAP, "exp": 0.05, "load": 1.0} for _ in range(4)]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(battery_kwh=10.0, mode=Mode.BALANCED)

    original_solver = optimizer._solver
    optimizer._solver = lambda: _RaisingSolver()
    try:
        result = optimizer.solve(slots, soc0_pct=50.0, cfg=cfg)
    finally:
        optimizer._solver = original_solver

    assert result.infeasible


def test_battery_kwh_is_anchored_to_its_own_slot_not_a_soc_delta():
    """Real-world confusion: reconstructing "how much battery did this
    slot use" from (this row's SoC - previous row's SoC) requires
    knowing which row a delta "belongs to", and it's easy to misattribute
    it to the wrong slot's price by one row when reading a table by eye
    (a 22.6p export slot that actually sold the most looked, read that
    way, like it barely sold anything, and an ECO slot right after it —
    which wasn't selling anything at all, just serving a small load —
    looked like a huge sale at a cheap price). cost_trace["battery_kwh"]
    must equal each slot's own real discharge, taken directly from that
    slot's own LP variables, with no dependence on any other row."""
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.50, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.05, "load": 0.5}] + \
        [{"imp": CHEAP, "exp": 0.05, "load": 0.0}]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(mode=Mode.BALANCED, degradation=0.05)
    result = optimizer.solve(slots, soc0_pct=100.0, cfg=cfg)

    export_slot_battery_kwh = result.cost_trace[1]["battery_kwh"]
    eco_slot_battery_kwh = result.cost_trace[2]["battery_kwh"]
    assert export_slot_battery_kwh > eco_slot_battery_kwh, (
        "the profitable export slot should show as the large battery user on "
        "its own row, and the small self-consumption-only slot as the small "
        "one on its own row — independent of how any other row reads")
    # And it should match the slot's own decision variables directly:
    # export slot's battery_kwh is (at minimum) its own export amount.
    assert export_slot_battery_kwh >= result.slots[1]["export"] - 1e-3


def test_reserve_margin_holds_back_more_than_the_bare_forecast():
    """The plan re-solves every 5 minutes and can't claw back energy an
    earlier slot already sold — a reserve built against the bare
    point-estimate load forecast cuts exactly to the wire against that
    forecast being right, which real house load rarely is slot to slot.
    reserve_margin_pct should make the optimiser hold back more than a
    zero-margin reserve would, i.e. export less now to leave slack for
    the forecast being wrong later. Needs a closing cheap slot so
    next_cheap_idx (and therefore the reserve constraint) is actually
    defined for the on-peak stretch in between — without one there's
    nothing to reserve *toward*, by design (see annotate_reserve)."""
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.60, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.05, "load": 2.0} for _ in range(4)] + \
        [{"imp": CHEAP, "exp": 0.05, "load": 0.0}]
    slots_a = make_slots(rows, CHEAP)
    slots_b = make_slots(rows, CHEAP)

    cfg_no_margin = base_cfg(battery_kwh=15.0, charge_kw=40.0, discharge_kw=40.0,
                              export_rate_kw=40.0, mode=Mode.BALANCED, degradation=0.05,
                              reserve_margin_pct=0.0)
    cfg_with_margin = base_cfg(battery_kwh=15.0, charge_kw=40.0, discharge_kw=40.0,
                                export_rate_kw=40.0, mode=Mode.BALANCED, degradation=0.05,
                                reserve_margin_pct=0.30)

    result_a = optimizer.solve(slots_a, soc0_pct=100.0, cfg=cfg_no_margin)
    result_b = optimizer.solve(slots_b, soc0_pct=100.0, cfg=cfg_with_margin)

    export_a = result_a.slots[1]["export"]
    export_b = result_b.slots[1]["export"]
    assert export_b < export_a, (
        "a larger reserve margin should export less from the same profitable "
        "slot, leaving more slack for the load forecast to be wrong")
    assert not result_a.infeasible and not result_b.infeasible
    # And concretely: the margin case should still have real charge left
    # right before the closing cheap slot, not have ground down to 0%.
    assert result_b.trace[-2] > result_a.trace[-2]


def test_pv_fills_battery_to_full_before_any_direct_export():
    """Real hardware fact, not an economic choice: in self-consumption
    mode the inverter's own firmware always routes surplus PV into the
    battery until it's full before any of it can export — the LP has no
    authority to sell PV directly while there's still headroom, no
    matter how good the export price looks. Reproduces a real production
    report: rate-limited overnight charging (3.5p import) leaves the
    battery below full when a heavy solar day (50+kWh) starts at a
    modest 15p export rate; if PV were treated as freely exportable
    regardless of SoC, the battery would sit at whatever level overnight
    charging left it at while every kWh of surplus PV exported straight
    through, never climbing further — exactly the bug reported live: a
    battery pegged at ~75% SoC all day while high solar exported around
    it instead of finishing the top-up to 100% first."""
    rows = ([{"imp": 0.035, "exp": 0.05, "pv": 0.0, "load": 0.3} for _ in range(4)]
            + [{"imp": 0.28, "exp": 0.15, "pv": 0.0, "load": 0.3} for _ in range(3)]
            + [{"imp": 0.28, "exp": 0.15, "pv": 2.5, "load": 0.3} for _ in range(20)]
            # Evening reserve need after the sun goes down — without this
            # the LP has nothing left to hold charge *for* by the end of
            # the horizon and profitably drains the battery on the last
            # few slots regardless of this test's concern, which isn't a
            # bug, just a horizon-edge artefact that would otherwise
            # contaminate the "stays full" assertions below.
            + [{"imp": 0.28, "exp": 0.15, "pv": 0.0, "load": 1.5} for _ in range(10)])
    slots = make_slots(rows, cheap_rate=0.035)
    cfg = base_cfg(battery_kwh=20.0, floor_soc=5.0, cheap_rate=0.035,
                    charge_kw=6.0, discharge_kw=20.0, export_rate_kw=20.0,
                    mode=Mode.BALANCED, degradation=0.05)

    result = optimizer.solve(slots, soc0_pct=5.0, cfg=cfg)
    assert not result.infeasible

    sunny = range(7, 27)  # the 20 heavy-PV slots, before the evening load kicks back in
    still_filling = [i for i in sunny if result.trace[i] < 99.0]
    already_full = [i for i in sunny if result.trace[i] >= 99.0]

    # The rate-limited overnight charge can't have filled a 20kWh battery
    # from 5% in 4 slots at 3kWh/slot — there must be real headroom left
    # when the sun comes up, or this test isn't exercising the fill path.
    assert still_filling, "fixture didn't leave any headroom for solar to fill — test is vacuous"
    assert already_full, "battery never reached full — solar isn't topping it up"

    # While there's still headroom, surplus PV must go into the battery,
    # not straight to export — no export revenue should show up yet.
    for i in still_filling:
        assert result.cost_trace[i]["delta"] <= EPS_COST, (
            f"slot {i}: exported PV revenue while battery still had headroom "
            f"(soc={result.trace[i]}%) — PV bypassed the mandatory battery fill")

    # SoC must actually climb slot over slot while filling, not sit flat
    # (the exact bug reported: pegged SoC with solar draining straight
    # past the battery all day instead of finishing the top-up).
    for a, b in zip(still_filling, still_filling[1:]):
        assert result.trace[b] > result.trace[a], (
            "SoC should rise every slot while there's still headroom and "
            "surplus PV available, not stay pegged")

    # Once genuinely full, further surplus PV must export for real revenue.
    for i in already_full:
        assert result.cost_trace[i]["delta"] < -EPS_COST, (
            f"slot {i}: battery is full (soc={result.trace[i]}%) but no "
            "export revenue was recorded for the surplus PV")


def test_storm_override_charges_regardless_of_price():
    decision = optimizer.storm_decision(
        soc0_pct=40.0, storm_target_soc=100.0, discharge_kw=10.0,
        charge_kw=10.0, ev_concurrent_charge_kw=5.0, ev_active=False)
    assert decision["charging"] is True
    assert decision["state"] == "Storm Watch — Charging"
    assert decision["charge_kw"] == 10.0
    assert decision["disch_kw"] == 10.0


def test_storm_override_holds_once_target_reached():
    decision = optimizer.storm_decision(
        soc0_pct=100.0, storm_target_soc=100.0, discharge_kw=10.0,
        charge_kw=10.0, ev_concurrent_charge_kw=5.0, ev_active=False)
    assert decision["charging"] is False
    assert decision["state"] == "Storm Watch — Holding"


def test_storm_override_ev_active_clamps_discharge_zero():
    decision = optimizer.storm_decision(
        soc0_pct=40.0, storm_target_soc=100.0, discharge_kw=10.0,
        charge_kw=10.0, ev_concurrent_charge_kw=5.0, ev_active=True)
    assert decision["disch_kw"] == 0.0
    assert decision["charge_kw"] == 5.0

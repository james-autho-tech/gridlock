from core.config import SiteConfig, Mode
from core import optimizer

from helpers import make_slots

CHEAP = 0.10


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


def test_eco_mode_never_exports_from_battery():
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 1.0, "pv": 0.0}] + \
        [{"imp": 0.10, "exp": 0.60, "load": 0.2, "pv": 0.0} for _ in range(4)]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(mode=Mode.ECO, degradation=0.09)
    result = optimizer.solve(slots, soc0_pct=100.0, cfg=cfg)
    assert not result.infeasible
    for s in result.slots:
        assert s["export"] == 0.0


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
    degradation_balanced = 0.05
    eff = 0.9
    # profit (balanced) = 0.15*0.9 - 0.10/0.9 - 0.05 = -0.026 (unprofitable)
    # profit (max_profit, degradation=0) = 0.15*0.9 - 0.10/0.9 = +0.024 (profitable)
    rows = [{"imp": CHEAP, "exp": 0.05, "load": 0.0}] + \
        [{"imp": 0.30, "exp": 0.15, "load": 0.0} for _ in range(2)]

    slots_bal = make_slots(rows, CHEAP)
    cfg_bal = base_cfg(mode=Mode.BALANCED, degradation=degradation_balanced, efficiency=eff)
    result_bal = optimizer.solve(slots_bal, soc0_pct=0.0, cfg=cfg_bal)
    assert all(s["export"] == 0 for s in result_bal.slots)

    slots_mp = make_slots(rows, CHEAP)
    cfg_mp = base_cfg(mode=Mode.MAX_PROFIT, degradation=degradation_balanced, efficiency=eff)
    result_mp = optimizer.solve(slots_mp, soc0_pct=0.0, cfg=cfg_mp)
    assert any(s["export"] > 0 for s in result_mp.slots), \
        "max_profit ignores degradation cost and should still find this margin worth selling"


def test_charging_blocked_outside_cheap_slots():
    rows = [{"imp": 0.30, "exp": 0.05, "load": 1.0} for _ in range(3)]
    slots = make_slots(rows, CHEAP)
    cfg = base_cfg(mode=Mode.BALANCED)
    result = optimizer.solve(slots, soc0_pct=50.0, cfg=cfg)
    assert all(s["charge"] == 0.0 for s in result.slots)


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

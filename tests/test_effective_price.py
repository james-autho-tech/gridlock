import pytest
from core.effective_price import battery_energy_cost_p, effective_prices_p


def _slots(imps, exps=None, exports=None):
    n = len(imps)
    exps = exps or [0.15] * n
    exports = exports or [0.0] * n
    return [{"imp": imps[i], "exp": exps[i], "export": exports[i]} for i in range(n)]


def _ct(grid_in, charge_in, batt):
    return [{"grid_in": g, "charge_in": c, "battery_kwh": b} for g, c, b in zip(grid_in, charge_in, batt)]


def test_battery_cost_is_the_weighted_charge_price_grossed_up_for_losses_and_wear():
    slots = _slots([0.07, 0.07, 0.30])
    ct = _ct([2.0, 2.0, 0.0], [2.0, 2.0, 0.0], [0.0, 0.0, 1.0])
    p = battery_energy_cost_p(slots, ct, cheap_rate_gbp=0.10, efficiency=0.9, degradation_gbp_per_kwh=0.01)
    assert abs(p - (0.07 / 0.9 + 0.01) * 100) < 1e-9
    # No grid charging in the plan -> the configured cheap rate
    p2 = battery_energy_cost_p(slots, _ct([0, 0, 0], [0, 0, 0], [1, 1, 1]), 0.10, 0.9, 0.0)
    assert abs(p2 - 0.10 / 0.9 * 100) < 1e-9


def test_battery_covered_slots_cost_the_battery_price_not_the_peak_rate():
    slots = _slots([0.07, 0.30, 0.30, 0.30])
    ct = _ct([2.0, 0.0, 0.0, 0.0], [2.0, 0.0, 0.0, 0.0], [0.0, 1.0, 1.0, 1.0])
    out = effective_prices_p(slots, ct, battery_cost_p=9.0)
    assert out == pytest.approx([7.0, 9.0, 9.0, 9.0])


def test_battery_running_flat_later_pushes_the_later_import_price_back():
    # Battery covers slots 1-2, then runs out: slot 3 imports at 30p before the next charge at slot 4.
    slots = _slots([0.07, 0.30, 0.30, 0.30, 0.07, 0.30])
    ct = _ct([2.0, 0.0, 0.0, 1.0, 2.0, 0.0], [2.0, 0.0, 0.0, 0.0, 2.0, 0.0], [0.0, 1.0, 1.0, 0.0, 0.0, 1.0])
    out = effective_prices_p(slots, ct, battery_cost_p=9.0)
    assert out[1] == pytest.approx(30.0) and out[2] == pytest.approx(30.0)   # paid for at slot 3's rate
    assert out[3] == pytest.approx(30.0)          # importing anyway
    assert out[4] == pytest.approx(7.0)           # charging slot: extra load imports at the cheap rate
    assert out[5] == pytest.approx(9.0)           # after the recharge, battery-covered again


def test_exporting_slots_cost_the_forgone_export_price():
    slots = _slots([0.30, 0.30], exps=[0.15, 0.15], exports=[1.0, 0.0])
    ct = _ct([0.0, 0.0], [0.0, 0.0], [1.0, 1.0])
    out = effective_prices_p(slots, ct, battery_cost_p=9.0)
    assert out[0] == pytest.approx(15.0) and out[1] == pytest.approx(9.0)

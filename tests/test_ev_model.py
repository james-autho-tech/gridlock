from datetime import datetime, timedelta, timezone

from core import ev_model

T = lambda h, m=0: datetime(2026, 10, 27, h, m, tzinfo=timezone.utc)


def test_this_mornings_dispatch_is_cut_to_what_the_car_needed():
    """EDF planned 11:30-14:30 (22.2 kWh); at 11:20 the car was ~2 kWh off
    its 80% limit at 7.1 kW. Only the 11:30 half-hour is real."""
    kept, dropped = ev_model.trim_dispatches([(T(11, 30), T(14, 30), 22.2)], 2.0, 7.1, T(11, 20))
    assert [(s, e) for s, e, _ in kept] == [(T(11, 30), T(12))]
    assert abs(kept[0][2] - 2.0) < 1e-9
    assert dropped[0] == (T(12), T(12, 30)) and dropped[-1] == (T(14), T(14, 30))


def test_trim_counts_only_the_rest_of_the_current_half_hour():
    # 11:45, 10 kWh at 7 kW: 1.75 kWh left in this half-hour, then 3.5 a slot.
    kept, dropped = ev_model.trim_dispatches([(T(11), T(14), 21)], 10.0, 7.0, T(11, 45))
    assert [round(k, 2) for _, _, k in kept] == [1.75, 3.5, 3.5, 1.25]
    assert len(dropped) == 1 and dropped[0][0] == T(13, 30)


def test_car_at_its_limit_drops_every_dispatch():
    kept, dropped = ev_model.trim_dispatches([(T(22, 30), T(23), 3.7)], 0.0, 7.0, T(12))
    assert kept == [] and dropped == [(T(22, 30), T(23))]


def test_need_and_expected_charge():
    assert ev_model.need_kwh(62, 80, 75) == 18 / 100 * 75 / ev_model.CHARGE_EFFICIENCY
    assert ev_model.need_kwh(None, 80, 75) is None
    assert ev_model.need_kwh(85, 80, 75) == 0.0
    slots = ev_model.expected_charge(5.0, 7.0, [T(23, 30), T(23)])
    assert [(s, round(k, 2)) for s, _, k in slots] == [(T(23), 3.5), (T(23, 30), 1.5)]


def test_learns_capacity_and_rate_across_a_charge():
    st = None
    for pct, kwh in ((60, 0.0), (63, 2.4), (70, 7.5), (80, 15.0)):
        st = ev_model.learn(st, charging=True, pct=pct, energy_added_kwh=kwh, power_kw=7.1)
    assert 70 <= st["capacity_kwh"] <= 80 and st["rate_kw"] == 7.1
    st = ev_model.learn(st, charging=False, pct=80, energy_added_kwh=15.0, power_kw=0)
    assert st["anchor"] is None and st["capacity_kwh"]
    # A fresh session (energy counter reset) re-anchors instead of learning nonsense.
    st2 = ev_model.learn(dict(st, anchor=[80, 15.0]), charging=True, pct=50, energy_added_kwh=0.1, power_kw=7)
    assert st2["anchor"] == [50, 0.1] and st2["capacity_kwh"] == st["capacity_kwh"]

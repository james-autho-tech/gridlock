from core.dhw_plan import (slot_scores, cheapest_window, run_slots_needed, deadline_slots,
                           unit_will_heat)


def test_slot_scores_prefer_warm_slots_on_a_flat_tariff():
    s = slot_scores([20.0] * 3, [2.0, 10.0, 16.0], flow_temp=58.0, eta=0.45)
    assert s[0] > s[1] > s[2]


def test_slot_scores_prefer_cheap_slots_when_the_price_gap_is_big():
    s = slot_scores([8.0, 30.0], [2.0, 16.0], flow_temp=58.0, eta=0.45)
    assert s[0] < s[1]


def test_cheapest_window_finds_the_lowest_contiguous_block_before_the_deadline():
    scores = [5, 5, 1, 1, 9, 9, 0, 0]
    assert cheapest_window(scores, 2, latest_end_slot=8)[:2] == (6, 8)
    assert cheapest_window(scores, 2, latest_end_slot=6)[:2] == (2, 4)
    # Deadline too tight for a full run: start now.
    assert cheapest_window(scores, 2, latest_end_slot=1)[:2] == (0, 2)
    assert cheapest_window([], 2, 4) is None


def test_cheapest_window_mean_score_and_oversized_run():
    start, end, mean = cheapest_window([3, 1, 2], 5, 10)
    assert (start, end) == (0, 3) and abs(mean - 2.0) < 1e-9


def test_run_slots_and_deadline_rounding():
    assert run_slots_needed(60, 30) == 2
    assert run_slots_needed(31, 30) == 2
    assert run_slots_needed(0, 30) == 1
    assert deadline_slots(20.0, 24.0, 0.5) == 8
    assert deadline_slots(30.0, 24.0, 0.5) == 1      # overdue -> as soon as possible
    assert deadline_slots(None, 24.0, 0.5) == 48     # unknown -> whole interval available
    assert deadline_slots(5.0, 0, 0.5) > 1000        # no guarantee configured


def test_unit_will_heat_respects_the_units_own_hysteresis():
    assert unit_will_heat(43.0, 53.0, 10.0) is True
    assert unit_will_heat(44.0, 53.0, 10.0) is False
    assert unit_will_heat(None, 53.0, 10.0) is True


def test_draw_wh_measures_only_the_drop_beyond_standing_loss():
    from core.dhw_plan import draw_wh
    mass = 150 * 1.163  # ~174 Wh/C
    # 60 W standing loss over 5 min on 174 Wh/C -> 0.029 C expected drop
    assert draw_wh(50.0, 49.97, 5 / 60, 60.0, mass, False) == 0.0          # just loss
    wh = draw_wh(50.0, 48.0, 5 / 60, 60.0, mass, False)                      # a real draw
    assert 330 < wh < 350
    assert draw_wh(50.0, 48.0, 5 / 60, 60.0, mass, True) is None             # heating masks it
    assert draw_wh(None, 48.0, 5 / 60, 60.0, mass, False) is None
    assert draw_wh(50.0, 48.0, 2.0, 60.0, mass, False) is None               # interval too long


def test_profile_blends_a_day_in_gradually_and_wraps_midnight():
    from core.dhw_plan import blend_day_into_profile, expected_draw, ready_by_slot, profile_bucket
    from datetime import datetime
    day = [None] * 48
    day[14] = 1000.0   # 07:00-07:30 showers
    p = blend_day_into_profile(None, day)
    assert p[14] == 200.0 and p[13] == 0.0
    p = blend_day_into_profile(p, day)
    assert abs(p[14] - 360.0) < 1e-9
    assert profile_bucket(datetime(2026, 10, 17, 7, 10)) == 14
    # From 23:00 (bucket 46) the 07:00 draw is 16 slots ahead
    exp = expected_draw(p, 46, 20)
    assert exp[16] == p[14]
    assert ready_by_slot(exp, 300.0, lead_slots=1) == 16
    assert ready_by_slot([0.0] * 10, 300.0) is None


def test_tank_can_cover_is_conservative():
    from core.dhw_plan import tank_can_cover
    mass = 174.0
    # Tank 52, unit starts a run at 43 -> 9 C * 174 = 1566 Wh usable
    assert tank_can_cover(52.0, 53.0, 10.0, mass, 500.0, 60.0, 6.0) is True     # needs ~860*1.25
    assert tank_can_cover(52.0, 53.0, 10.0, mass, 1200.0, 60.0, 6.0) is False
    assert tank_can_cover(None, 53.0, 10.0, mass, 0.0, 60.0, 6.0) is False


def test_slots_until_ready_by_wraps_to_tomorrow_and_takes_the_nearest():
    from core.dhw_plan import slots_until_ready_by
    from datetime import datetime
    now = datetime(2026, 10, 17, 20, 30)
    assert slots_until_ready_by(now, ["06:30", "18:30"], 30) == 20   # 06:30 tomorrow is 10 h away
    assert slots_until_ready_by(datetime(2026, 10, 17, 12, 0), ["06:30", "18:30"], 30) == 13
    assert slots_until_ready_by(now, [], 30) is None
    assert slots_until_ready_by(now, ["garbage"], 30) is None

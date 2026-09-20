from core.tariff_backtest import backtest_day_cost, edf_windows_to_daily_pattern
from datetime import datetime, timezone


def _flat_day(kwh_per_slot=1.0, slots=48):
    return [kwh_per_slot] * slots


def test_flat_rate_no_windows():
    imp = _flat_day(1.0)
    exp = [0.0] * 48
    cost = backtest_day_cost(imp, exp, import_default=0.30)
    assert abs(cost - 48 * 0.30) < 1e-9


def test_off_peak_window_applies_lower_rate_only_inside_it():
    imp = [1.0] * 48  # 1 kWh every half-hour, all day
    exp = [0.0] * 48
    windows = [{"start": "23:30", "end": "05:30", "rate": 0.05}]
    cost = backtest_day_cost(imp, exp, import_default=0.30, import_windows=windows)
    # 23:30-05:30 = 12 slots at 5p, remaining 36 slots at 30p
    expected = 12 * 0.05 + 36 * 0.30
    assert abs(cost - expected) < 1e-9


def test_wrapping_window_across_midnight():
    """slot_idx 47 is 23:30-00:00, slot_idx 0 is 00:00-00:30 -- a window
    like 23:30-05:30 must treat these as adjacent, not wrap incorrectly."""
    imp = [0.0] * 48
    imp[47] = 2.0  # 23:30-00:00
    imp[0] = 3.0   # 00:00-00:30
    exp = [0.0] * 48
    windows = [{"start": "23:30", "end": "05:30", "rate": 0.05}]
    cost = backtest_day_cost(imp, exp, import_default=0.30, import_windows=windows)
    assert abs(cost - (2.0 + 3.0) * 0.05) < 1e-9


def test_export_is_credited_against_import_cost():
    imp = [1.0] * 48
    exp = [1.0] * 48
    cost = backtest_day_cost(imp, exp, import_default=0.30, export_rate=0.15)
    expected = 48 * 0.30 - 48 * 0.15
    assert abs(cost - expected) < 1e-9


def test_standing_charge_is_added_flat():
    imp = [0.0] * 48
    exp = [0.0] * 48
    cost = backtest_day_cost(imp, exp, import_default=0.30, standing_gbp=0.50)
    assert abs(cost - 0.50) < 1e-9


def test_net_credit_day_is_negative():
    imp = [0.0] * 48
    exp = [2.0] * 48
    cost = backtest_day_cost(imp, exp, import_default=0.30, export_rate=0.15)
    assert cost < 0


def test_edf_windows_to_daily_pattern_extracts_offpeak_band():
    base = datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
    windows = [
        (base.replace(hour=23, minute=0), base.replace(hour=23, minute=0), 0.0699),  # placeholder overwritten below
    ]
    from datetime import timedelta
    windows = [
        (base + timedelta(hours=5), base + timedelta(hours=22), 0.36),   # day rate
        (base + timedelta(hours=22), base + timedelta(hours=29), 0.07),  # off-peak, wraps past midnight
    ]
    pattern = edf_windows_to_daily_pattern(windows)
    assert pattern["default"] == 0.36
    assert len(pattern["windows"]) == 1
    assert pattern["windows"][0]["rate"] == 0.07
    assert pattern["windows"][0]["start"] == "22:00"


def test_edf_windows_to_daily_pattern_empty_input():
    assert edf_windows_to_daily_pattern([]) is None

from datetime import date

from core.warranty import (throughput_used_mwh, throughput_pct_used,
                            equivalent_full_cycles, warranty_years_remaining,
                            parse_install_date)


def test_throughput_used_mwh_converts_kwh_to_mwh():
    assert throughput_used_mwh(1000.0) == 1.0
    assert throughput_used_mwh(0.0) == 0.0


def test_throughput_pct_used_computes_percentage_of_cap():
    # 57960 kWh = 57.96 MWh, half of a 115.92 MWh cap.
    assert throughput_pct_used(57960.0, 115.92) == 50.0


def test_throughput_pct_used_caps_at_100_percent_past_the_limit():
    assert throughput_pct_used(200000.0, 115.92) == 100.0


def test_throughput_pct_used_returns_none_without_a_real_cap():
    assert throughput_pct_used(1000.0, 0.0) is None
    assert throughput_pct_used(1000.0, None) is None


def test_equivalent_full_cycles_is_discharge_over_capacity():
    assert equivalent_full_cycles(72.0, 36.0) == 2.0
    assert equivalent_full_cycles(0.0, 36.0) == 0.0


def test_equivalent_full_cycles_returns_none_without_a_real_capacity():
    assert equivalent_full_cycles(72.0, 0.0) is None
    assert equivalent_full_cycles(72.0, None) is None


def test_warranty_years_remaining_counts_down_from_install_date():
    remaining = warranty_years_remaining(date(2025, 1, 1), 10, date(2026, 1, 1))
    assert abs(remaining - 9.0) < 0.01


def test_warranty_years_remaining_goes_negative_once_expired():
    remaining = warranty_years_remaining(date(2010, 1, 1), 10, date(2026, 1, 1))
    assert remaining < 0


def test_parse_install_date_accepts_uk_dash_format():
    assert parse_install_date("22-07-2026") == date(2026, 7, 22)


def test_parse_install_date_accepts_uk_slash_format():
    assert parse_install_date("22/07/2026") == date(2026, 7, 22)


def test_parse_install_date_accepts_iso_format():
    assert parse_install_date("2026-07-22") == date(2026, 7, 22)


def test_parse_install_date_unambiguous_even_for_day_under_13():
    # The classic ambiguous case (day <= 12, could look like month-first
    # in a format this module doesn't even support) -- confirms UK
    # day-month-year is what's actually used, not silently misread.
    assert parse_install_date("05-03-2026") == date(2026, 3, 5)


def test_parse_install_date_returns_none_for_garbage():
    assert parse_install_date("not a date") is None
    assert parse_install_date("") is None

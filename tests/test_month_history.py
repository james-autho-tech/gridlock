"""Heat pump COP and car charging by month and year."""
from datetime import date, datetime, timedelta, timezone

from core import cop_report as cop
from core import ev_history as evh


def _ledger_with_days(first, n, heat=20.0, elec=5.0):
    led = cop.clean_ledger({})
    d = first
    for _ in range(n):
        day = cop.empty_day()
        day["heat"]["heating"], day["elec"]["heating"] = heat, elec
        day["run_h"]["heating"], day["hours"] = 10.0, 24.0
        day["out_all"] = 24.0 * 5.0
        led["days"][d.isoformat()] = day
        d += timedelta(days=1)
    return led


def test_months_come_from_the_ledger_once_it_covers_them_and_from_the_meters_before():
    led = _ledger_with_days(date(2026, 11, 3), 10)
    cop.set_imported(led, {"2026-10": {"heat": 600.0, "elec": 130.0, "outdoor": 12.0},
                           "2026-11": {"heat": 300.0, "elec": 70.0, "outdoor": 7.0},
                           "2025-11": {"heat": 400.0, "elec": 120.0, "outdoor": 6.0}})
    months, years = cop.months_and_years(led, date(2026, 11, 12), {"pump_w": 0, "standby_w": 0})
    by = {r["month"]: r for r in months}
    assert by["2026-10"]["source"] == "meters" and by["2026-10"]["cop"] == round(600 / 130, 2)
    # Ledger began on the 3rd: the month's meter totals are the whole month.
    assert by["2026-11"]["source"] == "meters" and by["2026-11"]["heat_kwh"] == 300.0
    assert by["2026-11"]["last_year"]["cop"] == round(400 / 120, 2)
    assert [y["year"] for y in years] == ["2025", "2026"]
    assert years[1]["heat_kwh"] == 900.0


def test_a_month_the_ledger_covers_from_the_first_has_the_split_and_the_allowance():
    led = _ledger_with_days(date(2026, 12, 1), 31)
    cop.set_imported(led, {"2026-12": {"heat": 1.0, "elec": 1.0, "outdoor": None}})
    months, _ = cop.months_and_years(led, date(2027, 1, 5), {"pump_w": 70, "standby_w": 20})
    dec = months[0]
    assert dec["source"] == "ledger" and dec["heat_kwh"] == 620.0
    assert dec["heating_cop"] is not None and dec["outdoor"] == 5.0
    assert dec["elec_kwh"] > 155.0           # 155 metered plus the pump/standby allowance


def test_old_days_fold_into_their_month_instead_of_disappearing():
    led = _ledger_with_days(date(2025, 1, 1), cop.KEEP_DAYS)
    tz = timezone.utc
    t = datetime(2026, 2, 10, 12, 0, tzinfo=tz)
    cop.add_reading(led, t, 100.0, 30.0, "heating")
    cop.add_reading(led, t + timedelta(minutes=5), 100.5, 30.1, "heating")
    assert "2025-01-01" not in led["days"]
    assert led["months"]["2025-01"]["days"] == 1
    months, _ = cop.months_and_years(led, date(2026, 2, 10))
    jan = next(r for r in months if r["month"] == "2025-01")
    assert jan["heat_kwh"] == 620.0        # 31 days x 20, the first from the folded bucket
    # And it survives a save/load.
    assert cop.clean_ledger(led)["months"]["2025-01"]["days"] == 1


def test_car_months_cost_per_mile_and_estimates_before_tracking():
    months = evh.months_from_sessions([
        {"stem": "semi", "start": "2026-11-02T22:00:00+00:00", "kwh": 40.0, "cost_gbp": 2.80},
        {"stem": "semi", "start": "2026-11-09T22:00:00+00:00", "kwh": 20.0, "cost_gbp": 1.40},
    ])
    imported = {"semi": {"2026-10": {"miles": 900.0, "added_kwh": 300.0},
                         "2026-11": {"miles": 600.0, "added_kwh": 65.0}}}
    s = evh.summarise(months, imported, "2026-11")["semi"]
    nov = s["months"][-1]
    assert nov["cost_gbp"] == 4.2 and nov["sessions"] == 2 and nov["p_per_kwh"] == 7.0
    assert nov["p_per_mile"] == 0.7        # 420p / 600 miles
    oct_ = s["months"][0]
    assert oct_["estimated"] and oct_["cost_gbp"] == 21.0   # 300 kWh at the car's own 7p average
    assert s["years"][0]["cost_gbp"] == 25.2 and s["years"][0]["estimated"]
    assert evh.to_miles(1609.344, "km") == 1000.0

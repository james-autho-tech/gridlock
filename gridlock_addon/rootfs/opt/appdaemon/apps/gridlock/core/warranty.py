"""Battery warranty tracking — pure percentage/date maths, kept separate
from gridlock.py's own HA state glue so it's unit-testable.

Sigenergy's SigenStor warranty (confirmed from published EU documentation,
not a UK-specific source — see DOCS.md) is throughput-based, not a cycle
count: the battery is covered for `warranty_years` years OR until a fixed
total energy throughput cap is reached, whichever comes first, alongside
a minimum capacity-retention guarantee over that period.
"""

from datetime import datetime

# UK dates first (day-month-year) — this project's own config convention;
# ISO is also accepted since that's what a value read back from previously
# persisted state, or a config written by someone else's habit, would
# already be in. Deliberately no US month-first format: with only these
# two supported, a date string is never genuinely ambiguous between them
# (a 4-digit year only ever appears in one position in either format).
_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d")


def parse_install_date(value):
    """Returns a date object, or None if the string doesn't match any
    supported format — never guesses at reordering ambiguous digits."""
    s = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def throughput_used_mwh(lifetime_discharge_kwh):
    """Which side of charge/discharge Sigenergy's own throughput cap
    actually counts (charge energy in, discharge energy out, or both
    combined) isn't confirmed from published documentation. Discharge
    is used here — the more conservative choice, and the more common
    convention for a "useful energy delivered" throughput warranty —
    with lifetime charge still tracked and shown separately so it can
    be checked against your own confirmed warranty wording."""
    return lifetime_discharge_kwh / 1000.0


def throughput_pct_used(lifetime_discharge_kwh, throughput_cap_mwh):
    """Returns None when there's no real cap to measure against (a
    zero/negative config value), rather than dividing by zero."""
    if throughput_cap_mwh is None or throughput_cap_mwh <= 0:
        return None
    return min(100.0, throughput_used_mwh(lifetime_discharge_kwh) / throughput_cap_mwh * 100.0)


def equivalent_full_cycles(lifetime_discharge_kwh, capacity_kwh):
    """A full cycle = discharging the equivalent of the battery's whole
    nameplate capacity once. Purely an intuitive, informational figure
    for the dashboard — Sigenergy's real warranty measures throughput
    (above), not cycles, so this isn't what's actually being tracked
    against the warranty, just a more familiar way to picture the same
    number."""
    if capacity_kwh is None or capacity_kwh <= 0:
        return None
    return lifetime_discharge_kwh / capacity_kwh


def warranty_years_remaining(install_date, warranty_years, today):
    """install_date/today: date objects. Negative once the calendar
    limit has already passed — a caller decides how to present that
    (e.g. "warranty period ended"), this just does the arithmetic."""
    elapsed_years = (today - install_date).days / 365.25
    return warranty_years - elapsed_years

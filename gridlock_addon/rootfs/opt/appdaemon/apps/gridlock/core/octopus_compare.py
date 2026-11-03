"""Live Octopus tariffs for the tariff comparison.

Real published prices for your region from Octopus's public API (no key),
in place of rates typed into apps.yaml. Octopus publishes tariffs in two
shapes:

- single-register (Go, Cosy, Flexible): a list of unit-rate records, each
  valid for a stretch of time. Used as-is.
- four-rate EV (Intelligent Octopus Go since October 2026): a day rate and
  a night rate, with the night window stated in the product description.

Pure functions; gridlock.py does the HTTP.
"""
import re
from datetime import datetime, timedelta, time as dtime

API = "https://api.octopus.energy/v1"
FAMILIES = ("Intelligent Octopus Go", "Octopus Go", "Cosy Octopus", "Flexible Octopus")
PAYMENT = "DIRECT_DEBIT"
DEFAULT_IOG_NIGHT = ("23:30", "05:30")


def _iso(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def pick_product(products, display_name, now):
    """The newest household product currently on sale under exactly this name."""
    live = [p for p in products or []
            if p.get("display_name") == display_name and not p.get("is_business")
            and p.get("available_from") and _iso(p["available_from"]) <= now
            and (not p.get("available_to") or _iso(p["available_to"]) > now)]
    return max(live, key=lambda p: _iso(p["available_from"])) if live else None


def region_tariff(detail, region):
    """("single" | "four_rate", tariff dict) for the region, direct debit
    preferred, or (None, None) if the product isn't offered there."""
    key = f"_{region}"
    for kind, field in (("single", "single_register_electricity_tariffs"),
                        ("four_rate", "four_rate_ev_electricity_tariffs")):
        by_region = detail.get(field)
        if isinstance(by_region, dict) and isinstance(by_region.get(key), dict):
            options = by_region[key]
            tariff = options.get("direct_debit_monthly") or next(iter(options.values()), None)
            if tariff:
                return kind, tariff
    return None, None


def link(tariff, rel):
    for item in tariff.get("links") or []:
        if item.get("rel") == rel:
            return item.get("href")
    return None


def night_window(description):
    """The off-peak clock window from a description like
    '... between 23:30 - 05:30 every night ...'."""
    m = re.search(r"(\d{1,2}:\d{2})\s*(?:-|to|and)\s*(\d{1,2}:\d{2})", description or "")
    return (m.group(1).zfill(5), m.group(2).zfill(5)) if m else DEFAULT_IOG_NIGHT


def windows_from_records(records, payment=PAYMENT):
    """Unit-rate records -> [(start, end, £/kWh)]. Records for a different
    payment method are skipped; an open-ended record runs to far future."""
    out = []
    for r in records or []:
        if r.get("payment_method") not in (None, payment) or r.get("value_inc_vat") is None:
            continue
        start = _iso(r["valid_from"]) if r.get("valid_from") else None
        if start is None:
            continue
        end = _iso(r["valid_to"]) if r.get("valid_to") else start + timedelta(days=3650)
        out.append((start, end, r["value_inc_vat"] / 100.0))
    return sorted(out)


def windows_from_clock(night, day_rate, night_rate, start_day, days, tz):
    """Day/night rates on a fixed local clock window -> [(start, end, £/kWh)]
    covering `days` days from local midnight of start_day."""
    ns, ne = (dtime.fromisoformat(t) for t in night)
    out = []
    for i in range(-1, days + 1):
        d = start_day + timedelta(days=i)
        n_start = datetime.combine(d, ns, tz)
        n_end = datetime.combine(d + timedelta(days=1) if ne <= ns else d, ne, tz)
        out.append((n_start, n_end, night_rate))
    out.sort()
    filled = []
    for (s, e, r), nxt in zip(out, out[1:] + [None]):
        filled.append((s, e, r))
        if nxt and e < nxt[0]:
            filled.append((e, nxt[0], day_rate))
    return filled


def standing_gbp(records, payment=PAYMENT):
    for r in records or []:
        if r.get("payment_method") in (None, payment) and r.get("value_inc_vat") is not None:
            return r["value_inc_vat"] / 100.0
    return None


def describe(windows):
    """Short text for the dashboard: the distinct rates, cheapest first."""
    rates = sorted({round(r * 100, 1) for _, _, r in windows})
    if not rates:
        return ""
    return f"{rates[0]}p" if len(rates) == 1 else f"{rates[0]}p to {rates[-1]}p"

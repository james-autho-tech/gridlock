"""Car charging by month and year: what home charging cost, and per mile.

Two sources, per car (its entity stem):

- GridLock's own charging sessions at home (kWh and cost at the rate each
  half-hour was charged at), folded into a month as each session ends:
  months[stem]["YYYY-MM"] = {"kwh", "cost_gbp", "sessions"}. Kept for good;
  the session list itself only keeps the last 100.
- HA's long-term statistics, by month: the car's odometer (miles driven) and
  the car's own "charge energy added" (every charge, home or away), stored as
  imported[stem]["YYYY-MM"] = {"miles", "added_kwh"}.

A month before GridLock tracked sessions has its cost estimated from the
car's energy added at the car's own tracked average p/kWh (flagged
estimated). Pence per mile is home charging cost over miles driven, so a car
that also charges away from home looks cheaper than it is; the energy-added
figure alongside shows when that's happening.

Pure functions; gridlock.py feeds it, the dashboard reads the summary.
"""

KM_PER_MILE = 1.609344


def add_session(months, sess):
    """Fold one finished session into its car's month (the month it started)."""
    stem, start = sess.get("stem"), str(sess.get("start") or "")
    if not stem or len(start) < 7:
        return
    m = months.setdefault(stem, {}).setdefault(start[:7], {"kwh": 0.0, "cost_gbp": 0.0, "sessions": 0})
    m["kwh"] = round(m["kwh"] + float(sess.get("kwh") or 0.0), 3)
    m["cost_gbp"] = round(m["cost_gbp"] + float(sess.get("cost_gbp") or 0.0), 4)
    m["sessions"] += 1


def months_from_sessions(done):
    """The month totals rebuilt from a session list (first run after upgrading)."""
    months = {}
    for sess in done or []:
        add_session(months, sess)
    return months


def to_miles(value, unit):
    if value is None:
        return None
    return float(value) / KM_PER_MILE if str(unit or "").lower() in ("km", "kilometers", "kilometres") else float(value)


def summarise(months, imported, today_month):
    """{stem: {"months": [...], "years": [...]}} newest last."""
    out = {}
    for stem in sorted(set(months or {}) | set(imported or {})):
        mine, imp = (months or {}).get(stem, {}), (imported or {}).get(stem, {})
        tracked_kwh = sum(v["kwh"] for v in mine.values())
        avg_p = (sum(v["cost_gbp"] for v in mine.values()) * 100 / tracked_kwh) if tracked_kwh >= 5 else None
        rows = []
        for m in sorted(set(mine) | set(imp)):
            if m > today_month:
                continue
            s, i = mine.get(m), imp.get(m) or {}
            miles = i.get("miles")
            added = i.get("added_kwh")
            if s:
                kwh, cost, est = s["kwh"], s["cost_gbp"], False
            elif added and avg_p is not None:
                kwh, cost, est = added, added * avg_p / 100, True
            else:
                kwh, cost, est = added, None, False
            rows.append({"month": m, "kwh": round(kwh, 1) if kwh is not None else None,
                         "cost_gbp": round(cost, 2) if cost is not None else None, "estimated": est,
                         "sessions": s["sessions"] if s else None,
                         "p_per_kwh": round(cost * 100 / kwh, 1) if cost is not None and kwh else None,
                         "miles": round(miles) if miles is not None else None,
                         "p_per_mile": round(cost * 100 / miles, 1) if cost is not None and miles and miles >= 20 else None,
                         "added_kwh": round(added, 1) if added is not None else None})
        years = []
        for y in sorted({r["month"][:4] for r in rows}):
            rs = [r for r in rows if r["month"].startswith(y)]
            cost = sum(r["cost_gbp"] for r in rs if r["cost_gbp"] is not None)
            kwh = sum(r["kwh"] for r in rs if r["kwh"] is not None)
            priced_miles = sum(r["miles"] for r in rs if r["miles"] is not None and r["cost_gbp"] is not None)
            cost_with_miles = sum(r["cost_gbp"] for r in rs if r["miles"] is not None and r["cost_gbp"] is not None)
            years.append({"year": y, "months": len(rs), "kwh": round(kwh, 1), "cost_gbp": round(cost, 2),
                          "estimated": any(r["estimated"] for r in rs),
                          "miles": round(sum(r["miles"] for r in rs if r["miles"] is not None)) or None,
                          "p_per_mile": round(cost_with_miles * 100 / priced_miles, 1) if priced_miles >= 20 else None})
        out[stem] = {"months": rows, "years": years, "avg_p_per_kwh": round(avg_p, 1) if avg_p is not None else None}
    return out

"""EDF Flextras free hours: booked automatically, so earned hours get spent.

EDF hands Flextras members free hours (a joining bonus, challenge rewards)
that only count once they're booked on one of the weekend days EDF offers,
as whole hours, and that expire if left. The integration's hours sensor says
how many are unbooked and which days are on offer; GridLock books them
through its book_flextras_hours action. Once booked they arrive as free
electricity windows like any other, so the plan charges the battery and
runs flexible loads into them.

Which hours: "best" (the default) books the hours where a free hour saves
most -- the house's learned use in that hour (heat pump included) times the
rate it would otherwise pay -- soonest day first on a tie, so hours are used
before they expire. "fixed" books one hour a day at a chosen time instead.

Pure functions; gridlock.py does the calls, webui.py validates settings.
"""
import re
from datetime import datetime, timedelta

DEFAULT_SETTINGS = {"auto_book": True, "mode": "best", "start": "14:00"}
MODES = ("best", "fixed")
RETRY_AFTER = timedelta(hours=6)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):00$")


def clean_settings(raw):
    """{"auto_book", "start"} from edf_flextras_settings.json, whatever state it's in."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)
    if "auto_book" in raw:
        out["auto_book"] = raw["auto_book"] not in (False, "false", "off", 0, "0")
    start = str(raw.get("start") or "").strip()
    if re.match(r"^\d:00$", start):
        start = "0" + start
    if _TIME_RE.match(start):
        out["start"] = start
    if raw.get("mode") in MODES:
        out["mode"] = raw["mode"]
    return out


def _slot_key(value):
    """A booked hour from the sensor, as "YYYY-MM-DD HH:MM" (None if unreadable).
    Accepts the action's own string form, an ISO time, or {start: ...}."""
    if isinstance(value, dict):
        value = value.get("start") or value.get("from") or value.get("hour")
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().replace("T", " ")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.strptime(v[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return dt.strftime("%Y-%m-%d %H:%M")


def pick_hours(hours_left, bookable_days, booked, start, value_fn=None):
    """The hours to book: [ "YYYY-MM-DD HH:MM", ... ], at most hours_left.
    With value_fn(date, hour) -> saving: the most valuable hours across the
    bookable days, soonest first on a tie. Without it: `start` on each
    bookable day, soonest first, then the hour after it on each day, and
    so on. Hours already booked are skipped."""
    try:
        n = int(hours_left)
    except (TypeError, ValueError):
        return []
    days = sorted({d for d in (bookable_days or [])
                   if isinstance(d, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d)})
    if n <= 0 or not days:
        return []
    taken = {k for k in (_slot_key(b) for b in (booked or [])) if k}
    if value_fn is not None:
        scored = []
        for d in days:
            for hour in range(24):
                key = f"{d} {hour:02d}:00"
                if key in taken:
                    continue
                try:
                    v = float(value_fn(d, hour))
                except (TypeError, ValueError):
                    continue
                scored.append((-round(v, 6), d, hour, key))
        return [k for *_, k in sorted(scored)[:n]]
    h0 = int(clean_settings({"start": start})["start"][:2])
    out = []
    for step in range(24):
        hour = h0 + step
        if hour > 23:
            break
        for d in days:
            key = f"{d} {hour:02d}:00"
            if key in taken:
                continue
            out.append(key)
            if len(out) >= n:
                return out
    return out

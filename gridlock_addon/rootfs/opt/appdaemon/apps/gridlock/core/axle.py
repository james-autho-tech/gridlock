"""Axle Energy grid events (VPP) — opt-in.

Axle pays for exporting (or, occasionally, importing) during short grid
events, and commands the battery itself through the manufacturer's cloud
while one runs. GridLock only reads the schedule, so it can have the
battery charged beforehand and follow the event instead of fighting it.
Nothing here runs until the user saves an Axle API key on the dashboard.

The API is the one Axle publishes for Home Assistant: GET with a Bearer
token, returning the next event's start_time / end_time (ISO 8601) and
import_export. The response shape is documented as beta, so the parser
accepts a single object, a list, or a list under "events", and treats
anything else as "no event".
"""
from datetime import datetime, timezone

API_URL = "https://api.axle.energy/vpp/home-assistant/event"
DEFAULT_RATE_GBP = 1.0
MAX_RATE_GBP = 5.0
KINDS = ("export", "import")


def clean_settings(raw):
    """{"api_key": str ("" = off), "rate_gbp": float} from the dashboard's
    axle_settings.json, whatever state that file is in."""
    raw = raw if isinstance(raw, dict) else {}
    key = raw.get("api_key")
    key = key.strip() if isinstance(key, str) else ""
    try:
        rate = min(MAX_RATE_GBP, max(0.0, float(raw.get("rate_gbp", DEFAULT_RATE_GBP))))
    except (TypeError, ValueError):
        rate = DEFAULT_RATE_GBP
    return {"api_key": key, "rate_gbp": rate}


def _iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _items(data):
    if isinstance(data, list):
        for item in data:
            yield from _items(item)
    elif isinstance(data, dict):
        if isinstance(data.get("events"), list):
            yield from _items(data["events"])
        else:
            yield data


def parse_events(data, now=None):
    """-> sorted [(start, end, "export"|"import"), ...], dropping anything
    malformed or already over."""
    out = set()
    for item in _items(data):
        start = _iso(item.get("start_time"))
        end = _iso(item.get("end_time"))
        kind = str(item.get("import_export") or "").strip().lower()
        if start is None or end is None or end <= start or kind not in KINDS:
            continue
        if now is not None and end <= now:
            continue
        out.add((start, end, kind))
    return sorted(out)


def to_json(events):
    return [{"start_time": s.isoformat(), "end_time": e.isoformat(), "import_export": k}
            for s, e, k in events]


def event_at(events, now):
    """The event running at `now` as (start, end, kind), or None."""
    return next((ev for ev in events if ev[0] <= now < ev[1]), None)

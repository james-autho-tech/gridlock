from datetime import datetime, timedelta, timezone

from core import axle

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_parses_single_object_list_and_wrapped_events():
    ev = {"start_time": "2026-10-27T17:00:00Z", "end_time": "2026-10-27T18:00:00+00:00",
          "import_export": "export", "updated_at": "2026-10-27T09:00:00Z"}
    want = [(datetime(2026, 10, 27, 17, tzinfo=timezone.utc),
             datetime(2026, 10, 27, 18, tzinfo=timezone.utc), "export")]
    assert axle.parse_events(ev, NOW) == want
    assert axle.parse_events([ev], NOW) == want
    assert axle.parse_events({"events": [ev, ev]}, NOW) == want


def test_no_event_and_junk_parse_to_nothing():
    for data in (None, {}, [], "", {"start_time": None}, {"start_time": "2026-10-27T17:00:00Z"},
                 {"start_time": "x", "end_time": "y", "import_export": "export"},
                 {"start_time": "2026-10-27T18:00:00Z", "end_time": "2026-10-27T17:00:00Z",
                  "import_export": "export"},
                 {"start_time": "2026-10-27T17:00:00Z", "end_time": "2026-10-27T18:00:00Z",
                  "import_export": "sideways"}):
        assert axle.parse_events(data, NOW) == []


def test_finished_events_dropped_and_event_at():
    past = {"start_time": "2026-10-27T10:00:00Z", "end_time": "2026-10-27T11:00:00Z", "import_export": "export"}
    assert axle.parse_events(past, NOW) == []
    events = axle.parse_events(axle.to_json([(NOW - timedelta(minutes=10), NOW + timedelta(minutes=50), "import")]))
    assert axle.event_at(events, NOW)[2] == "import"
    assert axle.event_at(events, NOW + timedelta(hours=1)) is None


def test_settings_blank_key_is_off_and_rate_clamped():
    assert axle.clean_settings(None) == {"api_key": "", "rate_gbp": 1.0}
    assert axle.clean_settings({"api_key": "  abc ", "rate_gbp": "9"}) == {"api_key": "abc", "rate_gbp": 5.0}
    assert axle.clean_settings({"api_key": 5, "rate_gbp": "x"}) == {"api_key": "", "rate_gbp": 1.0}

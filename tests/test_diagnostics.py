from core.diagnostics import compute_state_sessions, classify_entity_history


def test_empty_input_returns_no_sessions():
    assert compute_state_sessions([]) == []


def test_single_unbroken_state_returns_one_open_session():
    changes = [
        {"ts": "2026-08-30T00:00:00+00:00", "state": "off"},
        {"ts": "2026-08-30T00:05:00+00:00", "state": "off"},
        {"ts": "2026-08-30T00:10:00+00:00", "state": "off"},
    ]
    sessions = compute_state_sessions(changes)
    assert sessions == [{"state": "off", "start": "2026-08-30T00:00:00+00:00", "end": None}]


def test_alternating_states_produce_correct_boundaries():
    changes = [
        {"ts": "2026-08-30T00:00:00+00:00", "state": "off"},
        {"ts": "2026-08-30T02:00:00+00:00", "state": "on"},
        {"ts": "2026-08-30T02:45:00+00:00", "state": "off"},
        {"ts": "2026-08-30T06:00:00+00:00", "state": "on"},
    ]
    sessions = compute_state_sessions(changes)
    assert sessions == [
        {"state": "off", "start": "2026-08-30T00:00:00+00:00", "end": "2026-08-30T02:00:00+00:00"},
        {"state": "on", "start": "2026-08-30T02:00:00+00:00", "end": "2026-08-30T02:45:00+00:00"},
        {"state": "off", "start": "2026-08-30T02:45:00+00:00", "end": "2026-08-30T06:00:00+00:00"},
        {"state": "on", "start": "2026-08-30T06:00:00+00:00", "end": None},
    ]


def test_entries_missing_state_or_ts_are_skipped():
    changes = [
        {"ts": "2026-08-30T00:00:00+00:00", "state": "off"},
        {"ts": None, "state": "on"},
        {"ts": "2026-08-30T01:00:00+00:00", "state": None},
        {"ts": "2026-08-30T02:00:00+00:00", "state": "on"},
    ]
    sessions = compute_state_sessions(changes)
    assert sessions == [
        {"state": "off", "start": "2026-08-30T00:00:00+00:00", "end": "2026-08-30T02:00:00+00:00"},
        {"state": "on", "start": "2026-08-30T02:00:00+00:00", "end": None},
    ]


def test_classify_temperature_unit_produces_a_series_not_sessions():
    changes = [
        {"ts": "2026-08-30T00:00:00+00:00", "state": "12.0"},
        {"ts": "2026-08-30T00:30:00+00:00", "state": "11.5"},
        {"ts": "2026-08-30T01:00:00+00:00", "state": "11.0"},
    ]
    sessions, series = classify_entity_history(changes, "°C")
    assert sessions == []
    assert series == [
        {"ts": "2026-08-30T00:00:00+00:00", "value": 12.0},
        {"ts": "2026-08-30T00:30:00+00:00", "value": 11.5},
        {"ts": "2026-08-30T01:00:00+00:00", "value": 11.0},
    ]


def test_classify_non_numeric_states_produce_sessions_not_a_series():
    changes = [
        {"ts": "2026-08-30T00:00:00+00:00", "state": "off"},
        {"ts": "2026-08-30T02:00:00+00:00", "state": "on"},
    ]
    sessions, series = classify_entity_history(changes, None)
    assert series == []
    assert sessions == [
        {"state": "off", "start": "2026-08-30T00:00:00+00:00", "end": "2026-08-30T02:00:00+00:00"},
        {"state": "on", "start": "2026-08-30T02:00:00+00:00", "end": None},
    ]


def test_classify_numeric_without_temperature_unit_is_dropped_entirely():
    changes = [
        {"ts": "2026-08-30T00:00:00+00:00", "state": "412.3"},
        {"ts": "2026-08-30T00:05:00+00:00", "state": "398.1"},
        {"ts": "2026-08-30T00:10:00+00:00", "state": "405.6"},
    ]
    sessions, series = classify_entity_history(changes, "V")
    assert sessions == []
    assert series == []


def test_classify_single_temperature_reading_is_dropped_not_a_one_point_series():
    changes = [{"ts": "2026-08-30T00:00:00+00:00", "state": "12.0"}]
    sessions, series = classify_entity_history(changes, "°C")
    assert sessions == []
    assert series == []

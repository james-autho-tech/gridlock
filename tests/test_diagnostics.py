from core.diagnostics import compute_state_sessions


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

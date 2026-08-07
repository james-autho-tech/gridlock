from datetime import datetime, timezone

from core.forecast import LearnedLoadForecastProvider

NOW = datetime(2026, 8, 7, 12, 3, tzinfo=timezone.utc)  # slot_idx 24 (12:00-12:30)
SLOT_IDX = "24"


class _FakeApp:
    """Minimal stand-in for gridlock.py's app — LearnedLoadForecastProvider
    only ever calls get_state(entity_id) with no attribute kwarg."""
    def __init__(self, states):
        self.states = states

    def get_state(self, entity_id):
        return self.states.get(entity_id)


def _provider(tmp_path, app, **kwargs):
    return LearnedLoadForecastProvider(
        app, str(tmp_path / "load_profile.json"),
        load_power_entity="sensor.house_power", **kwargs)


def test_old_flat_format_migrates_into_house_profile(tmp_path):
    """load_profile.json predates circuit tracking — a flat {slot_idx: kwh}
    dict. Upgrading must not silently discard an already-learned baseline
    by treating the whole file as empty just because it lacks the new
    "house"/"circuits" keys."""
    path = tmp_path / "load_profile.json"
    path.write_text('{"5": 1.2, "24": 3.4}')
    provider = LearnedLoadForecastProvider(
        _FakeApp({}), str(path), load_power_entity="sensor.house_power")
    assert provider.house_profile == {"5": 1.2, "24": 3.4}
    assert provider.circuit_profiles == {}


def test_circuit_power_is_subtracted_from_house_and_learned_on_its_own(tmp_path):
    app = _FakeApp({"sensor.house_power": "3.0", "sensor.circuit_a": "1.0"})
    provider = _provider(tmp_path, app, circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    # observed_slot_kwh = kw * (5/60) * 6 == kw * 0.5 (a 5-min sample of a
    # constant kw reading, extrapolated to what a half-hour at that power
    # level would total).
    assert provider.house_profile[SLOT_IDX] == 1.0  # (3.0 - 1.0) * 0.5
    assert provider.circuit_profiles["sensor.circuit_a"][SLOT_IDX] == 0.5  # 1.0 * 0.5


def test_load_kwh_returns_house_residual_plus_circuit_forecasts(tmp_path):
    app = _FakeApp({"sensor.house_power": "3.0", "sensor.circuit_a": "1.0"})
    provider = _provider(tmp_path, app, circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    assert provider.load_kwh(NOW) == 1.0 + 0.5


def test_ev_subtraction_still_works_and_is_not_learned_as_a_circuit(tmp_path):
    """Regression guard: EV already had its own subtraction behaviour
    before circuit tracking existed (a materially more accurate forecast
    lives in core/slots.py's dispatch-window-driven ev_slot_kwh) — it must
    keep being subtracted, but must NOT gain a learned per-circuit profile
    of its own, which would double-count it in load_kwh()."""
    app = _FakeApp({"sensor.house_power": "5.0", "input_boolean.ev": "on",
                     "sensor.ev_power": "2.0"})
    provider = _provider(tmp_path, app, ev_entity="input_boolean.ev",
                          ev_power_entity="sensor.ev_power")
    provider.sample(NOW)
    assert provider.house_profile[SLOT_IDX] == 1.5  # (5.0 - 2.0) * 0.5
    assert provider.circuit_profiles == {}
    assert provider.load_kwh(NOW) == 1.5


def test_ev_and_circuits_both_subtracted_together(tmp_path):
    app = _FakeApp({"sensor.house_power": "10.0", "input_boolean.ev": "on",
                     "sensor.ev_power": "4.0", "sensor.circuit_a": "1.5"})
    provider = _provider(tmp_path, app, ev_entity="input_boolean.ev",
                          ev_power_entity="sensor.ev_power",
                          circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    assert provider.house_profile[SLOT_IDX] == (10.0 - 4.0 - 1.5) * 0.5
    assert provider.circuit_profiles["sensor.circuit_a"][SLOT_IDX] == 1.5 * 0.5
    assert provider.load_kwh(NOW) == (10.0 - 4.0 - 1.5) * 0.5 + 1.5 * 0.5


def test_save_and_reload_round_trips_new_schema(tmp_path):
    app = _FakeApp({"sensor.house_power": "3.0", "sensor.circuit_a": "1.0"})
    provider = _provider(tmp_path, app, circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)

    reloaded = _provider(tmp_path, _FakeApp({}), circuit_power_entities=["sensor.circuit_a"])
    assert reloaded.house_profile[SLOT_IDX] == 1.0
    assert reloaded.circuit_profiles["sensor.circuit_a"][SLOT_IDX] == 0.5


def test_learned_series_reflects_house_residual_only(tmp_path):
    app = _FakeApp({"sensor.house_power": "3.0", "sensor.circuit_a": "1.0"})
    provider = _provider(tmp_path, app, circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    series = provider.learned_series()
    assert {"x": "12:00", "y": 1.0} in series

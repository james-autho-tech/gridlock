from datetime import datetime, timedelta, timezone

from core.forecast import LearnedLoadForecastProvider

NOW = datetime(2026, 8, 7, 12, 3, tzinfo=timezone.utc)  # slot_idx 24 (12:00-12:30)
NEXT_SLOT = NOW + timedelta(minutes=30)  # slot_idx 25 (12:30-13:00)
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
    # A single 5-minute tick only accumulates — see
    # test_sample_only_blends_into_the_profile_once_the_slot_completes for
    # why it doesn't land in house_profile until the slot actually rolls
    # over. Advancing into the next slot flushes slot 24's real total: one
    # tick's worth of (kw * 5/60), not kw extrapolated to a full half hour.
    provider.sample(NEXT_SLOT)
    assert provider.house_profile[SLOT_IDX] == (3.0 - 1.0) * (5 / 60)
    assert provider.circuit_profiles["sensor.circuit_a"][SLOT_IDX] == 1.0 * (5 / 60)


def test_load_kwh_returns_house_residual_plus_circuit_forecasts(tmp_path):
    app = _FakeApp({"sensor.house_power": "3.0", "sensor.circuit_a": "1.0"})
    provider = _provider(tmp_path, app, circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    provider.sample(NEXT_SLOT)
    assert provider.load_kwh(NOW) == (3.0 - 1.0) * (5 / 60) + 1.0 * (5 / 60)


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
    provider.sample(NEXT_SLOT)
    assert provider.house_profile[SLOT_IDX] == (5.0 - 2.0) * (5 / 60)
    assert provider.circuit_profiles == {}
    assert provider.load_kwh(NOW) == (5.0 - 2.0) * (5 / 60)


def test_ev_and_circuits_both_subtracted_together(tmp_path):
    app = _FakeApp({"sensor.house_power": "10.0", "input_boolean.ev": "on",
                     "sensor.ev_power": "4.0", "sensor.circuit_a": "1.5"})
    provider = _provider(tmp_path, app, ev_entity="input_boolean.ev",
                          ev_power_entity="sensor.ev_power",
                          circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    provider.sample(NEXT_SLOT)
    assert provider.house_profile[SLOT_IDX] == (10.0 - 4.0 - 1.5) * (5 / 60)
    assert provider.circuit_profiles["sensor.circuit_a"][SLOT_IDX] == 1.5 * (5 / 60)
    assert provider.load_kwh(NOW) == (10.0 - 4.0 - 1.5) * (5 / 60) + 1.5 * (5 / 60)


def test_save_and_reload_round_trips_new_schema(tmp_path):
    app = _FakeApp({"sensor.house_power": "3.0", "sensor.circuit_a": "1.0"})
    provider = _provider(tmp_path, app, circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    provider.sample(NEXT_SLOT)

    reloaded = _provider(tmp_path, _FakeApp({}), circuit_power_entities=["sensor.circuit_a"])
    assert reloaded.house_profile[SLOT_IDX] == (3.0 - 1.0) * (5 / 60)
    assert reloaded.circuit_profiles["sensor.circuit_a"][SLOT_IDX] == 1.0 * (5 / 60)


def test_learned_series_reflects_house_residual_only(tmp_path):
    app = _FakeApp({"sensor.house_power": "3.0", "sensor.circuit_a": "1.0"})
    provider = _provider(tmp_path, app, circuit_power_entities=["sensor.circuit_a"])
    provider.sample(NOW)
    provider.sample(NEXT_SLOT)
    series = provider.learned_series()
    assert {"x": "12:00", "y": round((3.0 - 1.0) * (5 / 60), 3)} in series


def test_sample_accumulates_real_ticks_instead_of_extrapolating_each_one(tmp_path):
    """Real bug, confirmed against a live install: this used to treat
    EACH 5-minute reading as if that power level were sustained for the
    FULL 30-minute slot, blending that extrapolated figure into the
    learned average up to six times per slot — a brief appliance spike
    during just one tick got recorded as if it ran for the whole half
    hour. A slot with one 5-minute spike (12kW — a kettle/oven moment)
    surrounded by five ticks of baseline 0.5kW load must learn something
    close to the TRUE total energy for the half hour, not something
    inflated toward "12kW sustained for 30 minutes" (6.0kWh)."""
    app = _FakeApp({"sensor.house_power": "0.5"})
    provider = _provider(tmp_path, app)
    t = NOW.replace(minute=0, second=0, microsecond=0)
    for offset in range(6):
        if offset == 2:
            app.states["sensor.house_power"] = "12.0"  # one brief spike tick
        else:
            app.states["sensor.house_power"] = "0.5"
        provider.sample(t + timedelta(minutes=5 * offset))
    # Roll into the next slot to flush the accumulated total.
    provider.sample(t + timedelta(minutes=30))

    true_total = (0.5 * 5 + 12.0 * 5 + 0.5 * 20) / 60  # 5 baseline ticks + 1 spike tick
    learned = provider.house_profile[str(t.hour * 2 + (1 if t.minute >= 30 else 0))]
    assert abs(learned - true_total) < 1e-9
    assert learned < 1.5, (
        "a single brief spike must not pull the learned slot total anywhere "
        "close to '12kW sustained for the whole half hour' (6.0kWh)"
    )

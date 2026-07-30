from core import dedup

SERVICES = {
    "mode": ("select.sigen_mode", "select/select_option", "option"),
    "disch_kw": ("number.sigen_disch", "number/set_value", "value"),
    "charge_kw": ("number.sigen_charge", "number/set_value", "value"),
}


def test_no_writes_when_state_already_matches():
    current = {"mode": "Maximum Self Consumption", "disch_kw": 10.0, "charge_kw": 10.0}
    desired = dict(current)
    writes = dedup.plan_writes(current, desired, SERVICES)
    assert writes == []


def test_writes_only_the_changed_entities():
    current = {"mode": "Maximum Self Consumption", "disch_kw": 10.0, "charge_kw": 10.0}
    desired = {"mode": "Command Charging (Grid First)", "disch_kw": 10.0, "charge_kw": 8.0}
    writes = dedup.plan_writes(current, desired, SERVICES)
    changed = {w.entity_id for w in writes}
    assert changed == {"select.sigen_mode", "number.sigen_charge"}


def test_float_writes_use_a_tolerance_not_exact_equality():
    current = {"disch_kw": 10.000000001, "charge_kw": 10.0, "mode": "x"}
    desired = {"disch_kw": 10.0, "charge_kw": 10.0, "mode": "x"}
    writes = dedup.plan_writes(current, desired, SERVICES)
    assert writes == []


def test_unknown_current_state_always_writes():
    current = {"mode": None, "disch_kw": None, "charge_kw": None}
    desired = {"mode": "Maximum Self Consumption", "disch_kw": 10.0, "charge_kw": 10.0}
    writes = dedup.plan_writes(current, desired, SERVICES)
    assert len(writes) == 3

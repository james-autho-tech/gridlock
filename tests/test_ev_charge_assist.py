from core.ev_charge_assist import should_charge


def test_charges_when_below_target_and_cheap():
    assert should_charge(50, 80, True) is True


def test_does_not_charge_when_at_or_above_target():
    assert should_charge(80, 80, True) is False
    assert should_charge(90, 80, True) is False


def test_does_not_charge_outside_cheap_window_even_if_below_target():
    assert should_charge(50, 80, False) is False


def test_missing_data_never_charges():
    assert should_charge(None, 80, True) is False
    assert should_charge(50, None, True) is False

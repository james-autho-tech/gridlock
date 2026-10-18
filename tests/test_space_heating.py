from core.space_heating import DEFAULTS, merge_settings, to_zone_cfg, NUMERIC_BOUNDS


def _defaults():
    from core.space_heating import HOT_WATER_DEFAULTS
    return dict(DEFAULTS, hot_water=HOT_WATER_DEFAULTS)


def test_merge_with_nothing_saved_is_the_defaults_and_a_copy():
    merged = merge_settings({})
    assert merged == _defaults()
    merged["control_requires"].clear()
    assert DEFAULTS["control_requires"]  # untouched -- deep copied


def test_merge_overlays_saved_values_and_clamps_numbers():
    merged = merge_settings({"target_temp": 200, "control_safety_min_temp": -40,
                             "heat_loss_watts": "abc", "name": "  Whole house  "})
    assert merged["target_temp"] == NUMERIC_BOUNDS["target_temp"][1]
    assert merged["control_safety_min_temp"] == NUMERIC_BOUNDS["control_safety_min_temp"][0]
    assert merged["heat_loss_watts"] == DEFAULTS["heat_loss_watts"]  # unparseable -> default, not 0
    assert merged["name"] == "Whole house"


def test_merge_blank_entity_means_not_set_not_empty_string():
    merged = merge_settings({"cop_entity": "   ", "weather_entity": None,
                             "control_entity": " switch.x "})
    assert merged["cop_entity"] is None
    assert merged["weather_entity"] is None
    assert merged["control_entity"] == "switch.x"


def test_merge_drops_incomplete_requirement_rows():
    merged = merge_settings({"control_requires": [
        {"entity": "switch.a", "state": "off"},
        {"entity": "", "state": "off"},
        {"entity": "switch.b"},
        "garbage",
        {"entity": "select.m", "state": " Heat "}]})
    assert merged["control_requires"] == [{"entity": "switch.a", "state": "off"},
                                          {"entity": "select.m", "state": "Heat"}]


def test_merge_tolerates_a_non_dict_file():
    assert merge_settings(None) == _defaults()
    assert merge_settings([1, 2]) == _defaults()


def test_to_zone_cfg_maps_onto_the_yaml_zone_shape():
    cfg = to_zone_cfg(merge_settings({"target_temp": 19.5}))
    assert cfg["target_temp_constant"] == 19.5
    assert "target_temp_entity" not in cfg
    assert cfg["heat_share"] == 1.0
    assert cfg["control_entity"] == DEFAULTS["control_entity"]
    assert cfg["control_requires"] == DEFAULTS["control_requires"]


def test_hot_water_settings_merge_and_zone_cfg():
    from core.space_heating import HOT_WATER_DEFAULTS, merge_hot_water, to_hot_water_zone_cfg
    merged = merge_settings({"hot_water": {"run_minutes": 5, "control_entity": " switch.x ", "name": ""}})
    hw = merged["hot_water"]
    assert hw["run_minutes"] == 15.0            # clamped to the floor
    assert hw["control_entity"] == "switch.x"
    assert hw["name"] == HOT_WATER_DEFAULTS["name"]
    assert merge_settings({})["hot_water"] == HOT_WATER_DEFAULTS
    cfg = to_hot_water_zone_cfg(hw)
    assert cfg["tank_litres"] == HOT_WATER_DEFAULTS["tank_litres"]
    assert cfg["control_max_off_hours"] == 0.0
    assert cfg["dhw"]["run_minutes"] == 15.0
    assert cfg["active"] is False


def test_hot_water_ready_by_times_are_cleaned_and_default_to_the_morning():
    from core.space_heating import merge_settings, clean_times
    assert merge_settings({})["hot_water"]["ready_by_times"] == ["06:30"]
    assert clean_times("7:00, 19:15, nonsense, 25:00") == ["07:00", "19:15"]
    assert merge_settings({"hot_water": {"ready_by_times": ""}})["hot_water"]["ready_by_times"] == []

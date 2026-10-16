from core.space_heating import DEFAULTS, merge_settings, to_zone_cfg, NUMERIC_BOUNDS


def test_merge_with_nothing_saved_is_the_defaults_and_a_copy():
    merged = merge_settings({})
    assert merged == DEFAULTS
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
    assert merge_settings(None) == DEFAULTS
    assert merge_settings([1, 2]) == DEFAULTS


def test_to_zone_cfg_maps_onto_the_yaml_zone_shape():
    cfg = to_zone_cfg(merge_settings({"target_temp": 19.5}))
    assert cfg["target_temp_constant"] == 19.5
    assert "target_temp_entity" not in cfg
    assert cfg["heat_share"] == 1.0
    assert cfg["control_entity"] == DEFAULTS["control_entity"]
    assert cfg["control_requires"] == DEFAULTS["control_requires"]

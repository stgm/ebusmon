import indicators

HEAT = {"label": "Heat", "conditions": {"three_way_valve": "heat",
                                        "compressor_speed": "on"}}


def latest(**values):
    return {k: {"value": v} for k, v in values.items()}


def test_all_conditions_met():
    snap = latest(three_way_valve="heat", compressor_speed=42)
    assert indicators.derive(snap, [HEAT]) == ["Heat"]


def test_conditions_are_anded():
    """The valve is right but the compressor is off, so the indicator is dark."""
    snap = latest(three_way_valve="heat", compressor_speed=0)
    assert indicators.derive(snap, [HEAT]) == []


def test_missing_field_means_not_met():
    assert indicators.derive(latest(three_way_valve="heat"), [HEAT]) == []


def test_none_value_means_not_met():
    snap = {"three_way_valve": {"value": None}, "compressor_speed": {"value": 42}}
    assert indicators.derive(snap, [HEAT]) == []


def test_string_match_is_case_insensitive_and_partial():
    ind = {"label": "Water", "conditions": {"valve": "warm water"}}
    assert indicators.derive(latest(valve="Warm Water Mode"), [ind]) == ["Water"]


def test_string_match_rejects_a_different_value():
    ind = {"label": "Water", "conditions": {"valve": "warm water"}}
    assert indicators.derive(latest(valve="heating"), [ind]) == []


def test_on_requires_a_positive_number():
    ind = {"label": "Running", "conditions": {"speed": "on"}}
    assert indicators.derive(latest(speed=1), [ind]) == ["Running"]
    assert indicators.derive(latest(speed=0), [ind]) == []
    assert indicators.derive(latest(speed=-1), [ind]) == []


def test_on_rejects_a_string_value():
    """A field that reports text can never satisfy "on"."""
    ind = {"label": "Running", "conditions": {"speed": "on"}}
    assert indicators.derive(latest(speed="fast"), [ind]) == []


def test_yaml_bare_on_is_true_and_behaves_like_the_string():
    ind = {"label": "Running", "conditions": {"speed": True}}
    assert indicators.derive(latest(speed=42), [ind]) == ["Running"]
    assert indicators.derive(latest(speed=0), [ind]) == []


def test_several_indicators_report_independently():
    heat  = {"label": "Heat",  "conditions": {"valve": "heat"}}
    water = {"label": "Water", "conditions": {"valve": "water"}}
    assert indicators.derive(latest(valve="heat"), [heat, water]) == ["Heat"]


def test_no_indicators_configured():
    assert indicators.derive(latest(valve="heat"), []) == []

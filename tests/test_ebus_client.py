import ebus_client


class FakeField:
    def __init__(self, name, unit=""):
        self.name = name
        self.unit = unit


class FakeMsg:
    def __init__(self, name, *fields):
        self.name = name
        self.fields = fields


class FakeEbus:
    def __init__(self, *msgdefs):
        self.msgdefs = msgdefs


# ── parse_value ───────────────────────────────────────────────────────────────

def test_parse_value_passes_through_numbers():
    assert ebus_client.parse_value(21) == 21.0
    assert ebus_client.parse_value(21.5) == 21.5
    assert ebus_client.parse_value(-3.25) == -3.25


def test_parse_value_extracts_a_number_from_a_string():
    assert ebus_client.parse_value("21.5") == 21.5
    assert ebus_client.parse_value("-5") == -5.0
    assert ebus_client.parse_value("21.5 °C") == 21.5


def test_parse_value_handles_scientific_notation():
    assert ebus_client.parse_value("1.5e2") == 150.0
    assert ebus_client.parse_value("2E-2") == 0.02


def test_parse_value_returns_none_for_a_pure_string():
    """A ThreeWayValve position has no number, so the caller stores the raw text."""
    assert ebus_client.parse_value("warm water") is None
    assert ebus_client.parse_value("") is None


def test_parse_value_returns_none_for_other_types():
    assert ebus_client.parse_value(None) is None
    assert ebus_client.parse_value(["21.5"]) is None


def test_parse_value_rounds_to_four_places():
    assert ebus_client.parse_value(1 / 3) == 0.3333


# ── build_field_map ───────────────────────────────────────────────────────────

def test_message_name_is_indexed_to_its_first_field():
    field = FakeField("FlowTemp/value")
    ebus  = FakeEbus(FakeMsg("FlowTemp", field, FakeField("FlowTemp/other")))
    fmap  = ebus_client.build_field_map(ebus)
    assert fmap["flowtemp"][1] is field


def test_lookup_is_case_insensitive():
    ebus = FakeEbus(FakeMsg("RunDataReturnTemp", FakeField("Temp")))
    fmap = ebus_client.build_field_map(ebus)
    assert "rundatareturntemp" in fmap
    assert "RunDataReturnTemp" not in fmap


def test_field_name_overrides_a_message_level_entry():
    """The field-name pass runs second and is more specific, so it wins."""
    first  = FakeField("HwcTemp")
    second = FakeField("HwcTemp")
    ebus   = FakeEbus(FakeMsg("HwcTemp", first), FakeMsg("Other", second))
    fmap   = ebus_client.build_field_map(ebus)
    assert fmap["hwctemp"][1] is second


def test_slash_suffix_is_indexed_under_its_short_name():
    field = FakeField("RunDataCompressorSpeed/value")
    ebus  = FakeEbus(FakeMsg("RunDataCompressorSpeed", field))
    fmap  = ebus_client.build_field_map(ebus)
    assert fmap["rundatacompressorspeed/value"][1] is field
    assert fmap["value"][1] is field


def test_short_name_does_not_displace_an_existing_entry():
    """Short names collide easily, so they only fill gaps."""
    own   = FakeField("value")
    other = FakeField("CompressorSpeed/value")
    ebus  = FakeEbus(FakeMsg("A", own), FakeMsg("B", other))
    fmap  = ebus_client.build_field_map(ebus)
    assert fmap["value"][1] is own


def test_message_with_no_fields_is_skipped():
    ebus = FakeEbus(FakeMsg("Empty"))
    assert ebus_client.build_field_map(ebus) == {}

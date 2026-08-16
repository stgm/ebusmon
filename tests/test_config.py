import pathlib
import textwrap

import pytest

import config


def write_config(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


# ── name conversion ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("FlowTemp",               "flow_temp"),
    ("RunDataCompressorSpeed", "run_data_compressor_speed"),
    ("HwcTemp",                "hwc_temp"),
    ("room_temp",              "room_temp"),
    ("EnergyIntegral",         "energy_integral"),
])
def test_camel_to_snake(name, expected):
    assert config.camel_to_snake(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("FlowTemp",               "Flow temp"),
    ("RunDataCompressorSpeed", "Run data compressor speed"),
    ("room_temp",              "Room temp"),      # already snake_case
    ("outside_air_temp",       "Outside air temp"),
])
def test_camel_to_label(name, expected):
    assert config.camel_to_label(name) == expected


# ── chart parsing ─────────────────────────────────────────────────────────────

def test_field_with_bounds_only(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Flow temp: FlowTemp, -5, 90
    """))
    assert cfg.fields == {"flow_temp": ("FlowTemp", "Flow temp")}
    assert cfg.bounds == {"flow_temp": (-5.0, 90.0)}
    assert cfg.log_key_overrides == {}
    assert cfg.chart_groups == [["flow_temp"]]


def test_field_with_log_key_and_bounds(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Return temp: RunDataReturnTemp, return_temp, -5, 80
    """))
    assert cfg.log_key_overrides == {"run_data_return_temp": "return_temp"}
    assert cfg.bounds == {"run_data_return_temp": (-5.0, 80.0)}


def test_field_with_neither_log_key_nor_bounds(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Heat curve: HeatCurve
    """))
    assert cfg.fields == {"heat_curve": ("HeatCurve", "Heat curve")}
    assert cfg.bounds == {}
    assert cfg.log_key_overrides == {}


def test_two_entries_under_one_dash_share_a_chart(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Water: HwcTemp, dhw_temp, 5, 80
            Target: TargetTempHwc, target_hwc_temp, 10, 80
          - Heat curve: HeatCurve
    """))
    assert cfg.chart_groups == [["hwc_temp", "target_temp_hwc"], ["heat_curve"]]
    assert len(cfg.fields) == 3


def test_display_name_overrides_the_derived_label(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Outdoor: OutdoorTemp, outside_temp, -30, 50
    """))
    assert cfg.fields["outdoor_temp"] == ("OutdoorTemp", "Outdoor")


def test_a_field_repeated_across_charts_is_kept_once(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Flow: FlowTemp, -5, 90
          - Flow again: FlowTemp, -5, 90
    """))
    assert list(cfg.fields) == ["flow_temp"]
    assert cfg.chart_groups == [["flow_temp"], ["flow_temp"]]


# ── indicators ────────────────────────────────────────────────────────────────

def test_indicator_conditions_are_resolved_to_keys(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Compressor: RunDataCompressorSpeed, 0, 200
        indicators:
          - Heat:
              ThreeWayValve: heat
              RunDataCompressorSpeed: "on"
    """))
    assert cfg.indicators == [{
        "label": "Heat",
        "conditions": {"three_way_valve": "heat",
                       "run_data_compressor_speed": "on"},
    }]


def test_indicator_only_fields_become_extra_fields(tmp_path):
    """ThreeWayValve is not charted, so it must still be polled."""
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Compressor: RunDataCompressorSpeed, 0, 200
        indicators:
          - Heat:
              ThreeWayValve: heat
              RunDataCompressorSpeed: "on"
    """))
    assert cfg.extra_fields == {"three_way_valve": "ThreeWayValve"}


def test_bare_on_is_parsed_by_yaml_as_true(tmp_path):
    """Unquoted `on` is a YAML boolean; indicators.derive treats it like "on"."""
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Compressor: RunDataCompressorSpeed, 0, 200
        indicators:
          - Running:
              RunDataCompressorSpeed: on
    """))
    assert cfg.indicators[0]["conditions"]["run_data_compressor_speed"] is True


# ── server / ebusd blocks ─────────────────────────────────────────────────────

def test_host_and_ports_are_read(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        server:
          port: 1234
        ebusd:
          host: 192.168.1.5
          port: 4321
        charts:
          - Flow: FlowTemp
    """))
    assert (cfg.ebusd_host, cfg.ebusd_port, cfg.server_port) == ("192.168.1.5", 4321, 1234)


def test_defaults_apply_when_blocks_are_absent(tmp_path):
    cfg = config.load(write_config(tmp_path, """
        charts:
          - Flow: FlowTemp
    """))
    assert (cfg.ebusd_host, cfg.ebusd_port, cfg.server_port) == ("127.0.0.1", 8888, 6789)
    assert cfg.read_ttl == cfg.poll_interval * 2


def test_the_shipped_config_still_parses():
    """Guards against a change here breaking the example users start from."""
    cfg = config.load(str(pathlib.Path(__file__).parent.parent / "config.yaml"))
    assert cfg.fields
    assert cfg.chart_groups
    assert all(k in cfg.fields or k in cfg.extra_fields
               for ind in cfg.indicators for k in ind["conditions"])

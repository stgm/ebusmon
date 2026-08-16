import dataclasses
import textwrap

import pytest

import aggregate
import config
import state

# Room has bounds; Return adds a log key override; Heat curve has neither.
CONFIG = """
    charts:
      - Room: RoomTemp, 10, 35
      - Return: RunDataReturnTemp, return_temp, -5, 80
      - Heat curve: HeatCurve
"""


@pytest.fixture
def pipeline(tmp_path):
    """
    A loaded config and initialised state, writing to a temporary data dir.

    Returns the Config. Both `state` and `aggregate` keep module-level state, so
    every test that touches them needs this to reset it.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(textwrap.dedent(CONFIG))

    loaded = dataclasses.replace(config.load(str(cfg_path)),
                                 data_dir=tmp_path / "data")
    state.init(loaded)
    aggregate.init()
    return loaded

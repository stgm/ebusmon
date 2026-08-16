"""
Live state: what the HTTP layer serves.

Populated by `init(cfg)` from app.main(). The aggregation pipeline's own working
state — the minute bucket and the correction windows — lives in `aggregate`.
"""
import threading
from collections import deque
from typing import NamedTuple

import config


class Field(NamedTuple):
    """
    A charted field. `unit` is empty until ebusd reports it at startup, which is
    why this lives here and not in Config.

    Serialized to the page as a JSON array; static/app.js destructures it as
    [, label, unit], so the order of these three matters.
    """
    ebus_name: str
    label:     str
    unit:      str


cfg: config.Config | None = None

data_lock = threading.Lock()

fields: dict[str, Field] = {}
series: dict[str, deque] = {}
latest: dict[str, dict]  = {}


def init(loaded: config.Config):
    """Build the runtime containers for a loaded config. Call once, at startup."""
    global cfg, fields, series, latest

    cfg    = loaded
    fields = {k: Field(name, label, "") for k, (name, label) in loaded.fields.items()}
    series = {k: deque(maxlen=loaded.history_points) for k in loaded.fields}
    latest = {}

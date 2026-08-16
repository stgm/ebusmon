import asyncio
import threading
from collections import deque, defaultdict

import config

data_lock = threading.Lock()

series: dict[str, deque] = {k: deque(maxlen=config.HISTORY_POINTS)
                             for k in config.EBUSCTL_FIELDS}
latest:     dict[str, dict] = {}
log_lines:  deque           = deque(maxlen=200)
sse_clients: list           = []

_minute_bucket:  dict[str, list] = defaultdict(list)
_current_minute: str             = ""

_loop: asyncio.AbstractEventLoop | None = None

_windows: dict[str, deque] = {k: deque(maxlen=5) for k in config.BOUNDS}

import re

import config
import state


def run_async(coro):
    """Submit a coroutine to the shared event loop and block until done."""
    import asyncio

    fut = asyncio.run_coroutine_threadsafe(coro, state._loop)
    return fut.result(timeout=15)


async def make_ebus():
    from pyebus import Ebus

    ebus = Ebus(config.EBUSD_HOST, port=config.EBUSD_PORT)
    await ebus.async_load_msgdefs()
    return ebus


def build_field_map(ebus) -> dict[str, object]:
    field_map = {}
    for msgdef in ebus.msgdefs:
        mname = msgdef.name.lower()
        if mname not in field_map:
            fields = list(msgdef.fields)
            if fields:
                field_map[mname] = (msgdef, fields[0])
    for msgdef in ebus.msgdefs:
        for fielddef in msgdef.fields:
            fname = fielddef.name.lower()
            field_map[fname] = (msgdef, fielddef)
            if "/" in fname:
                short = fname.split("/")[-1]
                if short not in field_map:
                    field_map[short] = (msgdef, fielddef)
    return field_map


def parse_value(raw) -> float | None:
    """
    Convert a pyebus field value to float.
    Returns None for pure strings (e.g. ThreeWayValve) so the caller stores raw.
    """
    if isinstance(raw, (int, float)):
        return round(float(raw), 4)
    if isinstance(raw, str):
        m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw)
        return round(float(m.group()), 4) if m else None
    return None

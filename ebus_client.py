"""
Everything that knows about pyebus. Read path only — the app never writes to
the heat pump.
"""
import re


async def make_ebus(host: str, port: int):
    """Create and connect an Ebus instance with msgdefs loaded."""
    from pyebus import Ebus

    ebus = Ebus(host, port=port)
    await ebus.async_load_msgdefs()
    return ebus


def build_field_map(ebus) -> dict[str, object]:
    """
    Map lowercase name → (msgdef, fielddef), indexed three ways in priority order:

    1. msgdef.name — the message name, which is what ebusctl uses.
    2. fielddef.name — more specific, so it overrides a message-level entry.
    3. the last component of a "Message/field" name, e.g. "value" for
       "RunDataCompressorSpeed/value" — only if nothing already claims it, since
       short names collide easily.
    """
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
    Convert a value to float, or None if it holds no number.

    pyebus returns typed values; strings that contain a number (including
    scientific notation) are parsed, and pure strings such as a ThreeWayValve
    position return None so the caller stores the raw text instead.
    """
    if isinstance(raw, (int, float)):
        return round(float(raw), 4)
    if isinstance(raw, str):
        m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw)
        return round(float(m.group()), 4) if m else None
    return None

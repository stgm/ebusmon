import asyncio
import json
from datetime import datetime

import aggregate
import ebus_client
import indicators
import sse
import state


async def _read_field(ebus, field_map: dict, ebus_name: str):
    """Read one field, or return None if it is unknown or the read fails."""
    entry = field_map.get(ebus_name.lower())
    if entry is None:
        return None
    msgdef, _ = entry
    try:
        msg = await ebus.async_read(msgdef, ttl=state.cfg.read_ttl)
        if msg is None:
            return None
        # msg.values is a tuple of field values in order
        return msg.values[0] if len(msg.values) == 1 else msg.values
    except Exception as e:
        print(f"[pyebus] {ebus_name}: {e}")
        return None


async def _async_poll_loop(ebus, field_map: dict):
    current_day = None

    while True:
        now        = datetime.now()
        ts         = now.isoformat(timespec="seconds")
        minute_str = now.strftime("%Y-%m-%dT%H:%M")
        updates    = {}

        # ── Charted fields ────────────────────────────────────────────────────
        for key, (ebus_name, label, unit) in state.fields.items():
            raw_val = await _read_field(ebus, field_map, ebus_name)
            if raw_val is None:
                continue
            value = ebus_client.parse_value(raw_val)
            if value is None:
                continue

            with state.data_lock:
                state.latest[key] = {"value": value, "unit": unit, "label": label,
                                     "raw": str(raw_val), "ts": ts}

            for fix in aggregate.add_sample(key, ts, value):
                updates.setdefault("_fixes", []).append(
                    {"key": key, "ts": fix["ts"], "value": fix["value"]})

            updates[key] = {"ts": ts, "value": value}

        # ── Indicator-only fields (values may be strings) ─────────────────────
        for key, ebus_name in state.cfg.extra_fields.items():
            raw_val = await _read_field(ebus, field_map, ebus_name)
            if raw_val is None:
                continue
            with state.data_lock:
                state.latest[key] = {"value": str(raw_val), "raw": str(raw_val), "ts": ts}

        # ── Minute flush ──────────────────────────────────────────────────────
        aggregate.roll_minute(minute_str)

        # ── Midnight rollover ─────────────────────────────────────────────────
        today_str = now.strftime("%Y-%m-%d")
        if current_day is None:
            current_day = today_str
        if today_str != current_day:
            print(f"[persist] midnight rollover → {today_str}")
            aggregate.reset()
            current_day = today_str
            # Tell the browser to reset all charts to the new day
            sse.broadcast(json.dumps({"type": "midnight"}))

        if updates:
            with state.data_lock:
                active = indicators.derive(state.latest, state.cfg.indicators)
            sse.broadcast(json.dumps({"type": "update", "ts": ts,
                                      "data": updates, "indicators": active}))

        await asyncio.sleep(state.cfg.poll_interval)


async def async_main():
    print("[pyebus] connecting to ebusd…")
    try:
        ebus = await ebus_client.make_ebus(state.cfg.ebusd_host, state.cfg.ebusd_port)
    except Exception as e:
        print(f"[pyebus] failed to connect: {e}")
        return

    print(f"[pyebus] loaded {sum(1 for _ in ebus.msgdefs)} message definitions")
    field_map = ebus_client.build_field_map(ebus)
    print(f"[pyebus] indexed {len(field_map)} fields")

    # Units are only known once ebusd has been asked.
    with state.data_lock:
        for key, field in list(state.fields.items()):
            entry = field_map.get(field.ebus_name.lower())
            if entry:
                _, fielddef = entry
                state.fields[key] = field._replace(unit=fielddef.unit or "")
    print(f"[pyebus] units loaded: { {k: f.unit for k, f in state.fields.items()} }")

    missing = ({f.ebus_name.lower() for f in state.fields.values()} |
               {v.lower() for v in state.cfg.extra_fields.values()}) - set(field_map.keys())
    if missing:
        print(f"[pyebus] WARNING: fields not found in msgdefs: {missing}")

    await _async_poll_loop(ebus, field_map)


def start_async_loop():
    """
    Run the poll loop on its own event loop in this thread. No reference is
    kept because 

    The app is read-only: it polls ebusd and logs, it never writes to the heat
    pump. So this loop has exactly one job and is never addressed from another
    thread — no handle to it is kept, and no sync-to-async bridge is needed.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_main())

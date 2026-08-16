import asyncio
import json

import config
import state
import ebus_client as ebus_mod
import correction
import persistence


def derive_indicators(latest_snap: dict) -> list[str]:
    active = []
    for indicator in config.INDICATORS:
        all_met = True
        for fname, condition in indicator["conditions"].items():
            key   = config._camel_to_snake(fname)
            value = latest_snap.get(key, {}).get("value")
            if value is None:
                all_met = False
                break
            if condition == "on" or condition is True:
                if not (isinstance(value, (int, float)) and value > 0):
                    all_met = False
                    break
            else:
                if str(condition).lower() not in str(value).lower():
                    all_met = False
                    break
        if all_met:
            active.append(indicator["label"])
    return active


def broadcast(payload: str):
    dead = []
    for q in state.sse_clients:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            state.sse_clients.remove(q)
        except ValueError:
            pass


async def _async_poll_loop(ebus, field_map: dict):
    global _current_day
    _current_day = None

    while True:
        from datetime import datetime
        now        = datetime.now()
        ts         = now.isoformat(timespec="seconds")
        minute_str = now.strftime("%Y-%m-%dT%H:%M")
        updates    = {}

        for key, (fname, label, unit) in config.EBUSCTL_FIELDS.items():
            entry = field_map.get(fname.lower())
            if entry is None:
                continue
            msgdef, _ = entry
            try:
                msg = await ebus.async_read(msgdef, ttl=config.READ_TTL)
                if msg is None:
                    continue
                raw_val = msg.values[0] if len(msg.values) == 1 else msg.values
            except Exception as e:
                print(f"[pyebus] {fname}: {e}")
                continue

            value = ebus_mod.parse_value(raw_val)
            if value is None:
                continue

            point = {"ts": ts, "value": value}
            with state.data_lock:
                state.latest[key] = {"value": value, "unit": unit, "label": label,
                                     "raw": str(raw_val), "ts": ts}
                state._minute_bucket[key].append(value)

                if key in state._windows:
                    state._windows[key].append(point)
                    corrections = correction.check_and_correct(key)
                    for c in corrections:
                        updates.setdefault("_fixes", []).append({**c, "key": key})
                    for c in corrections:
                        bucket = state._minute_bucket[key]
                        for i in range(len(bucket) - 1, -1, -1):
                            if bucket[i] != c["value"]:
                                bucket[i] = c["value"]
                                break

            updates[key] = point

        for key, fname in config.EXTRA_FIELDS.items():
            entry = field_map.get(fname.lower())
            if entry is None:
                continue
            msgdef, _ = entry
            try:
                msg = await ebus.async_read(msgdef, ttl=config.READ_TTL)
                if msg is None:
                    continue
                raw_val = msg.values[0] if len(msg.values) == 1 else msg.values
            except Exception as e:
                print(f"[pyebus] {fname}: {e}")
                continue
            with state.data_lock:
                state.latest[key] = {"value": str(raw_val), "raw": str(raw_val), "ts": ts}

        with state.data_lock:
            if state._current_minute and minute_str != state._current_minute:
                persistence.flush_minute_bucket(state._current_minute)
            state._current_minute = minute_str

        today_str = now.strftime("%Y-%m-%d")
        if _current_day is None:
            _current_day = today_str
        if today_str != _current_day:
            print(f"[persist] midnight rollover → {today_str}")
            with state.data_lock:
                for key in config.EBUSCTL_FIELDS:
                    state.series[key].clear()
                    if key in state._windows:
                        state._windows[key].clear()
                state._minute_bucket.clear()
            _current_day = today_str
            broadcast(json.dumps({"type": "midnight"}))

        if updates:
            with state.data_lock:
                indicators = derive_indicators(state.latest)
            broadcast(json.dumps({"type": "update", "ts": ts,
                                  "data": updates, "indicators": indicators}))

        await asyncio.sleep(config.POLL_INTERVAL)


async def async_main():
    print("[pyebus] connecting to ebusd…")
    try:
        ebus = await ebus_mod.make_ebus()
    except Exception as e:
        print(f"[pyebus] failed to connect: {e}")
        return

    print(f"[pyebus] loaded {sum(1 for _ in ebus.msgdefs)} message definitions")
    field_map = ebus_mod.build_field_map(ebus)
    print(f"[pyebus] indexed {len(field_map)} fields")

    for key, (fname, label, _) in config.EBUSCTL_FIELDS.items():
        entry = field_map.get(fname.lower())
        if entry:
            _, fielddef = entry
            config.EBUSCTL_FIELDS[key] = (fname, label, fielddef.unit or "")
    print(f"[pyebus] units loaded: { {k: v[2] for k, v in config.EBUSCTL_FIELDS.items()} }")

    missing = ({v[0].lower() for v in config.EBUSCTL_FIELDS.values()} |
               {v.lower() for v in config.EXTRA_FIELDS.values()}) - set(field_map.keys())
    if missing:
        print(f"[pyebus] WARNING: fields not found in msgdefs: {missing}")

    await _async_poll_loop(ebus, field_map)


def start_async_loop():
    state._loop = asyncio.new_event_loop()
    asyncio.set_event_loop(state._loop)
    state._loop.run_until_complete(async_main())

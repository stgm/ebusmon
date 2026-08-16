import json
from datetime import date

import config
import state


def _day_file(d: date):
    config.DATA_DIR.mkdir(exist_ok=True)
    return config.DATA_DIR / f"{d.isoformat()}.jsonl"


def _append_record(record: dict):
    path = _day_file(date.today())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_today() -> list[dict]:
    path = _day_file(date.today())
    records = []
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def flush_minute_bucket(minute_str: str):
    """
    Average the accumulated values for `minute_str`, write to disk,
    and push each key into the in-memory series deque.
    Called with state.data_lock HELD.
    """
    if not state._minute_bucket:
        return
    ts = minute_str + ":30"
    record: dict = {"ts": ts}
    for key, values in state._minute_bucket.items():
        if values:
            log_key = config.LOG_KEY_OVERRIDES.get(key, key)
            record[log_key] = round(sum(values) / len(values), 3)

    for key, values in state._minute_bucket.items():
        if values:
            state.series[key].append(
                {"ts": ts, "value": record[config.LOG_KEY_OVERRIDES.get(key, key)]}
            )
    state._minute_bucket.clear()

    try:
        _append_record(record)
    except Exception as e:
        print(f"[persist] write error: {e}")


def restore_today():
    records = load_today()
    print(f"[persist] restoring {len(records)} minute records from today's file")
    with state.data_lock:
        for record in records:
            ts = record.get("ts", "")
            for key in list(config.EBUSCTL_FIELDS.keys()):
                value = record.get(config.LOG_KEY_OVERRIDES.get(key, key))
                if value is not None:
                    state.series[key].append({"ts": ts, "value": value})


def shutdown_flush():
    with state.data_lock:
        if state._minute_bucket and state._current_minute:
            print(f"[persist] flushing on shutdown: {state._current_minute}")
            flush_minute_bucket(state._current_minute)

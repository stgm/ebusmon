"""
Reading and writing the daily .jsonl files.

One file per day, one JSON object per line, each holding the averaged values for
one minute. Field names on disk follow log_key_overrides, which is why pivoting
a file back into per-key series belongs here rather than in the routes.
"""
import json
import re
from datetime import date

import state

_DAY_NAME = re.compile(r"\d{4}-\d{2}-\d{2}")


def _day_file(d: date):
    return state.cfg.data_dir / f"{d.isoformat()}.jsonl"


def append_record(record: dict):
    """Append one averaged minute record to today's file."""
    state.cfg.data_dir.mkdir(exist_ok=True)
    with open(_day_file(date.today()), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def available_days() -> list[str]:
    """Day strings (YYYY-MM-DD) that have a data file, oldest first."""
    if not state.cfg.data_dir.exists():
        return []
    return sorted(p.stem for p in state.cfg.data_dir.glob("*.jsonl")
                  if _DAY_NAME.match(p.stem))


def load_day(d: date) -> list[dict]:
    """
    Every record for one day, or an empty list if there is no file.
    Malformed lines are skipped — a crash mid-write can leave a partial one.
    """
    path = _day_file(d)
    if not path.exists():
        return []

    records = []
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


def day_series(d: date) -> dict[str, list[dict]]:
    """One day's file as per-key series, ready to serve."""
    return _pivot(load_day(d))


def restore_today():
    """
    Repopulate the in-memory series from today's file, so a restart does not
    lose the day so far.
    """
    records = load_day(date.today())
    print(f"[persist] restoring {len(records)} minute records from today's file")
    with state.data_lock:
        for key, points in _pivot(records).items():
            state.series[key].extend(points)


def _pivot(records: list[dict]) -> dict[str, list[dict]]:
    """
    Records → {key: [{ts, value}, ...]}, resolving log key overrides.

    Keys in the file that are no longer configured are dropped, and configured
    keys missing from a record are skipped — either can happen when the config
    changes part-way through a day.
    """
    out: dict[str, list[dict]] = {key: [] for key in state.fields}
    for record in records:
        ts = record.get("ts", "")
        for key in out:
            value = record.get(state.cfg.log_key_overrides.get(key, key))
            if value is not None:
                out[key].append({"ts": ts, "value": value})
    return out

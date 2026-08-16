"""
From raw readings to one averaged record per minute.

Readings arrive every poll cycle. They are collected per minute, corrected for
out-of-bounds glitches, then averaged and written once the minute is over —
both to the in-memory series the dashboard draws and to today's .jsonl file.

This module owns the working state of that pipeline. Nothing outside it should
reach into the bucket or the correction windows; `state` holds only what the
HTTP layer serves.

All public functions take state.data_lock themselves — callers must not hold it.
"""
from collections import defaultdict, deque

import correction
import persistence
import state

# key → [(ts, value), ...] for the minute in progress. Pairs, not bare values,
# so a correction can be applied to the exact reading it belongs to.
minute_bucket: dict[str, list[tuple[str, float]]] = defaultdict(list)

# key → recent raw readings, for out-of-bounds correction. Only bounded fields.
windows: dict[str, deque] = {}

current_minute: str = ""


def init():
    """Build the pipeline's containers for the loaded config. Call after state.init()."""
    global current_minute
    windows.clear()
    windows.update({k: deque(maxlen=correction.WINDOW) for k in state.cfg.bounds})
    minute_bucket.clear()
    current_minute = ""


def add_sample(key: str, ts: str, value: float) -> list[dict]:
    """
    Record one reading for `key`.

    Returns any corrections the reading brought to light, as
    {"ts", "value", "was"} dicts. These apply to earlier timestamps — a glitch
    is only correctable once a good reading arrives after it — so the caller
    should forward them to the browser to patch points already plotted.
    """
    fixes: list[dict] = []

    with state.data_lock:
        bucket = minute_bucket[key]
        bucket.append((ts, value))

        win = windows.get(key)
        if win is None:            # field has no bounds, so nothing to correct
            return fixes

        lo, hi = state.cfg.bounds[key]
        win.append({"ts": ts, "value": value})

        fixes = correction.correct_window(list(win), lo, hi)
        if fixes:
            by_ts    = {f["ts"]: f["value"] for f in fixes}
            repaired = [{**p, "value": by_ts.get(p["ts"], p["value"])} for p in win]
            win.clear()
            win.extend(repaired)

            # Apply to the minute in progress so the average uses corrected
            # values. A correction for an already-flushed minute matches nothing.
            for i, (sample_ts, _) in enumerate(bucket):
                if sample_ts in by_ts:
                    bucket[i] = (sample_ts, by_ts[sample_ts])

    for fix in fixes:
        print(f"[bounds] {key}: {fix['was']} out of [{lo}, {hi}], "
              f"corrected → {fix['value']}")
    return fixes


def roll_minute(minute_str: str):
    """Flush the previous minute once the clock has moved into a new one."""
    global current_minute
    with state.data_lock:
        if current_minute and minute_str != current_minute:
            _flush_locked(current_minute)
        current_minute = minute_str


def reset():
    """Midnight rollover: start the new day with empty series and buckets."""
    with state.data_lock:
        for key in state.fields:
            state.series[key].clear()
        for win in windows.values():
            win.clear()
        minute_bucket.clear()


def shutdown_flush():
    """Do not lose the minute in progress when the process exits."""
    with state.data_lock:
        if minute_bucket and current_minute:
            print(f"[persist] flushing on shutdown: {current_minute}")
            _flush_locked(current_minute)


def _flush_locked(minute_str: str):
    """
    Average the bucket, append to the live series, and write one record.
    Assumes state.data_lock is HELD.
    """
    if not minute_bucket:
        return

    ts = minute_str + ":30"   # represent the minute at its midpoint
    record: dict = {"ts": ts}
    for key, samples in minute_bucket.items():
        if samples:
            log_key = state.cfg.log_key_overrides.get(key, key)
            record[log_key] = round(sum(v for _, v in samples) / len(samples), 3)

    for key, samples in minute_bucket.items():
        if samples:
            log_key = state.cfg.log_key_overrides.get(key, key)
            state.series[key].append({"ts": ts, "value": record[log_key]})
    minute_bucket.clear()

    try:
        persistence.append_record(record)
    except Exception as e:
        print(f"[persist] write error: {e}")

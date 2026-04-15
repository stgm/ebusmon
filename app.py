"""
ebusd Live Dashboard - Flask Backend
Uses pyebus to communicate directly with ebusd over TCP.
"""
import argparse
import asyncio
import threading
import time
import json
import re
import os
import atexit
import yaml
from datetime import datetime, date
from collections import deque, defaultdict
from pathlib import Path
from flask import Flask, Response, render_template, jsonify, request

app = Flask(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
POLL_INTERVAL  = 15                       # seconds between poll cycles
HISTORY_POINTS = 1440                     # one per minute × 24 h = full day in memory
DATA_DIR       = Path("data")             # where daily .jsonl files are stored
# TTL passed to async_read: accept cached values up to this many seconds old.
READ_TTL       = POLL_INTERVAL * 2


def _camel_to_snake(name: str) -> str:
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    return s.lower()


def _camel_to_label(name: str) -> str:
    if name == name.lower():          # already snake_case (e.g. room_temp)
        words = name.split('_')
        return ' '.join([words[0].capitalize()] + words[1:]) if words else name
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1 \2', s)
    words = s.split()
    return ' '.join([words[0].capitalize()] + [w.lower() for w in words[1:]]) if words else name


def _load_config(path: str = "config.yaml") -> dict:
    """Load and parse config.yaml into runtime data structures."""
    with open(path) as f:
        cfg = yaml.safe_load(f)

    ebusd = cfg.get("ebusd", {})
    host  = ebusd.get("host", "127.0.0.1")
    port  = int(ebusd.get("port", 8888))

    # Parse charts: list of {label: "FieldName[, log_key][, min, max]"} dicts
    field_groups: list[list[str]] = []
    bounds:       dict[str, tuple[float, float]] = {}
    label_overrides: dict[str, str] = {}
    log_key_overrides: dict[str, str] = {}

    def _is_number(s: str) -> bool:
        try:
            float(s)
            return True
        except ValueError:
            return False

    for chart_entry in cfg.get("charts", []):
        group_names = []
        for label, value in chart_entry.items():
            parts = [p.strip() for p in str(value).split(",")]
            field_name = parts[0]
            key = _camel_to_snake(field_name)
            label_overrides[key] = label
            group_names.append(field_name)
            rest = parts[1:]
            if rest and not _is_number(rest[0]):
                log_key_overrides[key] = rest[0]
                rest = rest[1:]
            if len(rest) == 2:
                bounds[key] = (float(rest[0]), float(rest[1]))
        field_groups.append(group_names)

    # Parse indicators: list of {label: {field: condition}} dicts
    indicators: list[dict] = []
    for entry in cfg.get("indicators", []):
        for label, conditions in entry.items():
            indicators.append({"label": label, "conditions": dict(conditions)})

    server = cfg.get("server", {})

    return {
        "host":            host,
        "port":            port,
        "server_port":     int(server.get("port", 6789)),
        "field_groups":    field_groups,
        "bounds":          bounds,
        "label_overrides":   label_overrides,
        "log_key_overrides": log_key_overrides,
        "indicators":        indicators,
    }


_parser = argparse.ArgumentParser(description="ebusd Live Dashboard")
_parser.add_argument("--config", "-c", default="config.yaml", metavar="FILE",
                     help="path to config file (default: config.yaml)")
_args, _ = _parser.parse_known_args()   # parse_known_args so Flask/uv args don't cause errors

_cfg = _load_config(_args.config)

EBUSD_HOST  = _cfg["host"]
EBUSD_PORT  = _cfg["port"]
SERVER_PORT = _cfg["server_port"]

# Flatten groups → deduplicated field dict; units filled from ebusd in _async_main.
EBUSCTL_FIELDS: dict[str, tuple] = {}
_seen: set[str] = set()
for _group in _cfg["field_groups"]:
    for _name in _group:
        _key = _camel_to_snake(_name)
        if _key not in _seen:
            _seen.add(_key)
            _label = _cfg["label_overrides"].get(_key) or _camel_to_label(_name)
            EBUSCTL_FIELDS[_key] = (_name, _label, "")
del _seen, _group, _name, _key, _label

# Chart groups as snake_case keys for JS injection
CHART_GROUPS: list[list[str]] = [
    [_camel_to_snake(n) for n in group]
    for group in _cfg["field_groups"]
]

BOUNDS: dict[str, tuple[float, float]] = _cfg["bounds"]

# Maps current snake_case key → old log file key (for reading historical .jsonl files)
LOG_KEY_OVERRIDES: dict[str, str] = _cfg["log_key_overrides"]

# Indicators: list of {label, conditions} — conditions: {field_name: value_or_"on"}
INDICATORS: list[dict] = _cfg["indicators"]

# Extra fields referenced by indicators but not charted (value may be a string)
EXTRA_FIELDS: dict[str, str] = {}
for _ind in INDICATORS:
    for _fname in _ind["conditions"]:
        _key = _camel_to_snake(_fname)
        if _key not in EBUSCTL_FIELDS and _key not in EXTRA_FIELDS:
            EXTRA_FIELDS[_key] = _fname
del _ind, _fname, _key

# ── Shared state ─────────────────────────────────────────────────────────────
data_lock    = threading.Lock()
series: dict[str, deque] = {k: deque(maxlen=HISTORY_POINTS)
                             for k in EBUSCTL_FIELDS}
latest: dict[str, dict]  = {}
log_lines: deque          = deque(maxlen=200)
sse_clients: list         = []

_minute_bucket: dict[str, list] = defaultdict(list)
_current_minute: str             = ""

# Shared asyncio event loop running in background thread
_loop: asyncio.AbstractEventLoop | None = None


# ── pyebus helpers ────────────────────────────────────────────────────────────
def _run_async(coro):
    """Submit a coroutine to the shared event loop and block until done."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=15)


async def _make_ebus():
    """Create and connect an Ebus instance with msgdefs loaded."""
    from pyebus import Ebus
    ebus = Ebus(EBUSD_HOST, port=EBUSD_PORT)
    await ebus.async_load_msgdefs()
    return ebus


def _build_field_map(ebus) -> dict[str, object]:
    """
    Build a dict mapping lowercase field_name → (msgdef, fielddef).
    Indexes by fielddef.name, and also by just the last component after '/'
    so that e.g. "RunDataCompressorSpeed/value" is also reachable as "value"
    -- but the full name takes priority to avoid collisions.
    Also indexes by msgdef.name alone (the message name, without circuit prefix)
    since that's what ebusctl uses.
    """
    field_map = {}
    # First pass: index by msgdef.name (message-level, no field suffix)
    for msgdef in ebus.msgdefs:
        mname = msgdef.name.lower()
        if mname not in field_map:
            # Use first fielddef as representative
            fields = list(msgdef.fields)
            if fields:
                field_map[mname] = (msgdef, fields[0])
    # Second pass: index by fielddef.name (may override message-level if more specific)
    for msgdef in ebus.msgdefs:
        for fielddef in msgdef.fields:
            fname = fielddef.name.lower()
            field_map[fname] = (msgdef, fielddef)
            # Also index by last component after '/' (e.g. "rundatacompressorspeed/value" → "value")
            if "/" in fname:
                short = fname.split("/")[-1]
                if short not in field_map:
                    field_map[short] = (msgdef, fielddef)
    return field_map


def parse_value(raw) -> float | None:
    """
    Convert a pyebus field value to float.
    pyebus returns typed values (float, int, str).  For strings that look
    like numbers (including scientific notation) we parse them; for pure
    strings (e.g. ThreeWayValve) we return None so the caller stores raw.
    """
    if isinstance(raw, (int, float)):
        return round(float(raw), 4)
    if isinstance(raw, str):
        m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw)
        return round(float(m.group()), 4) if m else None
    return None


# ── Bounds-based outlier correction ───────────────────────────────────────────
#
# Any reading outside [lo, hi] is treated as a glitch and replaced with a
# linear interpolation of its surrounding good neighbours (up to 2 consecutive
# bad points are handled). BOUNDS is populated from config.yaml.
#

# Rolling window of the last 5 raw (unfiltered) readings per key
_WINDOW  = 5
_windows: dict[str, deque] = {k: deque(maxlen=_WINDOW) for k in BOUNDS}


def _in_bounds(key: str, value: float) -> bool:
    lo, hi = BOUNDS[key]
    return lo <= value <= hi


def check_and_correct(key: str) -> list[dict]:
    """
    Called after a new raw point has been appended to _windows[key].

    Scans the window from oldest to newest. Any run of 1 or 2 consecutive
    out-of-bounds points that is flanked by in-bounds points on both sides
    is replaced by linear interpolation between those flanking points.

    Returns a list of {ts, value} correction dicts to broadcast (may be empty).
    """
    if key not in BOUNDS:
        return []

    win = list(_windows[key])   # snapshot; indices are stable during iteration
    n   = len(win)
    corrections = []
    i = 0

    while i < n:
        if _in_bounds(key, win[i]["value"]):
            i += 1
            continue

        # Found an out-of-bounds point at i — look ahead for the run length
        run_end = i + 1
        while run_end < n and not _in_bounds(key, win[run_end]["value"]):
            run_end += 1

        run_len = run_end - i   # number of consecutive bad points

        # We can only interpolate if there's a good anchor on both sides
        # and the run is ≤ 2 (longer runs may be a real state change)
        if run_len <= 2 and i > 0 and run_end < n:
            left_val  = win[i - 1]["value"]
            right_val = win[run_end]["value"]
            steps     = run_len + 1

            for offset in range(run_len):
                pt     = win[i + offset]
                interp = round(left_val + (right_val - left_val) * (offset + 1) / steps, 3)
                print(f"[bounds] {key}: {pt['value']} out of {BOUNDS[key]}, "
                      f"corrected → {interp}")
                win[i + offset] = {**pt, "value": interp}
                corrections.append({"ts": pt["ts"], "value": interp})

            # Write corrected values back into the real deque-based window
            real_win = _windows[key]
            for offset in range(run_len):
                idx_from_end = n - (i + offset) - 1
                real_win.rotate(idx_from_end + 1)
                real_win[0] = win[i + offset]
                real_win.rotate(-(idx_from_end + 1))

        i = run_end

    return corrections


# ── Persistence ───────────────────────────────────────────────────────────────
def _day_file(d: date) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR / f"{d.isoformat()}.jsonl"


def _append_record(record: dict):
    """Append a single averaged minute record to today's JSONL file."""
    path = _day_file(date.today())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_today() -> list[dict]:
    """
    Load all records from today's file.
    Returns a list of {ts, <key>: value, ...} dicts.
    Silently skips malformed lines.
    """
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


def _flush_minute_bucket(minute_str: str):
    """
    Average the accumulated values for `minute_str`, write to disk,
    and push each key into the in-memory series deque.
    Called with data_lock HELD.
    """
    if not _minute_bucket:
        return
    ts = minute_str + ":30"   # represent the minute at its midpoint
    record: dict = {"ts": ts}
    for key, values in _minute_bucket.items():
        if values:
            log_key = LOG_KEY_OVERRIDES.get(key, key)
            record[log_key] = round(sum(values) / len(values), 3)

    # Push into live series before clearing the bucket
    for key, values in _minute_bucket.items():
        if values:
            series[key].append({"ts": ts, "value": record[LOG_KEY_OVERRIDES.get(key, key)]})
    _minute_bucket.clear()

    # Persist
    try:
        _append_record(record)
    except Exception as e:
        print(f"[persist] write error: {e}")


def restore_today():
    """
    On startup: read today's JSONL, populate the in-memory series deques.
    Unknown keys in the file are ignored (handles removed fields).
    Keys present in EBUSCTL_FIELDS but absent from a record are skipped
    (handles newly-added fields with no historical data yet).
    """
    records = load_today()
    print(f"[persist] restoring {len(records)} minute records from today's file")
    with data_lock:
        for record in records:
            ts = record.get("ts", "")
            for key in list(EBUSCTL_FIELDS.keys()):
                value = record.get(LOG_KEY_OVERRIDES.get(key, key))
                if value is not None:
                    series[key].append({"ts": ts, "value": value})


def derive_indicators(latest_snap: dict) -> list[str]:
    """Return list of active indicator labels based on current field values."""
    active = []
    for indicator in INDICATORS:
        all_met = True
        for fname, condition in indicator["conditions"].items():
            key   = _camel_to_snake(fname)
            value = latest_snap.get(key, {}).get("value")
            if value is None:
                all_met = False
                break
            if condition == "on" or condition is True:  # YAML may parse bare 'on' as True
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


async def _async_poll_loop(ebus, field_map: dict):
    """Async poll loop: reads all configured fields every POLL_INTERVAL seconds."""
    global _current_minute
    while True:
        now        = datetime.now()
        ts         = now.isoformat(timespec="seconds")
        minute_str = now.strftime("%Y-%m-%dT%H:%M")
        updates    = {}

        # ── Chart fields ──────────────────────────────────────────────────────
        for key, (fname, label, unit) in EBUSCTL_FIELDS.items():
            entry = field_map.get(fname.lower())
            if entry is None:
                continue
            msgdef, _ = entry
            try:
                msg = await ebus.async_read(msgdef, ttl=READ_TTL)
                if msg is None:
                    continue
                # msg.values is a tuple of field values in order
                raw_val = msg.values[0] if len(msg.values) == 1 else msg.values
            except Exception as e:
                print(f"[pyebus] {fname}: {e}")
                continue

            value = parse_value(raw_val)
            if value is None:
                continue

            point = {"ts": ts, "value": value}
            with data_lock:
                latest[key] = {"value": value, "unit": unit, "label": label,
                                "raw": str(raw_val), "ts": ts}
                _minute_bucket[key].append(value)

                if key in _windows:
                    _windows[key].append(point)
                    corrections = check_and_correct(key)
                    for correction in corrections:
                        updates.setdefault("_fixes", []).append({**correction, "key": key})
                    for correction in corrections:
                        bucket = _minute_bucket[key]
                        for i in range(len(bucket) - 1, -1, -1):
                            if bucket[i] != correction["value"]:
                                bucket[i] = correction["value"]
                                break

            updates[key] = point

        # ── Extra (mode indicator) fields ─────────────────────────────────────
        for key, fname in EXTRA_FIELDS.items():
            entry = field_map.get(fname.lower())
            if entry is None:
                continue
            msgdef, _ = entry
            try:
                msg = await ebus.async_read(msgdef, ttl=READ_TTL)
                if msg is None:
                    continue
                raw_val = msg.values[0] if len(msg.values) == 1 else msg.values
            except Exception as e:
                print(f"[pyebus] {fname}: {e}")
                continue
            with data_lock:
                latest[key] = {"value": str(raw_val), "raw": str(raw_val), "ts": ts}

        # ── Minute flush ──────────────────────────────────────────────────────
        with data_lock:
            if _current_minute and minute_str != _current_minute:
                _flush_minute_bucket(_current_minute)
            _current_minute = minute_str

        # ── Midnight rollover ─────────────────────────────────────────────────
        today_str = now.strftime("%Y-%m-%d")
        if not hasattr(_async_poll_loop, "_current_day"):
            _async_poll_loop._current_day = today_str
        if today_str != _async_poll_loop._current_day:
            print(f"[persist] midnight rollover → {today_str}")
            with data_lock:
                for key in EBUSCTL_FIELDS:
                    series[key].clear()
                    _windows[key].clear() if key in _windows else None
                _minute_bucket.clear()
            _async_poll_loop._current_day = today_str
            # Tell the browser to reset all charts to the new day
            _broadcast(json.dumps({"type": "midnight"}))

        if updates:
            with data_lock:
                indicators = derive_indicators(latest)
            _broadcast(json.dumps({"type": "update", "ts": ts,
                                   "data": updates, "indicators": indicators}))

        await asyncio.sleep(POLL_INTERVAL)


async def _async_main():
    """Entry point for the background asyncio loop."""
    print("[pyebus] connecting to ebusd…")
    try:
        ebus = await _make_ebus()
    except Exception as e:
        print(f"[pyebus] failed to connect: {e}")
        return

    print(f"[pyebus] loaded {sum(1 for _ in ebus.msgdefs)} message definitions")
    field_map = _build_field_map(ebus)
    print(f"[pyebus] indexed {len(field_map)} fields")

    # Fill in units from ebusd msgdefs
    for key, (fname, label, _) in EBUSCTL_FIELDS.items():
        entry = field_map.get(fname.lower())
        if entry:
            _, fielddef = entry
            EBUSCTL_FIELDS[key] = (fname, label, fielddef.unit or "")
    print(f"[pyebus] units loaded: { {k: v[2] for k, v in EBUSCTL_FIELDS.items()} }")

    missing = ({v[0].lower() for v in EBUSCTL_FIELDS.values()} |
               {v.lower() for v in EXTRA_FIELDS.values()}) - set(field_map.keys())
    if missing:
        print(f"[pyebus] WARNING: fields not found in msgdefs: {missing}")

    await _async_poll_loop(ebus, field_map)


def _start_async_loop():
    """Start the asyncio event loop in a background thread."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_async_main())


# ── SSE broadcast ─────────────────────────────────────────────────────────────
def _broadcast(payload: str):
    dead = []
    for q in sse_clients:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            sse_clients.remove(q)
        except ValueError:
            pass


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("interface.html",
                           fields=json.dumps(EBUSCTL_FIELDS),
                           chart_groups=json.dumps(CHART_GROUPS),
                           indicators=INDICATORS)


@app.route("/roomtemp", methods=["POST"])
def post_roomtemp():
    raw = request.form.get("current") or request.json and request.json.get("current")
    if raw is None:
        return jsonify({"error": "missing 'current' param"}), 400
    # Normalise comma decimal separator (e.g. "20,5" → "20.5")
    value = parse_value(str(raw).replace(',', '.'))
    if value is None:
        return jsonify({"error": "invalid value"}), 400

    lo, hi = BOUNDS.get("room_temp", (-99, 99))
    if not (lo <= value <= hi):
        return jsonify({"error": f"value {value} out of bounds [{lo}, {hi}]"}), 400

    ts = datetime.now().isoformat(timespec="seconds")
    point = {"ts": ts, "value": value}
    with data_lock:
        latest["room_temp"] = {"value": value, "unit": "°C",
                                "label": "Room Temp", "raw": str(raw), "ts": ts}
        _minute_bucket["room_temp"].append(value)

    print(f"[roomtemp] {ts}: {value} °C")
    _broadcast(json.dumps({"type": "update", "ts": ts,
                           "data": {"room_temp": point},
                           "indicators": derive_indicators(latest)}))
    return jsonify({"ok": True, "ts": ts, "value": value})


@app.route("/api/dates")
def api_dates():
    """Return sorted list of available day strings (YYYY-MM-DD) from DATA_DIR."""
    if not DATA_DIR.exists():
        return jsonify([])
    days = sorted(
        p.stem for p in DATA_DIR.glob("*.jsonl")
        if re.match(r"\d{4}-\d{2}-\d{2}", p.stem)
    )
    return jsonify(days)


@app.route("/api/history")
def api_history():
    req_date = request.args.get("date")  # optional ?date=YYYY-MM-DD

    if req_date and req_date != date.today().isoformat():
        # Serve a past day directly from its .jsonl file
        path = DATA_DIR / f"{req_date}.jsonl"
        records = []
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        # Pivot to per-key series
        all_keys = list(EBUSCTL_FIELDS.keys())
        out: dict = {k: [] for k in all_keys}
        for record in records:
            ts = record.get("ts", "")
            for key in all_keys:
                value = record.get(LOG_KEY_OVERRIDES.get(key, key))
                if value is not None:
                    out[key].append({"ts": ts, "value": value})
        out["latest"] = {}
        out["logs"]   = []
        out["indicators"] = []
        return jsonify(out)

    # Today: serve live in-memory series
    with data_lock:
        out = {k: list(v) for k, v in series.items()}
        out["latest"] = dict(latest)
        out["logs"]   = list(log_lines)
        out["indicators"] = derive_indicators(latest)
    return jsonify(out)


@app.route("/api/stream")
def api_stream():
    import queue
    q = queue.Queue(maxsize=50)
    sse_clients.append(q)

    def generate():
        # Send snapshot of most recent live values for KPI tiles
        with data_lock:
            snap = {k: {"ts": v["ts"], "value": v["value"]}
                    for k, v in latest.items() if "value" in v}
        yield f"data: {json.dumps({'type':'snapshot','data':snap})}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except Exception:
                    yield ": ping\n\n"   # keepalive
        finally:
            try:
                sse_clients.remove(q)
            except ValueError:
                pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})




# ── Startup ───────────────────────────────────────────────────────────────────
def _shutdown_flush():
    """Flush whatever is in the current minute bucket when the process exits."""
    with data_lock:
        if _minute_bucket and _current_minute:
            print(f"[persist] flushing on shutdown: {_current_minute}")
            _flush_minute_bucket(_current_minute)

atexit.register(_shutdown_flush)

if __name__ == "__main__":
    restore_today()
    threading.Thread(target=_start_async_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)

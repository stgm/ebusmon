"""
ebusd Live Dashboard - Flask Backend
Polls ebusctl for heat pump metrics and serves SSE to the frontend.
"""
import subprocess
import threading
import time
import json
import re
import os
import atexit
from datetime import datetime, date
from collections import deque, defaultdict
from pathlib import Path
from flask import Flask, Response, render_template_string, jsonify

app = Flask(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
DOCKER_CONTAINER = "ebusd"                 # change to your container name/id
POLL_INTERVAL    = 5                       # seconds between ebusctl polls
HISTORY_POINTS   = 1440                    # one per minute × 24 h = full day in memory
DATA_DIR         = Path("data")            # where daily .jsonl files are stored

# Fields to poll via: ebusctl read -f <field>
# Format: { "key": ("ebusctl_field_name", "display_label", "unit") }
EBUSCTL_FIELDS = {
    "flow":             ("BuildingCircuitFlow",      "Circuit Flow",         "l/h"),
    "flow_temp":        ("FlowTemp",                 "Flow Temp",            "°C"),
    "target_flow_temp": ("TargetFlowTemp",           "Target Flow Temp",     "°C"),
    "return_temp":      ("RunDataReturnTemp",         "Return Temp",          "°C"),
    "outside_temp":     ("OutdoorTemp",              "Outside Temp",         "°C"),
    "air_intake_temp":  ("AirIntakeTemp",            "Temp on back of pump", "°C"),
    "dhw_temp":         ("HwcTemp",                  "DHW Temp",             "°C"),
    "power_consumption": ("CurrentConsumedPower",    "Power consumption",    "kW"),
    "power_yield":      ("CurrentYieldPower",        "Power yield",          "kW"),
    "cop":              ("COP",                      "COP",                  ""),
    "compressor_speed": ("RunDataCompressorSpeed",   "Compressor Speed",     "rps"),
    "energy_integral":  ("EnergyIntegral",           "Energie-integral",     "ºmin"),
    "heat_curve":       ("HeatCurve",                "Heat curve",           ""),
    "target_room_temp": ("TargetTempHc",             "Target room temp",     "ºC"),
    "target_hwc_temp":  ("TargetTempHwc",            "Target DHW temp",      "ºC"),
}

# Extra fields polled for mode indicators but NOT shown as charts
EXTRA_FIELDS = {
    "three_way_valve": "ThreeWayValve",
}

# ── Shared state ─────────────────────────────────────────────────────────────
data_lock    = threading.Lock()
series: dict[str, deque] = {k: deque(maxlen=HISTORY_POINTS) for k in EBUSCTL_FIELDS}
latest: dict[str, dict]  = {}          # { key: {value, unit, label, ts} }
log_lines: deque          = deque(maxlen=200)
sse_clients: list         = []

# Accumulator for per-minute averaging before writing to disk
_minute_bucket: dict[str, list] = defaultdict(list)   # key → [values in current minute]
_current_minute: str             = ""                  # "YYYY-MM-DDTHH:MM"


# ── ebusctl helpers ──────────────────────────────────────────────────────────
def run_ebusctl(field: str) -> str | None:
    """Run ebusctl read -f <field> inside Docker and return the value string."""
    try:
        result = subprocess.run(
            ["docker", "exec", DOCKER_CONTAINER, "ebusctl", "read", "-f", field],
            capture_output=True, text=True, timeout=8
        )
        out = result.stdout.strip()
        if out and "ERR" not in out.upper():
            return out
    except Exception as e:
        print(f"[ebusctl] {field}: {e}")
    return None


def parse_value(raw: str) -> float | None:
    """Extract the first numeric value from an ebusctl response.
    Handles plain decimals, negatives, and scientific notation (e.g. 2.3549e-38).
    """
    m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw)
    return round(float(m.group()), 4) if m else None


# ── Bounds-based outlier correction ───────────────────────────────────────────
#
# Any reading outside [lo, hi] is treated as a glitch and replaced with a
# linear interpolation of its surrounding good neighbours (up to 2 consecutive
# bad points are handled).
#
# Values are accepted as-is once they return inside bounds — no rate-of-change
# logic, no relative thresholds. Bounds are set to realistic physical limits
# with generous headroom so no legitimate reading is ever rejected.
#
BOUNDS: dict[str, tuple[float, float]] = {
    #                       lo       hi
    "flow_temp":         ( -5.0,    90.0),   # °C  — freeze to boiling headroom
    "target_flow_temp":  ( -5.0,    90.0),   # °C
    "return_temp":       ( -5.0,    80.0),   # °C
    "outside_temp":      (-30.0,    50.0),   # °C  — realistic NL range + margin
    "air_intake_temp":   (-30.0,    60.0),   # °C
    "dhw_temp":          (  5.0,    80.0),   # °C
    "flow":              (  0.0,  3000.0),   # l/h
    "power_consumption": (  0.0,    15.0),   # kW
    "power_yield":       (  0.0,    20.0),   # kW
    "cop":               (  0.0,    10.0),   # –
    "compressor_speed":  (  0.0,   200.0),   # rps
    "energy_integral":   (-300.0,  50.0),    # ºmin
    "heat_curve":        (  0.0,    5.0),    # dimensionless — typical range 0.2–3.0
    "target_room_temp":  ( 10.0,   30.0),    # °C
    "target_hwc_temp":   ( 10.0,   80.0),    # °C
}

# Rolling window of the last 5 raw (unfiltered) readings per key
_WINDOW  = 5
_windows: dict[str, deque] = {k: deque(maxlen=_WINDOW) for k in BOUNDS}


def _patch_series(series_deque: deque, ts: str, value: float):
    """Overwrite the most recent entry with matching ts in series_deque."""
    for i in range(len(series_deque) - 1, -1, -1):
        if series_deque[i]["ts"] == ts:
            series_deque[i] = {**series_deque[i], "value": value}
            return


def _in_bounds(key: str, value: float) -> bool:
    lo, hi = BOUNDS[key]
    return lo <= value <= hi


def check_and_correct(key: str, series_deque: deque) -> list[dict]:
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
            steps     = run_len + 1   # intervals between left anchor and right anchor

            for offset in range(run_len):
                pt    = win[i + offset]
                interp = round(left_val + (right_val - left_val) * (offset + 1) / steps, 3)
                print(f"[bounds] {key}: {pt['value']} out of {BOUNDS[key]}, "
                      f"corrected → {interp}")
                # Patch the window snapshot and the live series deque
                win[i + offset] = {**pt, "value": interp}
                _patch_series(series_deque, pt["ts"], interp)
                corrections.append({"ts": pt["ts"], "value": interp})

            # Write corrected values back into the real deque-based window
            real_win = _windows[key]
            for offset in range(run_len):
                idx_from_end = n - (i + offset) - 1
                # deque doesn't support negative-index assignment; use rotation
                real_win.rotate(idx_from_end + 1)
                real_win[0] = win[i + offset]
                real_win.rotate(-(idx_from_end + 1))

        i = run_end   # skip past the run (corrected or too long)

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
            record[key] = round(sum(values) / len(values), 3)
    _minute_bucket.clear()

    # Persist
    try:
        _append_record(record)
    except Exception as e:
        print(f"[persist] write error: {e}")

    # Also push into live series so restored data appears in charts
    for key in EBUSCTL_FIELDS:
        if key in record:
            series[key].append({"ts": ts, "value": record[key]})


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
            for key in EBUSCTL_FIELDS:
                if key in record:
                    series[key].append({"ts": ts, "value": record[key]})


def derive_mode(latest_snap: dict) -> str:
    """Return 'dhw' | 'heating' | 'idle' based on latest values."""
    compressor = latest_snap.get("compressor_speed", {}).get("value", 0) or 0
    # ThreeWayValve returns a string like "warm water circuit" or "heating circuit"
    valve_raw  = str(latest_snap.get("three_way_valve", {}).get("value", "")).lower()
    valve_dhw  = "warm water" in valve_raw or "dhw" in valve_raw
    if compressor > 0 and valve_dhw:
        return "dhw"
    if compressor > 0:
        return "heating"
    return "idle"


def poll_loop():
    """Background thread: poll all configured fields every POLL_INTERVAL seconds."""
    global _current_minute
    while True:
        now = datetime.now()
        ts  = now.isoformat(timespec="seconds")
        minute_str = now.strftime("%Y-%m-%dT%H:%M")
        updates = {}

        for key, (field, label, unit) in EBUSCTL_FIELDS.items():
            raw = run_ebusctl(field)
            if raw is None:
                continue
            value = parse_value(raw)
            if value is None:
                continue

            point = {"ts": ts, "value": value}
            with data_lock:
                series[key].append(point)
                latest[key] = {"value": value, "unit": unit, "label": label,
                                "raw": raw, "ts": ts}
                _minute_bucket[key].append(value)

                # Feed the trailing window and correct any out-of-bounds points
                if key in _windows:
                    _windows[key].append(point)
                    corrections = check_and_correct(key, series[key])
                    for correction in corrections:
                        updates.setdefault("_fixes", []).append({**correction, "key": key})
                    # Patch the minute bucket: corrections refer to recently
                    # appended values; find them by value match from the end
                    # (bucket has no timestamps, but bad values are distinctive)
                    for correction in corrections:
                        bucket = _minute_bucket[key]
                        for i in range(len(bucket) - 1, -1, -1):
                            if bucket[i] != correction["value"]:
                                bucket[i] = correction["value"]
                                break

            updates[key] = point

        # Poll extra (non-chart) fields — store raw string, not just numeric parse
        for key, field in EXTRA_FIELDS.items():
            raw = run_ebusctl(field)
            if raw is None:
                continue
            with data_lock:
                latest[key] = {"value": raw, "raw": raw, "ts": ts}

        # Flush completed minute to disk when the minute rolls over
        with data_lock:
            if _current_minute and minute_str != _current_minute:
                _flush_minute_bucket(_current_minute)
            _current_minute = minute_str

        if updates:
            with data_lock:
                mode = derive_mode(latest)
            _broadcast(json.dumps({"type": "update", "ts": ts,
                                   "data": updates, "mode": mode}))

        time.sleep(POLL_INTERVAL)


# ── Log tail (optional) ──────────────────────────────────────────────────────
def tail_logs():
    """Tail docker logs and forward [update notice] lines to SSE clients."""
    try:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", "0", DOCKER_CONTAINER],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in proc.stdout:
            line = line.strip()
            if "[update notice]" in line:
                with data_lock:
                    log_lines.append(line)
                _broadcast(json.dumps({"type": "log", "line": line}))
    except Exception as e:
        print(f"[tail_logs] {e}")


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
    return render_template_string(DASHBOARD_HTML,
                                  fields=json.dumps(EBUSCTL_FIELDS))


@app.route("/api/history")
def api_history():
    with data_lock:
        # series deques already contain today's restored + live points
        out = {k: list(v) for k, v in series.items()}
        out["latest"] = dict(latest)
        out["logs"]   = list(log_lines)
        out["mode"]   = derive_mode(latest)
    return jsonify(out)


@app.route("/api/stream")
def api_stream():
    import queue
    q = queue.Queue(maxsize=50)
    sse_clients.append(q)

    def generate():
        # Send initial snapshot
        with data_lock:
            snap = {k: list(v)[-1] if v else None for k, v in series.items()}
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


# ── HTML dashboard (embedded) ────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>eBUS Heat Pump Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #080c12;
    --panel:    #0d1520;
    --border:   #1a2d45;
    --accent:   #00d4ff;
    --accent2:  #ff6b35;
    --accent3:  #39ff14;
    --muted:    #4a6070;
    --text:     #c8dde8;
    --mono:     'Share Tech Mono', monospace;
    --sans:     'Exo 2', sans-serif;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Scanline overlay */
  body::after {
    content:'';
    position:fixed; inset:0; pointer-events:none;
    background: repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      rgba(0,0,0,.08) 2px, rgba(0,0,0,.08) 4px
    );
    z-index:999;
  }

  header {
    display: flex; align-items: center; gap: 1.5rem;
    padding: 1.2rem 2rem;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(90deg, #0d1520 0%, #091018 100%);
  }
  .logo { font-size: 1.6rem; font-weight: 800; letter-spacing:.06em;
          color: var(--accent); text-transform: uppercase; }
  .logo span { color: var(--accent2); }
  .subtitle { font-size: .8rem; color: var(--muted); font-family: var(--mono); }
  .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--accent3); margin-left: auto;
    box-shadow: 0 0 8px var(--accent3);
    animation: pulse 2s ease-in-out infinite;
  }
  .status-dot.offline { background: #ff3b3b; box-shadow: 0 0 8px #ff3b3b; animation: none; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .status-label { font-family: var(--mono); font-size: .75rem; color: var(--muted); }

  /* Mode indicators */
  .mode-badges { display: flex; gap: .6rem; margin-left: auto; align-items: center; }
  .badge {
    display: flex; align-items: center; gap: .45rem;
    padding: .35rem .75rem;
    border-radius: 2px;
    border: 1px solid var(--border);
    font-family: var(--mono); font-size: .7rem; letter-spacing: .08em;
    text-transform: uppercase;
    background: #0a1520;
    color: var(--muted);
    transition: border-color .4s, color .4s, box-shadow .4s;
  }
  .badge-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--muted);
    transition: background .4s, box-shadow .4s;
  }
  .badge.active-heating {
    border-color: #ff6b35;
    color: #ff9a6c;
    box-shadow: 0 0 10px #ff6b3540;
  }
  .badge.active-heating .badge-dot {
    background: #ff6b35;
    box-shadow: 0 0 6px #ff6b35;
    animation: pulse 1.4s ease-in-out infinite;
  }
  .badge.active-dhw {
    border-color: #00d4ff;
    color: #6ee6ff;
    box-shadow: 0 0 10px #00d4ff40;
  }
  .badge.active-dhw .badge-dot {
    background: #00d4ff;
    box-shadow: 0 0 6px #00d4ff;
    animation: pulse 1.4s ease-in-out infinite;
  }

  /* KPI strip */
  .kpi-strip {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 1px;
    background: var(--border);
    border-bottom: 1px solid var(--border);
  }
  .kpi {
    background: var(--panel);
    padding: 1rem 1.2rem;
    cursor: default;
    transition: background .2s;
  }
  .kpi:hover { background: #111e2e; }
  .kpi-label { font-size: .65rem; text-transform: uppercase;
               letter-spacing: .1em; color: var(--muted); margin-bottom: .3rem; }
  .kpi-value { font-family: var(--mono); font-size: 1.6rem; color: var(--accent);
               transition: color .4s; }
  .kpi-value.flash { color: var(--accent3) !important; }
  .kpi-unit  { font-size: .7rem; color: var(--muted); margin-left: .2rem; }

  /* Charts grid */
  .charts-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(480px, 1fr));
    gap: 1px;
    background: var(--border);
    padding: 1px;
  }
  .chart-card {
    background: var(--panel);
    padding: 1.2rem 1.4rem 1rem;
    position: relative;
    overflow: hidden;
  }
  .chart-card::before {
    content:'';
    position:absolute; top:0; left:0; right:0; height:2px;
    background: linear-gradient(90deg, var(--accent) 0%, transparent 100%);
  }
  .chart-title {
    font-size: .72rem; text-transform: uppercase; letter-spacing: .12em;
    color: var(--muted); margin-bottom: .8rem; font-family: var(--mono);
  }
  canvas { width: 100% !important; }

  /* Log panel */
  .log-section { padding: 1rem 1.4rem; border-top: 1px solid var(--border); }
  .log-header { font-size: .7rem; text-transform: uppercase; letter-spacing: .12em;
                color: var(--muted); margin-bottom: .6rem; font-family: var(--mono); }
  #log-box {
    background: #050a0f;
    border: 1px solid var(--border);
    border-radius: 2px;
    padding: .6rem .8rem;
    height: 160px;
    overflow-y: auto;
    font-family: var(--mono);
    font-size: .7rem;
    color: #5a8a9a;
    line-height: 1.5;
  }
  #log-box .ln { display: block; }
  #log-box .ln .ts  { color: var(--muted); }
  #log-box .ln .msg { color: #7ab8cc; }
  #log-box .ln .val { color: var(--accent3); }

  footer {
    padding: .6rem 2rem;
    font-family: var(--mono); font-size: .65rem; color: var(--muted);
    border-top: 1px solid var(--border);
    display: flex; gap: 2rem;
  }
</style>
</head>
<body>

<header>
  <div>
    <div class="logo">e<span>BUS</span> Monitor</div>
    <div class="subtitle">Heat Pump Live Dashboard</div>
  </div>
  <div class="mode-badges">
    <div class="badge" id="badge-heating">
      <div class="badge-dot"></div>
      <span>Heating</span>
    </div>
    <div class="badge" id="badge-dhw">
      <div class="badge-dot"></div>
      <span>Hot Water</span>
    </div>
  </div>
  <div class="status-label" id="status-label">connecting…</div>
  <div class="status-dot offline" id="status-dot"></div>
</header>

<div class="kpi-strip" id="kpi-strip"></div>

<div class="charts-grid" id="charts-grid"></div>

<div class="log-section">
  <div class="log-header">▸ eBUS traffic log</div>
  <div id="log-box"></div>
</div>

<footer>
  <span id="last-update">last update: —</span>
  <span>poll interval: {{ fields | tojson | length }} fields / {{ 5 }}s</span>
</footer>

<script>
// ── field config (injected from server) ─────────────────────────────────────
const FIELDS = {{ fields | safe }};

// Chart colour palette
const PALETTE = [
  '#00d4ff','#ff6b35','#39ff14','#f7c59f',
  '#b388ff','#4dd0e1','#ffb300','#e91e63'
];

// ── State ────────────────────────────────────────────────────────────────────
const chartMap   = {};    // key → Chart.js instance
const kpiMap     = {};    // key → DOM element
const seriesData = {};    // key → [{ts,value}]

// ── Build KPI strip & chart grid ─────────────────────────────────────────────
function buildUI() {
  const strip = document.getElementById('kpi-strip');
  const grid  = document.getElementById('charts-grid');
  let ci = 0;

  for (const [key, [field, label, unit]] of Object.entries(FIELDS)) {
    seriesData[key] = [];

    // KPI tile
    const kpi = document.createElement('div');
    kpi.className = 'kpi';
    kpi.innerHTML = `<div class="kpi-label">${label}</div>
      <span class="kpi-value" id="kpi-${key}">—</span>
      <span class="kpi-unit">${unit}</span>`;
    strip.appendChild(kpi);
    kpiMap[key] = kpi.querySelector(`#kpi-${key}`);

    // Chart card
    const card = document.createElement('div');
    card.className = 'chart-card';
    card.innerHTML = `<div class="chart-title">${label}${unit ? ' ('+unit+')' : ''}</div>
      <canvas id="chart-${key}" height="120"></canvas>`;
    grid.appendChild(card);

    const ctx = card.querySelector(`#chart-${key}`).getContext('2d');
    const color = PALETTE[ci++ % PALETTE.length];

    // Full-day time bounds — recalculated at midnight if the page stays open
    const dayStart = new Date(); dayStart.setHours(0,0,0,0);
    const dayEnd   = new Date(); dayEnd.setHours(23,59,59,999);

    chartMap[key] = new Chart(ctx, {
      type: 'line',
      data: {
        datasets: [{
          data: [],          // {x: Date, y: value} objects
          borderColor: color,
          backgroundColor: color + '18',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
          spanGaps: false,   // break line where data is missing
        }]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: true,
        plugins: { legend: { display: false } },
        scales: {
          x: {
            type: 'time',
            min: dayStart,
            max: dayEnd,
            time: {
              unit: 'hour',
              displayFormats: { hour: 'HH:mm' },
              tooltipFormat: 'HH:mm:ss',
            },
            ticks: { color: '#4a6070', font: { family: "'Share Tech Mono'", size: 9 },
                     maxTicksLimit: 12, maxRotation: 0 },
            grid: { color: '#0f1e2e' }
          },
          y: {
            ticks: { color: '#4a6070', font: { family: "'Share Tech Mono'", size: 9 } },
            grid:  { color: '#0f1e2e' }
          }
        }
      }
    });
  }
}

// ── Mode indicators ───────────────────────────────────────────────────────────
function updateMode(mode) {
  const bH = document.getElementById('badge-heating');
  const bD = document.getElementById('badge-dhw');
  bH.className = 'badge' + (mode === 'heating' ? ' active-heating' : '');
  bD.className = 'badge' + (mode === 'dhw'     ? ' active-dhw'     : '');
}

// ── Retroactively patch an out-of-bounds point in the chart ──────────────────
function fixPoint(key, ts, value) {
  const chart = chartMap[key];
  if (!chart) return;
  const t = new Date(ts).getTime();
  const data = chart.data.datasets[0].data;
  for (let i = data.length - 1; i >= 0; i--) {
    if (data[i].x.getTime() === t) {
      data[i] = { x: data[i].x, y: value };
      chart.update('none');
      break;
    }
  }
}

// ── Push a data point into chart + KPI ──────────────────────────────────────
const GAP_MS = 3 * 60 * 1000;   // gap threshold: 3 minutes

function pushPoint(key, ts, value) {
  if (!(key in chartMap)) return;
  const chart = chartMap[key];
  const data  = chart.data.datasets[0].data;
  const t     = new Date(ts);

  // If the previous point is more than GAP_MS ago, bracket the gap with nulls
  if (data.length > 0) {
    const prevT = data[data.length - 1].x;
    if (prevT && (t - prevT) > GAP_MS) {
      data.push({ x: new Date(prevT.getTime() + 1), y: null });
      data.push({ x: new Date(t.getTime()    - 1), y: null });
    }
  }

  data.push({ x: t, y: value });
  chart.update('none');

  // update KPI
  const el = kpiMap[key];
  if (el) {
    el.textContent = Number.isInteger(value) ? value : value.toFixed(1);
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 600);
  }
}

// ── Log formatter ────────────────────────────────────────────────────────────
function appendLog(line) {
  const box = document.getElementById('log-box');
  const m = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\S*\s+(.+?received\s+)(.+)$/);
  const span = document.createElement('span');
  span.className = 'ln';
  if (m) {
    span.innerHTML =
      `<span class="ts">${m[1]}</span> ` +
      `<span class="msg">${m[2]}</span>` +
      `<span class="val">${m[3]}</span>`;
  } else {
    span.textContent = line;
  }
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
  // trim old lines
  while (box.children.length > 200) box.removeChild(box.firstChild);
}

// ── SSE connection ────────────────────────────────────────────────────────────
function connect() {
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  const es = new EventSource('/api/stream');

  es.onopen = () => {
    dot.classList.remove('offline');
    label.textContent = 'live';
  };

  es.onerror = () => {
    dot.classList.add('offline');
    label.textContent = 'reconnecting…';
  };

  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    const now = new Date().toISOString().replace('T',' ').substring(0,19);
    document.getElementById('last-update').textContent = 'last update: ' + now;

    if (msg.type === 'snapshot' || msg.type === 'update') {
      for (const [key, point] of Object.entries(msg.data || {})) {
        if (key === '_fixes') continue;
        if (point) pushPoint(key, point.ts, point.value);
      }
      for (const fix of (msg.data?._fixes || [])) {
        fixPoint(fix.key, fix.ts, fix.value);
      }
      if (msg.mode) updateMode(msg.mode);
    }
    if (msg.type === 'log') {
      appendLog(msg.line);
    }
  };
}

// ── Load history on start ─────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch('/api/history');
    const h = await r.json();

    for (const [key, points] of Object.entries(h)) {
      if (key === 'latest' || key === 'logs' || key === 'mode' || !Array.isArray(points)) continue;
      points.forEach(p => pushPoint(key, p.ts, p.value));
    }

    (h.logs || []).forEach(appendLog);
    if (h.mode) updateMode(h.mode);

    if (h.latest) {
      for (const [key, meta] of Object.entries(h.latest)) {
        const el = kpiMap[key];
        if (el && meta.value != null) {
          el.textContent = Number.isInteger(meta.value)
            ? meta.value : meta.value.toFixed(1);
        }
      }
    }
  } catch(e) { console.warn('history load failed', e); }
}

// ── Init ──────────────────────────────────────────────────────────────────────
buildUI();
loadHistory();
connect();
</script>
</body>
</html>
"""

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
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=tail_logs,  daemon=True).start()
    app.run(host="0.0.0.0", port=6789, debug=False, threaded=True)

"""
ebusd Live Dashboard - Flask Backend
Uses pyebus to communicate directly with ebusd over TCP.
"""
import atexit
import json
import queue
import re
import threading
from datetime import date, datetime

from flask import Flask, Response, jsonify, render_template, request

import config
import persistence
import polling
import state
from ebus_client import parse_value

app = Flask(__name__)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("interface.html",
                           fields=json.dumps(config.EBUSCTL_FIELDS),
                           chart_groups=json.dumps(config.CHART_GROUPS),
                           indicators=config.INDICATORS)


@app.route("/roomtemp", methods=["POST"])
def post_roomtemp():
    raw = request.form.get("current") or request.json and request.json.get("current")
    if raw is None:
        return jsonify({"error": "missing 'current' param"}), 400
    value = parse_value(str(raw).replace(',', '.'))
    if value is None:
        return jsonify({"error": "invalid value"}), 400

    lo, hi = config.BOUNDS.get("room_temp", (-99, 99))
    if not (lo <= value <= hi):
        return jsonify({"error": f"value {value} out of bounds [{lo}, {hi}]"}), 400

    ts = datetime.now().isoformat(timespec="seconds")
    point = {"ts": ts, "value": value}
    with state.data_lock:
        state.latest["room_temp"] = {"value": value, "unit": "°C",
                                     "label": "Room Temp", "raw": str(raw), "ts": ts}
        state._minute_bucket["room_temp"].append(value)

    print(f"[roomtemp] {ts}: {value} °C")
    polling.broadcast(json.dumps({"type": "update", "ts": ts,
                                  "data": {"room_temp": point},
                                  "indicators": polling.derive_indicators(state.latest)}))
    return jsonify({"ok": True, "ts": ts, "value": value})


@app.route("/api/dates")
def api_dates():
    if not config.DATA_DIR.exists():
        return jsonify([])
    days = sorted(
        p.stem for p in config.DATA_DIR.glob("*.jsonl")
        if re.match(r"\d{4}-\d{2}-\d{2}", p.stem)
    )
    return jsonify(days)


@app.route("/api/history")
def api_history():
    req_date = request.args.get("date")

    if req_date and req_date != date.today().isoformat():
        path = config.DATA_DIR / f"{req_date}.jsonl"
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
        all_keys = list(config.EBUSCTL_FIELDS.keys())
        out: dict = {k: [] for k in all_keys}
        for record in records:
            ts = record.get("ts", "")
            for key in all_keys:
                value = record.get(config.LOG_KEY_OVERRIDES.get(key, key))
                if value is not None:
                    out[key].append({"ts": ts, "value": value})
        out["latest"]     = {}
        out["logs"]       = []
        out["indicators"] = []
        return jsonify(out)

    with state.data_lock:
        out = {k: list(v) for k, v in state.series.items()}
        out["latest"]     = dict(state.latest)
        out["logs"]       = list(state.log_lines)
        out["indicators"] = polling.derive_indicators(state.latest)
    return jsonify(out)


@app.route("/api/stream")
def api_stream():
    q = queue.Queue(maxsize=50)
    state.sse_clients.append(q)

    def generate():
        with state.data_lock:
            snap = {k: {"ts": v["ts"], "value": v["value"]}
                    for k, v in state.latest.items() if "value" in v}
        yield f"data: {json.dumps({'type':'snapshot','data':snap})}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            try:
                state.sse_clients.remove(q)
            except ValueError:
                pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Startup ───────────────────────────────────────────────────────────────────

atexit.register(persistence.shutdown_flush)

if __name__ == "__main__":
    persistence.restore_today()
    threading.Thread(target=polling.start_async_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=config.SERVER_PORT, debug=False, threaded=True)

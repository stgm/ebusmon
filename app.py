"""
ebusd Live Dashboard - Flask Backend
Uses pyebus to communicate directly with ebusd over TCP.
"""
import argparse
import atexit
import json
import signal
import sys
import threading
from datetime import date, datetime

from flask import Flask, Response, jsonify, render_template, request

import aggregate
import config
import ebus_client
import indicators
import persistence
import polling
import sse
import state

app = Flask(__name__)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("interface.html",
                           fields=json.dumps(state.fields),
                           chart_groups=json.dumps(state.cfg.chart_groups),
                           indicators=state.cfg.indicators)


@app.route("/roomtemp", methods=["POST"])
def post_roomtemp():
    """
    Accept a room temperature from outside ebusd.

    Systems without a room thermostat have no RoomTemp on the bus, so this
    stands in for one: the reading joins the same pipeline as a polled field.
    """
    raw = request.form.get("current") or request.json and request.json.get("current")
    if raw is None:
        return jsonify({"error": "missing 'current' param"}), 400
    # Normalise comma decimal separator (e.g. "20,5" → "20.5")
    value = ebus_client.parse_value(str(raw).replace(',', '.'))
    if value is None:
        return jsonify({"error": "invalid value"}), 400

    lo, hi = state.cfg.bounds.get("room_temp", (-99, 99))
    if not (lo <= value <= hi):
        return jsonify({"error": f"value {value} out of bounds [{lo}, {hi}]"}), 400

    ts = datetime.now().isoformat(timespec="seconds")
    point = {"ts": ts, "value": value}
    with state.data_lock:
        state.latest["room_temp"] = {"value": value, "unit": "°C",
                                     "label": "Room Temp", "raw": str(raw), "ts": ts}
    aggregate.add_sample("room_temp", ts, value)

    print(f"[roomtemp] {ts}: {value} °C")
    sse.broadcast(json.dumps({"type": "update", "ts": ts,
                              "data": {"room_temp": point},
                              "indicators": indicators.derive(state.latest,
                                                              state.cfg.indicators)}))
    return jsonify({"ok": True, "ts": ts, "value": value})


@app.route("/api/dates")
def api_dates():
    """Available day strings (YYYY-MM-DD), for the history picker."""
    return jsonify(persistence.available_days())


@app.route("/api/history")
def api_history():
    req_date = request.args.get("date")  # optional ?date=YYYY-MM-DD

    if req_date and req_date != date.today().isoformat():
        # Serve a past day from its .jsonl file. Parsing the date rather than
        # interpolating it into a path also keeps the request inside data_dir.
        try:
            day = date.fromisoformat(req_date)
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400

        out: dict = persistence.day_series(day)
        out["latest"]     = {}
        out["indicators"] = []
        return jsonify(out)

    # Today: serve live in-memory series
    with state.data_lock:
        out = {k: list(v) for k, v in state.series.items()}
        out["latest"]     = dict(state.latest)
        out["indicators"] = indicators.derive(state.latest, state.cfg.indicators)
    return jsonify(out)


@app.route("/api/stream")
def api_stream():
    q = sse.register()

    def generate():
        # Send snapshot of most recent live values for KPI tiles
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
                    yield ": ping\n\n"   # keepalive
        finally:
            sse.unregister(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Startup ───────────────────────────────────────────────────────────────────

def _install_shutdown_handlers():
    """
    Make sure remaining data is summarized and logged when the server is
    killed.

    Install this when the main thread no longer holds state.data_lock
    the handler runs on the main thread and would deadlock on its own lock.
    """
    def handle(_signum, _frame):
        aggregate.shutdown_flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


def main():
    parser = argparse.ArgumentParser(description="ebusd Live Dashboard")
    parser.add_argument("--config", "-c", default="config.yaml", metavar="FILE",
                        help="path to config file (default: config.yaml)")
    args = parser.parse_args()

    state.init(config.load(args.config))
    aggregate.init()
    atexit.register(aggregate.shutdown_flush)

    persistence.restore_today()
    _install_shutdown_handlers()
    threading.Thread(target=polling.start_async_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=state.cfg.server_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

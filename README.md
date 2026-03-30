# eBUS Heat Pump Live Dashboard

A Flask dashboard that connects directly to ebusd via [pyebus](https://github.com/c0fec0de/pyebus) and streams live charts to the browser using Server-Sent Events (SSE).

## Quick start

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:6789
```

## Configuration (top of app.py)

| Variable | Default | Description |
|---|---|---|
| `EBUSD_HOST` | `127.0.0.1` | ebusd TCP host |
| `EBUSD_PORT` | `8888` | ebusd TCP port |
| `POLL_INTERVAL` | `15` | Seconds between poll cycles |
| `HISTORY_POINTS` | `1440` | Data points kept in memory per metric (1 per minute × 24h) |
| `DATA_DIR` | `data/` | Directory for daily `.jsonl` persistence files |

## Adding / removing fields

Edit the `EBUSCTL_FIELDS` dict:

```python
EBUSCTL_FIELDS = {
    "my_key": ("EbusdMessageName", "Display Label", "unit"),
    ...
}
```

Also add a bounds entry in `BOUNDS` to enable outlier correction:

```python
BOUNDS = {
    ...
    "my_key": (0.0, 100.0),   # lo, hi
}
```

Field names are matched against ebusd message names (case-insensitive). On startup the app logs a warning for any fields it cannot resolve.

## How it works

1. **pyebus** connects directly to ebusd over TCP — no Docker exec required.
2. **Background async loop** reads all configured fields every `POLL_INTERVAL` seconds using `async_read()` with a cached TTL of `POLL_INTERVAL * 2` seconds.
3. **Outlier correction** — readings outside their configured `BOUNDS` are replaced by linear interpolation of surrounding good values (up to 2 consecutive bad points).
4. **Persistence** — one averaged value per minute is written to `data/YYYY-MM-DD.jsonl`. On startup, today's file is restored into memory so charts show the full day from 00:00.
5. **Midnight rollover** — at midnight the in-memory series are cleared and the browser resets all charts to the new day automatically.
6. **`/api/stream`** is an SSE endpoint — the browser connects once and receives push events.
7. **`/api/history`** returns today's full series as JSON (used on page load).

## Mode indicators

The header shows two badges:

- **Heating** — compressor is running and `ThreeWayValve` is not in DHW mode
- **Hot Water** — compressor is running and `ThreeWayValve` reports "warm water circuit"

## Notes

- The dashboard auto-reconnects if the SSE connection drops.
- If a field is unavailable or returns an error it is silently skipped for that cycle.
- Previous days' `.jsonl` files are kept indefinitely in `DATA_DIR` and are never modified.

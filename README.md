# eBUS Heat Pump Live Dashboard

A Flask dashboard that polls your ebusd Docker container via `ebusctl` and streams live charts to the browser using Server-Sent Events (SSE).

## Quick start

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:5000
```

## Configuration (top of app.py)

| Variable | Default | Description |
|---|---|---|
| `DOCKER_CONTAINER` | `peaceful_heyrovsky` | Name or ID of your ebusd container |
| `POLL_INTERVAL` | `5` | Seconds between ebusctl reads |
| `HISTORY_POINTS` | `120` | Data points kept in memory per metric |

## Adding / removing fields

Edit the `EBUSCTL_FIELDS` dict:

```python
EBUSCTL_FIELDS = {
    "my_key": ("EbusctlFieldName", "Display Label", "unit"),
    ...
}
```

The key must be a valid `ebusctl read -f <field>` field name.  
Run this to discover available fields in your container:

```bash
docker exec peaceful_heyrovsky ebusctl find -VV
```

## How it works

1. **Background thread** calls `docker exec <container> ebusctl read -f <field>` for each configured field every `POLL_INTERVAL` seconds and stores the value in a ring buffer.
2. **Log tail thread** runs `docker logs -f` and forwards every `[update notice]` line to connected clients.
3. **`/api/stream`** is an SSE endpoint — the browser connects once and receives push events.
4. **`/api/history`** returns the current ring buffer as JSON (used on page load).

## Notes

- The `ebusctl` field names in `EBUSCTL_FIELDS` are guesses based on common ebusd CSV definitions. Run `ebusctl find` or check your ebusd CSV files to get exact names for your heat pump model.
- If a field returns an error or is unavailable, it is silently skipped.
- The dashboard auto-reconnects if the SSE connection drops.

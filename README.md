# eBUS Heat Pump Live Dashboard

A live monitoring dashboard for eBUS heat pumps. It connects to a running
[ebusd](https://github.com/john30/ebusd) instance, polls your heat pump's sensors
every few seconds, and streams the data to a browser dashboard in real time.
You can configure the charts that you would like to see in `config.yaml`.

---

## Prerequisites

- **uv** — a fast Python package manager ([install instructions](https://docs.astral.sh/uv/getting-started/installation/))
- **ebusd or micro-ebusd** already running and reachable on your network,
  with your heat pump's message definitions loaded

> **What is ebusd?**
> [ebusd](https://github.com/john30/ebusd) is a daemon that talks to your
> heat pump over the eBUS serial interface and exposes its data over TCP.
> [micro-ebusd](https://token.ebusd.eu) is an ebusd implementation that's
> fully embedded on a specialized ebus adapter.

---

## Getting started

### 1. Get the code

```bash
git clone https://github.com/stgm/ebusmon.git
cd ebusmon
```

### 2. Edit the config file

Open `config.yaml` and set the address of your ebusd:

```yaml
ebusd:
  host: 127.0.0.1   # ← your ebusd IP address
  port: 8888            # default ebusd TCP port, usually unchanged
```

That's the minimum you need to change to get started. The rest of the config
controls which fields are displayed, and some nice defaults are included.

### 3. Run the dashboard

```bash
uv run app.py
```

`uv` will make sure that dependencies are installed into the `.venv` directory.
Then the server starts running.
Now open your browser at **http://localhost:6789** (or replace `localhost` with the
machine's IP if you're running it on a different server).

You can point to a different config file with `--config`:

```bash
uv run app.py --config /path/to/my-config.yaml
# or short form:
uv run app.py -c /path/to/my-config.yaml
```

---

## Configuring charts

All chart configuration lives in `config.yaml`. You do not need to touch `app.py`.

### How charts are defined

Each entry under `charts:` becomes one chart panel and one KPI tile in the header.
You specify field names exactly as ebusd knows them (CamelCase message names).
The dashboard derives the display label and internal key automatically.

```yaml
charts:
  - FlowTemp:
      bounds: [-5, 90]
```

This creates a chart for the `FlowTemp` message, auto-labelled **"Flow temp"**,
with outlier correction applied for values outside −5 … 90.

### Pairing two fields on one chart

Put two field names in the same entry — the first is the primary (left axis),
the second is the secondary (shown dimmer, used for comparison):

```yaml
  - HwcTemp:
      bounds: [5, 80]
    TargetTempHwc:
      bounds: [10, 80]
```

This shows DHW temperature and its target setpoint on the same chart.

### Overriding the auto-derived label

By default, `FlowTemp` becomes "Flow temp", `RunDataReturnTemp` becomes
"Run data return temp", and so on. If you want something shorter or clearer:

```yaml
  - RunDataReturnTemp:
      label: Return temp
      bounds: [-5, 80]
```

### Bounds and outlier correction

`bounds: [lo, hi]` tells the dashboard the physically realistic range for
that field. Any reading outside this range is assumed to be a glitch and is
silently replaced by interpolation between the surrounding good values
(up to two consecutive bad readings are corrected).

Omit `bounds` entirely if you don't want outlier correction for a field.

### Non-ebus fields

You can include fields that don't come from ebusd — for example a manually
submitted room temperature. These are listed the same way in `config.yaml`;
the dashboard simply won't poll ebusd for them. Use the `RoomTemp` convention
(CamelCase), which the app converts to the `room_temp` internal key.

Room temperature can be submitted via HTTP POST:

```bash
curl -X POST http://localhost:6789/roomtemp \
     -d "current=20.5"
```

### Field names

The field names you use in `config.yaml` must match what ebusd calls them.

---

## Indicators

The indicators in the page header light up when configurable conditions are
met. Each indicator has a name and a set of conditions that must all be true
simultaneously.

```yaml
indicators:
  - Heat:
      ThreeWayValve: heat           # field value contains "heat" (case-insensitive)
      RunDataCompressorSpeed: "on"  # numeric field value is > 0
```

Two condition types:
- **String match** — the field's string value contains the given text (case-insensitive)
- **`"on"`** — the field's numeric value is greater than zero

---

## How it works

1. On startup, we load all message definitions from ebusd and resolve
   your configured field names. Fields not found in ebusd are skipped with a
   warning in the console.
2. A background loop polls all configured fields every 15 seconds, using
   ebusd's value cache to avoid hammering the bus.
3. Readings are averaged per minute and written to `data/YYYY-MM-DD.jsonl`
   for persistence. Today's file is restored into memory on startup so the
   full day is visible immediately.
4. The browser receives live updates via Server-Sent Events (SSE) — a
   persistent HTTP connection where the server pushes data as it arrives.
   The page auto-reconnects if the connection drops.
5. At midnight the in-memory data rolls over automatically and the browser
   resets all charts for the new day.

Historical days are available via the date picker in the top-right corner.

---

## Troubleshooting

**No data appears / fields show "—"**
Check the console output when starting the app. Lines like:
```
[pyebus] WARNING: fields not found in msgdefs: {'flowtemp', ...}
```
mean ebusd doesn't know that field name.

**"Failed to connect" on startup**
The app can't reach ebusd. Check that the `host` and `port` in `config.yaml`
are correct and that ebusd is running. You can test with:
```bash
nc -zv 192.168.1.100 8888
```

**Outlier spikes in charts**
Tighten the `bounds` for that field in `config.yaml`. The current bounds may
be too wide to catch the glitches your heat pump produces.

**Values look stale**
ebusd caches values on the bus. The dashboard accepts cached values up to
`2 × poll interval` (30 seconds by default). This is normal — it reduces
bus traffic.

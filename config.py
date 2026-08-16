"""
config.yaml → Config.

Importing this module has no side effects: `load()` is called once, from
app.main(). Nothing here changes at runtime — values that do, such as the units
reported by ebusd and the readings themselves, live in `state`.
"""
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


def camel_to_snake(name: str) -> str:
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    return s.lower()


def camel_to_label(name: str) -> str:
    if name == name.lower():          # already snake_case (e.g. room_temp)
        words = name.split('_')
        return ' '.join([words[0].capitalize()] + words[1:]) if words else name
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1 \2', s)
    words = s.split()
    return ' '.join([words[0].capitalize()] + [w.lower() for w in words[1:]]) if words else name


@dataclass(frozen=True)
class Config:
    ebusd_host:  str
    ebusd_port:  int
    server_port: int

    # key → (ebus field name, display label)
    fields:            dict[str, tuple[str, str]]
    # charts, as lists of keys; the first of each list is the primary series
    chart_groups:      list[list[str]]
    bounds:            dict[str, tuple[float, float]]
    # key → the name this field is written under in the .jsonl files
    log_key_overrides: dict[str, str]
    # {"label": str, "conditions": {key: condition}} — keys already resolved
    indicators:        list[dict]
    # key → ebus field name, for indicator fields that are not charted
    extra_fields:      dict[str, str]

    data_dir:       Path = Path("data")
    poll_interval:  int  = 15
    history_points: int  = 1440       # one per minute × 24 h

    @property
    def read_ttl(self) -> int:
        """TTL passed to async_read: accept a cached value up to this old."""
        return self.poll_interval * 2


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _parse_charts(entries) -> tuple[list, dict, dict, dict]:
    """
    Each entry is one chart; entries sharing a dash share a chart.
    Format: "Display name: FieldName[, log_key][, min, max]".
    """
    field_groups:      list[list[str]]               = []
    bounds:            dict[str, tuple[float, float]] = {}
    labels:            dict[str, str]                = {}
    log_key_overrides: dict[str, str]                = {}

    for chart_entry in entries:
        group_names = []
        for label, value in chart_entry.items():
            parts      = [p.strip() for p in str(value).split(",")]
            field_name = parts[0]
            key        = camel_to_snake(field_name)
            labels[key] = label
            group_names.append(field_name)

            rest = parts[1:]
            if rest and not _is_number(rest[0]):
                log_key_overrides[key] = rest[0]
                rest = rest[1:]
            if len(rest) == 2:
                bounds[key] = (float(rest[0]), float(rest[1]))
        field_groups.append(group_names)

    return field_groups, bounds, labels, log_key_overrides


def load(path: str = "config.yaml") -> Config:
    with open(path) as f:
        cfg = yaml.safe_load(f)

    ebusd  = cfg.get("ebusd", {})
    server = cfg.get("server", {})

    field_groups, bounds, labels, log_key_overrides = _parse_charts(cfg.get("charts", []))

    # Flatten the groups into a deduplicated key → (name, label) mapping.
    fields: dict[str, tuple[str, str]] = {}
    for group in field_groups:
        for name in group:
            key = camel_to_snake(name)
            if key not in fields:
                fields[key] = (name, labels.get(key) or camel_to_label(name))

    chart_groups = [[camel_to_snake(n) for n in group] for group in field_groups]

    # Conditions are resolved to keys here so the poll loop does not redo it on
    # every cycle. extra_fields keeps the ebus name, which is what ebusd is asked for.
    indicators:   list[dict]     = []
    extra_fields: dict[str, str] = {}
    for entry in cfg.get("indicators", []):
        for label, conditions in entry.items():
            resolved = {}
            for name, condition in conditions.items():
                key = camel_to_snake(name)
                resolved[key] = condition
                if key not in fields and key not in extra_fields:
                    extra_fields[key] = name
            indicators.append({"label": label, "conditions": resolved})

    return Config(
        ebusd_host        = ebusd.get("host", "127.0.0.1"),
        ebusd_port        = int(ebusd.get("port", 8888)),
        server_port       = int(server.get("port", 6789)),
        fields            = fields,
        chart_groups      = chart_groups,
        bounds            = bounds,
        log_key_overrides = log_key_overrides,
        indicators        = indicators,
        extra_fields      = extra_fields,
    )

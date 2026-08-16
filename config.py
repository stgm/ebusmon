import argparse
import re
import yaml
from pathlib import Path

POLL_INTERVAL  = 15
HISTORY_POINTS = 1440
DATA_DIR       = Path("data")
READ_TTL       = POLL_INTERVAL * 2


def _camel_to_snake(name: str) -> str:
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    return s.lower()


def _camel_to_label(name: str) -> str:
    if name == name.lower():
        words = name.split('_')
        return ' '.join([words[0].capitalize()] + words[1:]) if words else name
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1 \2', s)
    words = s.split()
    return ' '.join([words[0].capitalize()] + [w.lower() for w in words[1:]]) if words else name


def _load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)

    ebusd = cfg.get("ebusd", {})
    host  = ebusd.get("host", "127.0.0.1")
    port  = int(ebusd.get("port", 8888))

    field_groups: list[list[str]]             = []
    bounds:       dict[str, tuple[float, float]] = {}
    label_overrides:   dict[str, str]         = {}
    log_key_overrides: dict[str, str]         = {}

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

    indicators: list[dict] = []
    for entry in cfg.get("indicators", []):
        for label, conditions in entry.items():
            indicators.append({"label": label, "conditions": dict(conditions)})

    server = cfg.get("server", {})

    return {
        "host":              host,
        "port":              port,
        "server_port":       int(server.get("port", 6789)),
        "field_groups":      field_groups,
        "bounds":            bounds,
        "label_overrides":   label_overrides,
        "log_key_overrides": log_key_overrides,
        "indicators":        indicators,
    }


_parser = argparse.ArgumentParser(description="ebusd Live Dashboard")
_parser.add_argument("--config", "-c", default="config.yaml", metavar="FILE",
                     help="path to config file (default: config.yaml)")
_args, _ = _parser.parse_known_args()

_cfg = _load_config(_args.config)

EBUSD_HOST  = _cfg["host"]
EBUSD_PORT  = _cfg["port"]
SERVER_PORT = _cfg["server_port"]

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

CHART_GROUPS: list[list[str]] = [
    [_camel_to_snake(n) for n in group]
    for group in _cfg["field_groups"]
]

BOUNDS:            dict[str, tuple[float, float]] = _cfg["bounds"]
LOG_KEY_OVERRIDES: dict[str, str]                 = _cfg["log_key_overrides"]
INDICATORS:        list[dict]                      = _cfg["indicators"]

EXTRA_FIELDS: dict[str, str] = {}
for _ind in INDICATORS:
    for _fname in _ind["conditions"]:
        _key = _camel_to_snake(_fname)
        if _key not in EBUSCTL_FIELDS and _key not in EXTRA_FIELDS:
            EXTRA_FIELDS[_key] = _fname
del _ind, _fname, _key

"""
Header indicators: which configured conditions currently hold.

An indicator's conditions are ANDed. A condition is either a substring match
against the field's value (case-insensitive), or "on", meaning the field is
numeric and greater than zero.
"""


def derive(latest: dict, indicators: list[dict]) -> list[str]:
    """
    Return the labels of the indicators whose conditions are all met.

    `latest` maps key → {"value": ...}; `indicators` is Config.indicators, whose
    condition keys are already resolved to the same keys `latest` uses.
    """
    active = []
    for indicator in indicators:
        if all(_holds(latest.get(key, {}).get("value"), condition)
               for key, condition in indicator["conditions"].items()):
            active.append(indicator["label"])
    return active


def _holds(value, condition) -> bool:
    if value is None:
        return False
    if condition == "on" or condition is True:  # YAML parses bare 'on' as True
        return isinstance(value, (int, float)) and value > 0
    return str(condition).lower() in str(value).lower()

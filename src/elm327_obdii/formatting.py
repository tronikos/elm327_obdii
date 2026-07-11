"""Value formatting for the HA sensor platform.

The sensor platform calls :func:`format_sensor_value` on the raw
resolver return value before storing it as the entity's
``native_value``.
"""

from typing import Any


def format_sensor_value(value: Any) -> str | int | float | None:
    """Format a standard-PID resolver value for HA state display.

    Lists are joined into comma-separated strings; everything else is
    returned as-is. Fuel-system status (list-of-tuples) is collapsed
    to the first element of each tuple.
    """
    if value is None:
        return None
    if isinstance(value, list | tuple):
        if all(isinstance(x, tuple) and len(x) > 0 for x in value):
            return ", ".join(str(x[0]) for x in value)
        return ", ".join(str(item) for item in value)
    if isinstance(value, (int, float, str)):
        return value
    return str(value)

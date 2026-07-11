"""Canonical Mode 01 PID catalog, entity heuristics, and supported-PID scan.

``RECOMMENDED_DEFAULTS`` is validated against the live obdii registry
at import time so a typo fails loudly instead of silently at runtime.
"""

from typing import Any, Final

from obdii import Command, Connection, commands

RECOMMENDED_DEFAULTS: Final[list[str]] = [
    "FUEL_LEVEL",
]

for _name in RECOMMENDED_DEFAULTS:
    if _name not in commands[1]:
        raise RuntimeError(
            f"RECOMMENDED_DEFAULTS references {_name!r} which is not a real "
            f"obdii Mode 01 command - check _core/standard_pids.py against "
            f"the pinned py-obdii version"
        )
del _name


# The bitmap chain: PID 0x00 covers PIDs 0x01..0x20, PID 0x20 covers
# 0x21..0x40, etc. The next block must be requested only if the MSB
# of the current block's bitmap is set (bit 31 of the 4-byte response
# = next-block request indicator).
_BITMAP_PID_INTS: Final[list[int]] = [0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0]
_BITMAP_COMMAND_NAMES: Final[list[str]] = [
    "SUPPORTED_PIDS_A",
    "SUPPORTED_PIDS_B",
    "SUPPORTED_PIDS_C",
    "SUPPORTED_PIDS_D",
    "SUPPORTED_PIDS_E",
    "SUPPORTED_PIDS_F",
    "SUPPORTED_PIDS_G",
]


def is_supported_pids_bitmap(name: str) -> bool:
    """True if ``name`` is one of the SUPPORTED_PIDS_A..G bitmap commands.

    These are metadata - not user-trackable parameters - so they're
    filtered out of every list that feeds into the UI or the query plan.
    """
    return name.startswith("SUPPORTED_PIDS")


def get_standard_command(name: str) -> Command[Any] | None:
    """Look up a standard Mode 01 command by canonical name.

    Returns None if the name is unknown, so callers can skip unknown
    entries instead of raising. Useful when a user has stale entries
    in their config after upgrading the obdii library.
    """
    try:
        return commands[1][name]
    except KeyError:
        return None


def propose_icon(command: Command[Any]) -> str | None:
    """Heuristic mdi:icon suggestion for a standard PID.

    Returns the mdi: string (e.g. "mdi:thermometer") or None if no
    good guess. The sensor platform can still fall back to a default
    icon when this returns None.
    """
    name = (command.name or "").upper()
    units = _first_unit(command)

    if "TEMP" in name or units in ("c", "f", "k"):
        return "mdi:thermometer"
    if "VOLT" in name or units == "v":
        return "mdi:flash"
    if "SPEED" in name:
        return "mdi:speedometer"
    if "RPM" in name or name == "ENGINE_SPEED":
        return "mdi:engine"
    if "FUEL" in name or "LEVEL" in name:
        return "mdi:gas-station"
    if "LOAD" in name:
        return "mdi:gauge"
    if "PRESSURE" in name or units in ("kpa", "bar", "psi"):
        return "mdi:gauge"
    if "TIME" in name or units == "s":
        return "mdi:timer"
    if "DISTANCE" in name or units == "km":
        return "mdi:map-marker-distance"
    if "ANGLE" in name or units == "degree":
        return "mdi:angle-acute"
    return None


def propose_device_class(command: Command[Any]) -> str | None:
    """Heuristic SensorDeviceClass suggestion for a standard PID.

    Returns the string name (e.g. "temperature") so this module has no
    HA dependency. The sensor platform maps the string to the
    SensorDeviceClass enum.
    """
    name = (command.name or "").upper()
    units = _first_unit(command)

    if "TEMP" in name or units == "c":
        return "temperature"
    if "VOLT" in name or units == "v":
        return "voltage"
    if "PRESSURE" in name or units in ("kpa", "bar"):
        return "pressure"
    if "SPEED" in name or units in ("km/h", "mph"):
        return "speed"
    if "FUEL" in name and "LEVEL" in name:
        return "battery"
    if "LEVEL" in name and units == "%":
        return "battery"
    if units == "l":
        return "volume"
    if units == "s":
        return "duration"
    if "DISTANCE" in name or units == "km":
        return "distance"
    return None


def propose_state_class(command: Command[Any]) -> str | None:
    """Heuristic StateClass suggestion - 'measurement' or 'total_increasing'.

    Odometer, runtime, and absolute-distance counters are monotonic
    (total_increasing). Non-numeric commands (DTCs, status, ID) return
    None so the recorder doesn't try to store a state class. Everything
    else is instantaneous (measurement).
    """
    name = (command.name or "").upper()
    if (
        "DTC" in name
        or "STATUS" in name
        or "_ID" in name
        or "TYPE" in name
        or "STANDARDS" in name
    ):
        return None
    if "ODOMETER" in name or "DISTANCE" in name or "RUN_TIME" in name:
        return "total_increasing"
    return "measurement"


def get_list_of_units(command: Command[Any]) -> list[str]:
    """Return the unit options for a command's native_unit_of_measurement.

    obdii Command.units can be a string, a list of strings (for PIDs
    that return multiple values), or None. This normalizes to a list.
    """
    if not command.units:
        return []
    if isinstance(command.units, str):
        return [command.units]
    return list(command.units)


def scan_supported_pids(connection: Connection) -> list[str]:
    """Walk Mode 01 PID 00/20/40/.../C0 bitmaps and return supported command names.

    ``connection`` is an obdii.Connection. The caller is responsible
    for opening and closing it.

    Returns a list of canonical command names (e.g. ["ENGINE_SPEED",
    "VEHICLE_SPEED", ...]) sorted by PID number. If the scan fails at
    any point - adapter returns no data, the ECU is offline, the user
    turned the car off mid-scan - returns an empty list, and the
    caller falls back to RECOMMENDED_DEFAULTS (with a UI warning).
    """
    supported: list[str] = []

    for pid_int, name in zip(_BITMAP_PID_INTS, _BITMAP_COMMAND_NAMES, strict=True):
        try:
            cmd = commands[1][name]
        except KeyError:
            break

        resp = connection.query(cmd)
        if resp is None or resp.value is None:
            break

        supported_pid_ints = resp.value
        if not supported_pid_ints:
            break

        for pid_int_supported in supported_pid_ints:
            try:
                cmd_obj = commands[1][pid_int_supported]
            except KeyError:
                continue
            if cmd_obj and cmd_obj.name != "Unnamed":
                if not is_supported_pids_bitmap(cmd_obj.name):
                    supported.append(cmd_obj.name)

        next_block = pid_int + 0x20
        if next_block not in supported_pid_ints:
            break

    seen: set[str] = set()
    deduped: list[str] = []
    for name in supported:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    return sorted(deduped)


def _first_unit(command: Command[Any]) -> str:
    """Return the first unit string from a command, lowercased."""
    units = command.units
    if not units:
        return ""
    if isinstance(units, str):
        return units.lower()
    if isinstance(units, (list, tuple)) and units:
        return str(units[0]).lower()
    return ""

"""OBDb vehicle profile importer.

Translates OBDb signal definitions (from ``matrix_data.json`` and
per-vehicle repo ``default.json``) into our internal
:class:`ProfileConfig`.

OBDb uses a structured ``fmt`` dict for formulas — this maps directly
to our internal representation with no translation loss. The importer
handles:

  - Unit translation (OBDb long names → HA abbreviations)
  - ``suggestedMetric`` → ``device_class`` mapping
  - Year filtering via ``modelYears`` (matrix) or ``dbgfilter`` (repo)
  - Receive address (``rax``) from the per-vehicle repo, falling back
    to ``None`` when only the matrix is available
  - Flow control (``fcm1``) and extra init (``fcm1`` → ``ATFCSM1``)
  - Enumeration signals (``fmt.map``) → string-valued sensors
"""

import logging
from typing import Any

from .._core.elm327_parsing import as_float
from .._core.schema import CustomPid, ProfileConfig

_LOGGER = logging.getLogger(__name__)

# OBDb unit name → HA unit abbreviation.
OBDB_UNIT_MAP: dict[str, str] = {
    "ampereHours": "Ah",
    "amps": "A",
    "bars": "bar",
    "celsius": "°C",
    "centimeters": "cm",
    "degrees": "°",
    "fahrenheit": "°F",
    "gallons": "gal",
    "gramsPerLiter": "g/L",
    "gramsPerSecond": "g/s",
    "gravity": "g",
    "hertz": "Hz",
    "hours": "h",
    "kilogramsPerHour": "kg/h",
    "kilometers": "km",
    "kilometersPerHour": "km/h",
    "kiloohms": "kΩ",
    "kilopascal": "kPa",
    "kilowattHours": "kWh",
    "kilowattHoursPer100Miles": "kWh/100mi",
    "kilowatts": "kW",
    "liters": "L",
    "litersPerHour": "L/h",
    "meters": "m",
    "metersPerSecond": "m/s",
    "metersPerSecondSquared": "m/s²",
    "miles": "mi",
    "milesPerHour": "mph",
    "milliamps": "mA",
    "milliseconds": "ms",
    "minutes": "min",
    "newtonmeters": "N·m",
    "ohms": "Ω",
    "percent": "%",
    "powerSteeringAngle": "°",
    "psi": "psi",
    "revolutionsPerMinute": "rpm",
    "seconds": "s",
    "volts": "V",
    "watts": "W",
    "kilowattsPerHour": "kW/h",
}

# OBDb suggestedMetric → our device_class string.
OBDB_SUGGESTED_METRIC_MAP: dict[str, str] = {
    "stateOfCharge": "battery",
    "odometer": "distance",
    "electricRange": "distance",
    "vehicleSpeed": "speed",
    "fuelLevel": "battery",
    "batteryVoltage": "voltage",
    "batteryCurrent": "current",
    "batteryTemperature": "temperature",
    "ambientTemperature": "temperature",
    "engineCoolantTemperature": "temperature",
    "engineSpeed": "speed",
    "power": "power",
    "energy": "energy",
}


def import_obdb_profile(
    matrix_signals: list[dict[str, Any]],
    repo_default: dict[str, Any] | None = None,
    selected_year: int | None = None,
) -> ProfileConfig:
    """Translate OBDb signals into a :class:`ProfileConfig`.

    Args:
        matrix_signals: List of signal dicts from ``matrix_data.json``
            for a single ``(make, model)``.
        repo_default: Optional dict from the per-vehicle repo's
            ``signalsets/v3/default.json``. Provides ``rax``,
            ``fcm1``, and ``dbgfilter``. If ``None``, the import
            omits ATCRA (receive filter).
        selected_year: If not ``None``, filter signals by model year
            using ``modelYears`` (matrix) or ``dbgfilter`` (repo).

    Returns:
        A :class:`ProfileConfig` with standard_pids (always empty —
        OBDb signals are custom by definition) and custom_pids.
    """
    make = ""
    model = ""
    if matrix_signals:
        make = matrix_signals[0].get("make", "")
        model = matrix_signals[0].get("model", "")
    source = f"import:obdb:{make}:{model}"

    # Build a lookup from repo commands for rax/fcm1/dbgfilter.
    repo_cmd_map: dict[str, dict[str, Any]] = {}
    if repo_default and isinstance(repo_default.get("commands"), list):
        for cmd in repo_default["commands"]:
            if not isinstance(cmd, dict):
                continue
            key = _repo_cmd_key(cmd)
            if key:
                repo_cmd_map[key] = cmd

    custom: list[CustomPid] = []
    for signal in matrix_signals:
        try:
            # Year filter (matrix modelYears).
            if selected_year is not None and not _year_matches_matrix(
                signal, selected_year
            ):
                continue

            fmt = _extract_fmt(signal)
            if fmt is None:
                continue

            mode, query = _extract_mode_query(signal)
            if not mode:
                continue

            # Look up repo-level rax/fcm1/dbgfilter.
            repo_cmd = repo_cmd_map.get(f"{mode}{query}", {})
            if repo_cmd:
                # Year filter (repo dbgfilter).
                if selected_year is not None and not _year_matches_repo(
                    repo_cmd, selected_year
                ):
                    continue

            can_header = signal.get("hdr")
            can_filter = repo_cmd.get("rax") if repo_cmd else None
            init_extra = _build_init_extra(signal, repo_cmd)

            unit = _translate_unit(signal.get("unit") or fmt.get("unit"))
            device_class = _translate_device_class(signal, fmt)
            state_class = _translate_state_class(signal, fmt)

            pid_id = (
                signal.get("id") or f"{mode}:{query}:{signal.get('name', 'unnamed')}"
            )
            custom.append(
                CustomPid(
                    id=pid_id,
                    name=signal.get("name") or pid_id,
                    mode=mode,
                    query=query,
                    fmt=fmt,
                    can_header=can_header,
                    can_filter=can_filter,
                    init_extra=init_extra,
                    unit=unit,
                    device_class=device_class,
                    state_class=state_class,
                    min_value=as_float(fmt.get("min")),
                    max_value=as_float(fmt.get("max")),
                    model_years=_extract_model_years(signal, repo_cmd),
                    source=source,
                )
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Skipping malformed OBDb signal %r in %s %s: %s",
                signal.get("id", "?"),
                make,
                model,
                err,
            )

    return ProfileConfig(standard_pids=[], custom_pids=custom)


class ObdbImporter:
    """Protocol-conforming importer for runtime dispatch."""

    def can_handle(self, raw: object) -> bool:
        """Return True if ``raw`` looks like OBDb matrix signals."""
        return (
            isinstance(raw, list)
            and bool(raw)
            and "fmt" in (raw[0] if isinstance(raw[0], dict) else {})
        )

    def import_profile(self, raw: object) -> ProfileConfig:
        """Translate OBDb signals into a :class:`ProfileConfig`."""
        if not isinstance(raw, list):
            raise TypeError(f"OBDb signals must be a list, got {type(raw).__name__}")
        return import_obdb_profile(raw)


def _repo_cmd_key(cmd: dict[str, Any]) -> str | None:
    """Build a lookup key from a repo command's ``cmd`` field."""
    cmd_field = cmd.get("cmd")
    if not isinstance(cmd_field, dict):
        return None
    parts = []
    for mode, pid in sorted(cmd_field.items()):
        parts.append(f"{mode}{pid}")
    return parts[0] if parts else None


def _extract_fmt(signal: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and normalize the fmt dict from an OBDb signal."""
    fmt = signal.get("fmt")
    if not isinstance(fmt, dict):
        return None
    result: dict[str, Any] = {}
    bix = fmt.get("bix", signal.get("bitOffset", 0))
    length = fmt.get("len", signal.get("bitLength", 0))
    if not isinstance(bix, int) or not isinstance(length, int):
        return None
    if length < 1 or length > 64:
        return None
    result["bix"] = bix
    result["len"] = length
    for key in ("mul", "div", "add", "min", "max", "nullmin", "nullmax"):
        if key in fmt and fmt[key] is not None:
            result[key] = fmt[key]
    for key in ("sign", "blsb"):
        if fmt.get(key):
            result[key] = True
    if isinstance(fmt.get("map"), dict):
        result["map"] = {
            str(k): v.get("value", str(k)) if isinstance(v, dict) else str(v)
            for k, v in fmt["map"].items()
        }
    return result


def _extract_mode_query(signal: dict[str, Any]) -> tuple[str, str]:
    """Extract mode and query from an OBDb signal's ``cmd`` field."""
    cmd = signal.get("cmd")
    if not isinstance(cmd, dict) or not cmd:
        return ("", "")
    # cmd is like {"22": "1E3B"} — one key-value pair.
    for mode, pid in cmd.items():
        return (str(mode).upper(), str(pid).upper())
    return ("", "")


def _translate_unit(obdb_unit: str | None) -> str | None:
    """Translate an OBDb unit name to an HA abbreviation."""
    if not obdb_unit or obdb_unit in ("none", "None", ""):
        return None
    return OBDB_UNIT_MAP.get(obdb_unit, obdb_unit)


def _translate_device_class(signal: dict[str, Any], fmt: dict[str, Any]) -> str | None:
    """Map OBDb suggestedMetric to our device_class string."""
    if "map" in fmt:
        return None  # enumerations have no device_class
    suggested = signal.get("suggestedMetric", "")
    if suggested:
        mapped = OBDB_SUGGESTED_METRIC_MAP.get(suggested)
        if mapped:
            return mapped
    # Fall back to unit-based heuristic.
    unit = signal.get("unit", "")
    if unit == "volts":
        return "voltage"
    if unit in ("amps", "milliamps"):
        return "current"
    if unit in ("celsius", "fahrenheit"):
        return "temperature"
    if unit in ("kilometers", "miles"):
        return "distance"
    if unit in ("kilometersPerHour", "milesPerHour"):
        return "speed"
    if unit == "percent":
        return "battery"
    if unit == "kilowattHours":
        return "energy"
    if unit in ("kilowatts", "watts"):
        return "power"
    return None


def _translate_state_class(signal: dict[str, Any], fmt: dict[str, Any]) -> str | None:
    """Map OBDb signal to our state_class string."""
    if "map" in fmt:
        return None  # enumerations have no state_class
    suggested = signal.get("suggestedMetric", "")
    if suggested == "odometer":
        return "total_increasing"
    path = signal.get("path", "")
    if (
        "Cumulative" in path
        or "cumulative" in path
        or "Total" in path
        or "total" in path
    ):
        return "total_increasing"
    return "measurement"


def _build_init_extra(signal: dict[str, Any], repo_cmd: dict[str, Any]) -> str | None:
    """Build init_extra from repo-level fcm1 and other flags."""
    extras: list[str] = []
    if repo_cmd.get("fcm1"):
        extras.append("ATFCSM1")
    # Could add more repo-level init here in the future.
    return ";".join(extras) if extras else None


def _year_matches_matrix(signal: dict[str, Any], year: int) -> bool:
    """Check if a year falls within the signal's modelYears range."""
    model_years = signal.get("modelYears")
    if not model_years or not isinstance(model_years, list):
        return True  # no year filter → matches all
    if len(model_years) == 1:
        return year == int(model_years[0])
    if len(model_years) >= 2:
        return int(model_years[0]) <= year <= int(model_years[-1])
    return True


def _year_matches_repo(repo_cmd: dict[str, Any], year: int) -> bool:
    """Check if a year falls within the repo command's dbgfilter."""
    dbgfilter = repo_cmd.get("dbgfilter")
    if not dbgfilter or not isinstance(dbgfilter, dict):
        return True
    years_list = dbgfilter.get("years")
    if years_list and isinstance(years_list, list) and year in years_list:
        return True
    from_year = dbgfilter.get("from")
    to_year = dbgfilter.get("to")
    if from_year is not None and year < from_year:
        return False
    if to_year is not None and year > to_year:
        return False
    return True


def _extract_model_years(
    signal: dict[str, Any], repo_cmd: dict[str, Any]
) -> list[int] | None:
    """Extract model_years from matrix signal or repo dbgfilter."""
    model_years = signal.get("modelYears")
    if model_years and isinstance(model_years, list):
        return [int(y) for y in model_years if isinstance(y, int)]
    dbgfilter = repo_cmd.get("dbgfilter") if repo_cmd else None
    if dbgfilter and isinstance(dbgfilter, dict):
        years = dbgfilter.get("years")
        if years and isinstance(years, list):
            return [int(y) for y in years if isinstance(y, int)]
    return None

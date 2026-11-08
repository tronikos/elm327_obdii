"""Config-flow form-building helpers.

These produce plain dicts (not HA voluptuous schemas or selectors) so
the library stays HA-agnostic. The HA config flow wraps them in
``vol.Schema`` and ``selector.SelectSelector`` as needed.

Formulas are represented as structured ``fmt`` dicts (bix, len, mul,
div, add, sign, blsb, min, max, map). The config flow UI can present
these as individual fields, or accept a shorthand string (like
``B(4)/2.5``) that is parsed to a ``fmt`` dict via
:func:`parse_formula_string`.
"""

import re
from typing import Any

from obdii import commands as veh_commands

from ._core.elm327_parsing import as_float
from ._core.schema import CustomPid
from ._core.standard_pids import get_standard_command, is_supported_pids_bitmap


def all_known_standard_pid_names() -> list[str]:
    """Every standard Mode 01 PID name the obdii registry knows about.

    Excludes the SUPPORTED_PIDS_A..G bitmap commands (metadata, not
    user-trackable parameters) and any "Unnamed" command entries.
    """
    names: list[str] = []
    for cmd in veh_commands[1]:
        if cmd.name == "Unnamed":
            continue
        if is_supported_pids_bitmap(cmd.name):
            continue
        names.append(cmd.name)
    return names


def standard_pid_options(command_names: list[str]) -> list[dict[str, str]]:
    """Build the standard-PID multiselect options sorted by name.

    Returns a list of ``{"value": name, "label": "NAME (mode pid)"}`` dicts.
    """
    options: list[dict[str, str]] = []
    for name in sorted(command_names):
        cmd = get_standard_command(name)
        if cmd is None:
            continue
        label = f"{name} ({cmd.mode} {cmd.pid})"
        options.append({"value": name, "label": label})
    return options


def pid_to_form_defaults(pid: CustomPid) -> dict[str, Any]:
    """Pre-fill the edit form from an existing :class:`CustomPid`."""
    fmt = pid.fmt or {}
    # Try to render the fmt as a formula string for the simple text box.
    # If the fmt isn't string-expressible (enum, sub-byte, blsb), the
    # string field is empty and the structured fields carry the data.
    formula_str = fmt_to_string(fmt)
    return {
        "pid_name": pid.name,
        "mode": pid.mode,
        "query": pid.query,
        "can_header": pid.can_header or "",
        "can_filter": pid.can_filter or "",
        "init_extra": pid.init_extra or "",
        "formula": formula_str,
        "bix": fmt.get("bix", 0),
        "len": fmt.get("len", 8),
        "mul": fmt.get("mul", 1),
        "div": fmt.get("div", 1),
        "add": fmt.get("add", 0),
        "sign": fmt.get("sign", False),
        "blsb": fmt.get("blsb", False),
        "min": fmt.get("min"),
        "max": fmt.get("max"),
        "map_text": _map_to_text(fmt.get("map")),
        "unit": pid.unit or "",
        "device_class": pid.device_class or "",
        "state_class": pid.state_class or "",
        "min_value": pid.min_value,
        "max_value": pid.max_value,
        "expected_bytes": pid.expected_bytes or 0,
    }


def empty_form_defaults() -> dict[str, Any]:
    """Defaults for a brand-new custom PID form."""
    return {
        "pid_name": "",
        "mode": "22",
        "query": "",
        "can_header": "",
        "can_filter": "",
        "init_extra": "",
        "formula": "",
        "bix": 0,
        "len": 8,
        "mul": 1,
        "div": 1,
        "add": 0,
        "sign": False,
        "blsb": False,
        "min": None,
        "max": None,
        "map_text": "",
        "unit": "",
        "device_class": "",
        "state_class": "",
        "min_value": None,
        "max_value": None,
        "expected_bytes": 0,
    }


def user_input_to_form_defaults(user_input: dict[str, Any]) -> dict[str, Any]:
    """Build form defaults from submitted user input (preserves on validation error)."""
    bix_val = user_input.get("bix")
    len_val = user_input.get("len")
    mul_val = as_float(user_input.get("mul"))
    div_val = as_float(user_input.get("div"))
    add_val = as_float(user_input.get("add"))
    exp_val = user_input.get("expected_bytes")
    return {
        "pid_name": user_input.get("pid_name", ""),
        "mode": (user_input.get("mode") or "").strip().upper(),
        "query": (user_input.get("query") or "").strip().upper(),
        "can_header": (user_input.get("can_header") or "").strip().upper(),
        "can_filter": (user_input.get("can_filter") or "").strip().upper(),
        "init_extra": user_input.get("init_extra", ""),
        "formula": (user_input.get("formula") or "").strip(),
        "bix": int(bix_val) if bix_val not in (None, "") else 0,
        "len": int(len_val) if len_val not in (None, "") else 8,
        "mul": mul_val if mul_val is not None else 1,
        "div": div_val if div_val is not None else 1,
        "add": add_val if add_val is not None else 0,
        "sign": bool(user_input.get("sign")),
        "blsb": bool(user_input.get("blsb")),
        "min": as_float(user_input.get("min")),
        "max": as_float(user_input.get("max")),
        "map_text": user_input.get("map_text", ""),
        "unit": user_input.get("unit", ""),
        "device_class": user_input.get("device_class", ""),
        "state_class": user_input.get("state_class", ""),
        "min_value": as_float(user_input.get("min_value")),
        "max_value": as_float(user_input.get("max_value")),
        "expected_bytes": int(exp_val) if exp_val not in (None, "") else 0,
    }


def form_input_to_fmt(user_input: dict[str, Any]) -> dict[str, Any]:
    """Convert config-flow form input into a ``fmt`` dict.

    Validates required fields (bix, len) and includes optional fields
    only when non-default. Raises ``ValueError`` on invalid input.
    """
    bix_raw = user_input.get("bix")
    bix = int(bix_raw) if bix_raw is not None else 0
    len_raw = user_input.get("len")
    length = int(len_raw) if len_raw is not None else 8
    if bix < 0:
        raise ValueError("bix must be non-negative")
    if length < 1 or length > 64:
        raise ValueError("len must be between 1 and 64")

    fmt: dict[str, Any] = {"bix": bix, "len": length}

    mul = as_float(user_input.get("mul"))
    div = as_float(user_input.get("div"))
    add = as_float(user_input.get("add"))
    if mul is not None and mul != 1:
        fmt["mul"] = mul
    if div is not None and div != 1:
        if div == 0:
            raise ValueError("div must not be zero")
        fmt["div"] = div
    if add is not None and add != 0:
        fmt["add"] = add

    if user_input.get("sign"):
        fmt["sign"] = True
    if user_input.get("blsb"):
        fmt["blsb"] = True

    min_val = as_float(user_input.get("min"))
    max_val = as_float(user_input.get("max"))
    if min_val is not None:
        fmt["min"] = min_val
    if max_val is not None:
        fmt["max"] = max_val

    map_text = (user_input.get("map_text") or "").strip()
    if map_text:
        mapping = _parse_map_text(map_text)
        if mapping:
            fmt["map"] = mapping

    return fmt


# Shorthand string patterns for the config flow text box.
_RE_FORMULA_SINGLE = re.compile(
    r"^(?P<type>[BS])\((?P<a>\d+)(?:,\s*(?P<b>\d+))?\)"
    r"(?:\s*(?P<op>[*/])\s*(?P<val>[\d.]+))?$"
)
_RE_FORMULA_BIT = re.compile(r"^BIT\((\d+),\s*(\d+)\)$")


def parse_formula_string(source: str) -> dict[str, Any] | None:
    """Parse a shorthand formula string (e.g. ``B(4)/2.5``) to a ``fmt`` dict.

    Returns ``None`` if the string doesn't match a supported pattern.
    The caller should then reject the input (no string fallback in the
    simplified approach).

    Supported patterns:
      - ``B(n)`` → ``{"bix": n*8, "len": 8}``
      - ``S(n)`` → ``{"bix": n*8, "len": 8, "sign": True}``
      - ``B(n, m)`` → ``{"bix": n*8, "len": (m-n+1)*8}``
      - ``S(n, m)`` → with sign
      - ``BIT(b, n)`` → ``{"bix": b*8+n, "len": 1}``
      - Any of the above with ``/const`` or ``*const`` suffix
    """
    if not source:
        return None
    s = source.strip().replace(" ", "")

    m = _RE_FORMULA_BIT.match(s)
    if m:
        byte_idx = int(m.group(1))
        bit_idx = int(m.group(2))
        if bit_idx > 7:
            return None
        return {"bix": byte_idx * 8 + bit_idx, "len": 1}

    m = _RE_FORMULA_SINGLE.match(s)
    if not m:
        return None

    typ = m.group("type")
    a = int(m.group("a"))
    b = int(m.group("b")) if m.group("b") else a
    if b < a or (b - a + 1) * 8 > 64:
        return None
    fmt: dict[str, Any] = {
        "bix": a * 8,
        "len": (b - a + 1) * 8,
    }
    if typ == "S":
        fmt["sign"] = True
    if m.group("op"):
        try:
            val = float(m.group("val"))
        except ValueError:
            return None
        if m.group("op") == "*":
            fmt["mul"] = val
        elif m.group("op") == "/":
            if val == 0:
                return None
            fmt["div"] = val
    return fmt


def _map_to_text(mapping: dict[str, str] | None) -> str:
    """Serialize a map dict to the text format used in the config flow."""
    if not mapping:
        return ""
    lines = []
    for k, v in sorted(mapping.items(), key=lambda x: int(x[0])):
        lines.append(f"{k}={v}")
    return "\n".join(lines)


def _parse_map_text(text: str) -> dict[str, str] | None:
    r"""Parse the map text format (``0=Off\n1=On``) into a dict."""
    mapping: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key.lstrip("-").isdigit():
            continue
        mapping[key] = value
    return mapping or None


def fmt_to_string(fmt: dict[str, Any]) -> str:
    """Render a ``fmt`` dict as a formula string, or empty if not expressible.

    Returns a canonical string (e.g. ``B(4)/2.55``) when the fmt is:
      - Byte-aligned (bix % 8 == 0, len % 8 == 0)
      - No ``blsb``, no ``map``, no ``nullmin``/``nullmax``
      - Linear scaling only (mul, div, add)

    Returns empty string when the fmt uses features the string notation
    can't express (sub-byte fields, cross-byte non-aligned, enums,
    byte-swap). The config flow then shows the structured fields.
    """
    if not fmt:
        return ""

    bix = fmt.get("bix", 0)
    length = fmt.get("len", 0)

    # Must be byte-aligned and positive length.
    if length <= 0 or bix % 8 != 0 or length % 8 != 0:
        return ""

    # No advanced features.
    if (
        fmt.get("blsb")
        or fmt.get("map")
        or fmt.get("nullmin") is not None
        or fmt.get("nullmax") is not None
    ):
        return ""

    byte_start = bix // 8
    byte_count = length // 8
    prefix = "S" if fmt.get("sign") else "B"

    if byte_count == 1:
        core = f"{prefix}({byte_start})"
    else:
        byte_end = byte_start + byte_count - 1
        core = f"{prefix}({byte_start}, {byte_end})"

    # Scaling suffix.
    mul = fmt.get("mul", 1)
    div = fmt.get("div", 1)
    add = fmt.get("add", 0)

    suffix = ""
    if mul != 1 and div == 1 and add == 0:
        suffix = f"*{mul}"
    elif mul == 1 and div != 1 and add == 0:
        suffix = f"/{div}"
    elif mul != 1 or div != 1 or add != 0:
        # Complex scaling — can't express in a single suffix.
        # Fall back to structured fields.
        return ""

    return core + suffix


def form_input_to_fmt_from_hybrid(
    user_input: dict[str, Any],
    existing_fmt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a fmt dict from the hybrid form (string + structured fields).

    Priority:
      1. If the user filled in the ``formula`` string field:
         - If it does not parse as a valid shorthand formula, return ``None``.
         - If ``existing_fmt`` is provided and ``formula`` matches the
           untouched pre-filled formula string of ``existing_fmt``, fall
           through to the structured fields so edits made in the structured
           section are preserved.
         - Otherwise, use the parsed fmt and merge any structured fields
           that shorthand notation cannot express (``add``, ``blsb``,
           ``min``, ``max``, ``map_text``).
      2. If ``formula`` is empty, build the fmt from the structured fields
         (bix, len, mul, div, add, sign, blsb, min, max, map_text).
      3. Return ``None`` on invalid input so the caller shows an error.
    """
    formula_str = (user_input.get("formula") or "").strip()
    if formula_str:
        parsed = parse_formula_string(formula_str)
        if parsed is None:
            return None
        if existing_fmt is None or formula_str != fmt_to_string(existing_fmt):
            add_val = as_float(user_input.get("add"))
            if add_val is not None and add_val != 0:
                parsed["add"] = add_val
            if user_input.get("blsb"):
                parsed["blsb"] = True
            min_val = as_float(user_input.get("min"))
            max_val = as_float(user_input.get("max"))
            if min_val is not None:
                parsed["min"] = min_val
            if max_val is not None:
                parsed["max"] = max_val
            map_text = (user_input.get("map_text") or "").strip()
            if map_text:
                mapping = _parse_map_text(map_text)
                if mapping:
                    parsed["map"] = mapping
            return parsed

    # Build from structured fields.
    try:
        return form_input_to_fmt(user_input)
    except ValueError:
        return None

"""WiCAN vehicle profile importer.

Translates the upstream WiCAN JSON schema (used by the meatpiHQ/wican-fw
project) into our internal :class:`ProfileConfig`.

The WiCAN schema has this shape::

    {
      "car_model": "VW: e-Golf 2019 (Custom)",
      "init": "ATSP6;ATST96;",
      "pids": [
        {
          "pid": "22028C1",          # first 2 chars = mode, rest = pid hex
          "pid_init": "ATSH7E5;ATCRA7ED;",
          "parameters": [
            {"name": "SOC BMS", "expression": "B4/2.5", "unit": "%", "class": "battery"}
          ]
        }
      ]
    }

This importer is the ONLY place in the codebase that knows the WiCAN
shape. Reverse de-duplication runs here: any PID whose (mode, query)
maps to a known Mode 01 standard command is promoted to
``standard_pids`` and dropped from the custom parser.

Formula translation converts WiCAN string expressions to structured
``fmt`` dicts. WiCAN formulas operate on the "dirty array" (includes
PCI + mode echo + PID echo bytes); the ``fmt.bix`` offset is relative
to the clean payload (data bytes only). The translation accounts for
this offset: for Mode 22 responses, WiCAN ``B(n)`` maps to
``bix = (n - 4) * 8``; for Mode 01 responses, ``bix = (n - 3) * 8``.

Formulas that cannot be expressed as a single contiguous bit field with
linear scaling (multi-field products, non-contiguous bytes, non-linear
math) are skipped with a warning. This affects ~1% of WiCAN formulas;
the affected signals are typically available via OBDb with correct
contiguous offsets.
"""

import logging
import re
from typing import Any

from obdii import commands

from .._core.elm327_parsing import as_float
from .._core.schema import CustomPid, ProfileConfig
from .._core.standard_pids import is_supported_pids_bitmap

_LOGGER = logging.getLogger(__name__)

# Matches `AT<cmd> <args>;` where cmd is letters only (SH, CRA, SP, ST, Z, ...)
_WICAN_AT_CMD_RE: re.Pattern[str] = re.compile(
    r"AT\s*([A-Z]+)\s*([^;]*);?",
    re.IGNORECASE,
)

# WiCAN dirty-array header size per mode.
# Mode 01/02/09: dirty[0]=PCI, dirty[1]=mode echo, dirty[2]=PID → 3-byte header
# Mode 22: dirty[0]=PCI, dirty[1]=mode echo, dirty[2:4]=PID echo → 4-byte header
_DIRTY_HEADER_SIZE = {"01": 3, "02": 3, "09": 3, "22": 4}


def import_wican_profile(raw: dict[str, Any]) -> ProfileConfig:
    """Translate a WiCAN profile dict into a :class:`ProfileConfig`.

    Never raises on a single bad PID - skips it and continues, so one
    malformed entry doesn't lose the whole profile. Raises
    :class:`TypeError` only if ``raw`` is not a dict at all.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"WiCAN profile must be a dict, got {type(raw).__name__}")

    car_model = raw.get("car_model", "unknown")
    standard: set[str] = set()
    custom: list[CustomPid] = []

    profile_init = (raw.get("init") or "").strip()

    for block in raw.get("pids", []):
        try:
            if not isinstance(block, dict):
                continue
            mode, query = _split_wican_command(block.get("pid", ""))
            if not mode:
                continue
            header, can_filter, extra_init = _parse_pid_init(block.get("pid_init"))
            if profile_init:
                extra_init = _merge_init_strings(profile_init, extra_init)

            std_name = _match_standard_pid(mode, query)
            if std_name:
                standard.add(std_name)
                continue

            for param_idx, param in enumerate(
                _iter_parameters(block.get("parameters"))
            ):
                fmt = _parse_wican_formula_to_fmt(param.get("expression", ""), mode)
                if fmt is None:
                    _LOGGER.warning(
                        "Skipping WiCAN PID %s param %r in profile %r: "
                        "formula %r cannot be expressed as fmt (non-contiguous "
                        "or multi-field)",
                        mode + query,
                        param.get("name"),
                        car_model,
                        param.get("expression"),
                    )
                    continue
                pid_name = param.get("name") or f"{mode}{query}"
                pid_id = f"{mode}:{query}:{pid_name}:{param_idx}"
                custom.append(
                    CustomPid(
                        id=pid_id,
                        name=pid_name,
                        mode=mode,
                        query=query,
                        fmt=fmt,
                        can_header=header,
                        can_filter=can_filter,
                        init_extra=extra_init,
                        unit=param.get("unit"),
                        device_class=param.get("class"),
                        state_class=param.get("state_class"),
                        min_value=as_float(param.get("min")),
                        max_value=as_float(param.get("max")),
                        source=f"import:wican:{car_model}",
                    )
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Skipping malformed WiCAN PID block in profile %r: %s", car_model, err
            )

    return ProfileConfig(
        standard_pids=sorted(standard),
        custom_pids=custom,
    )


class WicanImporter:
    """Protocol-conforming importer for runtime dispatch."""

    def can_handle(self, raw: object) -> bool:
        """Return True if ``raw`` looks like a WiCAN profile dict."""
        return isinstance(raw, dict) and ("pids" in raw or "car_model" in raw)

    def import_profile(self, raw: object) -> ProfileConfig:
        """Translate a WiCAN profile dict into a :class:`ProfileConfig`."""
        if not isinstance(raw, dict):
            raise TypeError(f"WiCAN profile must be a dict, got {type(raw).__name__}")
        return import_wican_profile(raw)


def _split_wican_command(raw: Any) -> tuple[str, str]:
    """Split a WiCAN ``pid`` field like ``'22028C1'`` into ``('22', '028C1')``."""
    if not isinstance(raw, str):
        return ("", "")
    s = raw.strip().upper()
    if len(s) < 3:
        return ("", "")
    return (s[:2], s[2:])


def _parse_pid_init(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """Parse a WiCAN ``pid_init`` string into ``(header, filter, extra_init)``."""
    if not raw:
        return (None, None, None)
    header: str | None = None
    can_filter: str | None = None
    extras: list[str] = []
    for match in _WICAN_AT_CMD_RE.finditer(raw):
        cmd = match.group(1).upper()
        arg = (match.group(2) or "").strip()
        if cmd in ("H", "S") and arg == "0":
            _LOGGER.warning(
                "Filtering AT%s0 from pid_init (headers/spaces off break the "
                "response parser): %r",
                cmd,
                raw,
            )
            continue
        if cmd == "SH" and arg:
            header = arg.upper()
        elif cmd == "CRA" and arg:
            can_filter = arg.upper()
        else:
            extras.append(f"AT{cmd} {arg}".strip())
    return (header, can_filter, ";".join(extras) if extras else None)


def _merge_init_strings(a: str, b: str | None) -> str | None:
    """Merge two init strings, preserving order and dropping duplicates."""
    parts_a = [p.strip() for p in a.split(";") if p.strip()]
    parts_b = [p.strip() for p in (b or "").split(";") if p.strip()]
    seen: set[str] = set()
    merged: list[str] = []
    for p in parts_a + parts_b:
        key = re.sub(r"\s+", "", p).upper()
        if key in ("ATH0", "ATS0"):
            _LOGGER.warning(
                "Filtering %s from init string (headers/spaces off break the "
                "response parser)",
                p,
            )
            continue
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)
    return ";".join(merged) if merged else None


def _iter_parameters(raw: object) -> list[dict[str, Any]]:
    """Normalize the ``parameters`` field to a list of dicts."""
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict):
        return [
            {"name": k, "expression": v} for k, v in raw.items() if isinstance(v, str)
        ]
    return []


def _match_standard_pid(mode: str, query: str) -> str | None:
    """If ``(mode, query)`` is a known Mode 01 standard PID, return its name."""
    if mode != "01":
        return None
    pid_hex = query[:2]
    try:
        pid_int = int(pid_hex, 16)
    except ValueError:
        return None
    try:
        cmd = commands[1][pid_int]
    except KeyError:
        return None
    if cmd is None or cmd.name == "Unnamed":
        return None
    if is_supported_pids_bitmap(cmd.name):
        return None
    return cmd.name


def _parse_wican_formula_to_fmt(expr: Any, mode: str) -> dict[str, Any] | None:
    """Translate a WiCAN string expression to a ``fmt`` dict.

    Returns ``None`` if the formula cannot be expressed as a single
    contiguous bit field with linear scaling.

    Handles these WiCAN notation forms:
      - ``B<n>`` → unsigned byte at dirty-array index n
      - ``S<n>`` → signed byte
      - ``[B<m>:B<n>]`` → big-endian unsigned multi-byte slice
      - ``[S<m>:S<n>]`` → big-endian signed multi-byte slice
      - ``B<n>:<bit>`` → single bit extraction
      - Manual big-endian words: ``(B<m>*256)+B<m+1>``, ``(B<m><<8)+B<m+1>``,
        ``(B<m>*65536)+(B<m+1>*256)+B<m+2>``, 4-byte variants
      - Optional scaling: ``/const``, ``*const``, ``+const``, ``-const``

    The dirty-array byte index is translated to a clean-payload bit
    offset by subtracting the mode-specific header size (3 for Mode 01,
    4 for Mode 22) and multiplying by 8.
    """
    if not isinstance(expr, str):
        return None
    s = expr.strip().replace(" ", "")
    if not s:
        return None

    header_size = _DIRTY_HEADER_SIZE.get(mode, 4)

    # Try each pattern in order of specificity.
    for parser in (
        _try_slice,
        _try_bit_extraction,
        _try_single_byte,
        _try_manual_word,
    ):
        result = parser(s, header_size)
        if result is not None:
            return result

    return None


def _dirty_to_bix(dirty_index: int, header_size: int) -> int:
    """Convert a dirty-array byte index to a clean-payload bit offset."""
    clean_index = dirty_index - header_size
    if clean_index < 0:
        return 0
    return clean_index * 8


# [B<m>:B<n>] or [S<m>:S<n>]
_RE_SLICE = re.compile(r"^\[([BS])(\d+):([BS])(\d+)\](.*)$")


def _try_slice(s: str, header_size: int) -> dict[str, Any] | None:
    """Match ``[B<m>:B<n>]`` with optional scaling suffix."""
    m = _RE_SLICE.match(s)
    if not m:
        return None
    typ = m.group(1).upper()
    start = int(m.group(2))
    end = int(m.group(4))
    if m.group(3).upper() != typ:
        return None  # mixed B/S in slice - not expressible
    if end < start:
        return None
    suffix = m.group(5)
    scaling = _parse_scaling_suffix(suffix)
    if scaling is None:
        return None
    fmt: dict[str, Any] = {
        "bix": _dirty_to_bix(start, header_size),
        "len": (end - start + 1) * 8,
    }
    if typ == "S":
        fmt["sign"] = True
    fmt.update(scaling)
    return fmt


# B<n>:<bit> or S<n>:<bit>
_RE_BIT = re.compile(r"^([BS])(\d+):(\d+)$")


def _try_bit_extraction(s: str, header_size: int) -> dict[str, Any] | None:
    """Match ``B<n>:<bit>`` (single bit extraction)."""
    m = _RE_BIT.match(s)
    if not m:
        return None
    byte_idx = int(m.group(2))
    bit_idx = int(m.group(3))
    return {
        "bix": _dirty_to_bix(byte_idx, header_size) + bit_idx,
        "len": 1,
    }


# B<n> or S<n> with optional scaling
_RE_SINGLE = re.compile(r"^([BS])(\d+)(.*)$")


def _try_single_byte(s: str, header_size: int) -> dict[str, Any] | None:
    """Match ``B<n>`` or ``S<n>`` with optional scaling suffix."""
    m = _RE_SINGLE.match(s)
    if not m:
        return None
    typ = m.group(1).upper()
    idx = int(m.group(2))
    suffix = m.group(3)
    scaling = _parse_scaling_suffix(suffix)
    if scaling is None:
        return None
    fmt: dict[str, Any] = {
        "bix": _dirty_to_bix(idx, header_size),
        "len": 8,
    }
    if typ == "S":
        fmt["sign"] = True
    fmt.update(scaling)
    return fmt


# Manual big-endian words: (B<m>*256)+B<m+1>, (B<m><<8)+B<m+1>,
# 3-byte and 4-byte variants, with optional scaling.
_RE_WORD2 = re.compile(r"^\(?B(\d+)\*256\)?\+B(\d+)(.*)$")
_RE_WORD2_SHIFT = re.compile(r"^\(?B(\d+)<<8\)?\+B(\d+)(.*)$")
_RE_WORD3 = re.compile(r"^\(?B(\d+)\*65536\)?\+\(?B(\d+)\*256\)?\+B(\d+)(.*)$")
_RE_WORD3_SHIFT = re.compile(r"^\(?B(\d+)<<16\)?\+\(?B(\d+)<<8\)?\+B(\d+)(.*)$")
_RE_WORD4 = re.compile(
    r"^\(?B(\d+)<<24\)?\+\(?B(\d+)<<16\)?\+\(?B(\d+)<<8\)?\+B(\d+)(.*)$"
)
_RE_WORD4_MUL = re.compile(
    r"^\(?B(\d+)\*16777216\)?\+\(?B(\d+)\*65536\)?\+\(?B(\d+)\*256\)?\+B(\d+)(.*)$"
)


def _try_manual_word(s: str, header_size: int) -> dict[str, Any] | None:
    """Match manual big-endian word constructions like ``(B4*256)+B5``."""
    for pattern, byte_count in (
        (_RE_WORD4, 4),
        (_RE_WORD4_MUL, 4),
        (_RE_WORD3, 3),
        (_RE_WORD3_SHIFT, 3),
        (_RE_WORD2, 2),
        (_RE_WORD2_SHIFT, 2),
    ):
        m = pattern.match(s)
        if not m:
            continue
        indices = [int(m.group(i + 1)) for i in range(byte_count)]
        suffix = m.group(byte_count + 1)

        # Check contiguity: indices must be consecutive ascending.
        expected = list(range(indices[0], indices[0] + byte_count))
        if indices != expected:
            return None  # non-contiguous - not expressible

        scaling = _parse_scaling_suffix(suffix)
        if scaling is None:
            return None
        return {
            "bix": _dirty_to_bix(indices[0], header_size),
            "len": byte_count * 8,
            **scaling,
        }
    return None


def _parse_scaling_suffix(suffix: str) -> dict[str, float] | None:
    """Parse an optional scaling suffix like ``/2.5``, ``*1.8+32``, ``-40``.

    Returns ``None`` if the suffix contains operations beyond simple
    linear scaling (mul/div/add). Returns a dict with keys from
    ``{"mul", "div", "add"}`` (only present if non-default).
    """
    if not suffix:
        return {}

    # Must be a sequence of */X and +X/-X operations only.
    # Tokenize: split into operator+value pairs.
    tokens = re.findall(r"([*/+-])([\d.]+)", suffix)
    if not tokens:
        return None

    # Reconstruct to verify the whole suffix was consumed.
    reconstructed = "".join(op + val for op, val in tokens)
    if reconstructed != suffix:
        return None  # unparsable characters remain

    mul = 1.0
    div = 1.0
    add = 0.0
    for op, val in tokens:
        v = float(val)
        if op == "*":
            mul *= v
        elif op == "/":
            div *= v
        elif op == "+":
            add += v
        elif op == "-":
            add -= v

    result: dict[str, float] = {}
    if mul != 1.0:
        result["mul"] = mul
    if div != 1.0:
        result["div"] = div
    if add != 0.0:
        result["add"] = add
    return result

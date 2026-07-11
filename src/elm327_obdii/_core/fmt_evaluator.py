"""Structured ``fmt`` dict evaluator for custom PID formulas.

Replaces the former AST-based string evaluator (``formula.py``). No
``eval()``, no AST, no string fallback. Each custom PID carries a
``fmt`` dict with bit-extraction and linear-scaling fields matching
OBDb's signal definition format.

Bit numbering is **Motorola (big-endian bit, MSB-first within each
byte)** — verified against OBDb test case ``7E0.0101.yaml`` where
``CCM_SUP`` at ``bix=13`` correctly extracts bit 5 (``0x04``) of byte 1.

The ``blsb`` flag reverses byte order before Motorola bit extraction,
handling Intel-byte-arrangement signals (little-endian byte order).
"""

from collections.abc import Callable
from typing import Any

__all__ = [
    "FmtValidationError",
    "evaluate_fmt",
    "make_fmt_evaluator",
    "validate_fmt",
]


class FmtValidationError(ValueError):
    """Raised when a ``fmt`` dict fails structural validation."""


def validate_fmt(fmt: dict[str, Any]) -> None:
    """Validate that ``fmt`` has the required fields with correct types.

    Raises :class:`FmtValidationError` on any problem. Does not modify
    the dict.
    """
    if not isinstance(fmt, dict):
        raise FmtValidationError(f"fmt must be a dict, got {type(fmt).__name__}")

    if "bix" not in fmt:
        raise FmtValidationError("fmt is missing required field 'bix'")
    if "len" not in fmt:
        raise FmtValidationError("fmt is missing required field 'len'")

    bix = fmt["bix"]
    length = fmt["len"]
    if not isinstance(bix, int) or isinstance(bix, bool) or bix < 0:
        raise FmtValidationError(f"fmt.bix must be a non-negative int, got {bix!r}")
    if not isinstance(length, int) or isinstance(length, bool) or length < 1:
        raise FmtValidationError(f"fmt.len must be a positive int, got {length!r}")
    if length > 64:
        raise FmtValidationError(f"fmt.len must be <= 64, got {length}")

    for key in ("mul", "div", "add", "min", "max", "nullmin", "nullmax"):
        if key in fmt and fmt[key] is not None:
            val = fmt[key]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise FmtValidationError(
                    f"fmt.{key} must be a number or None, got {val!r}"
                )

    if fmt.get("div", 1) == 0:
        raise FmtValidationError("fmt.div must not be zero")

    for key in ("sign", "blsb"):
        if key in fmt and fmt[key] is not None:
            if not isinstance(fmt[key], bool):
                raise FmtValidationError(f"fmt.{key} must be a bool, got {fmt[key]!r}")

    if "map" in fmt and fmt["map"] is not None:
        if not isinstance(fmt["map"], dict):
            raise FmtValidationError("fmt.map must be a dict")
        for k, v in fmt["map"].items():
            if not str(k).lstrip("-").isdigit():
                raise FmtValidationError(f"fmt.map key {k!r} must be an integer string")
            if not isinstance(v, str):
                raise FmtValidationError(f"fmt.map[{k!r}] must be a string, got {v!r}")


def evaluate_fmt(payload: list[int], fmt: dict[str, Any]) -> float | str | None:
    """Evaluate a ``fmt`` dict against a clean-payload byte list.

    ``payload`` is the **clean payload** — data bytes only, after the
    CAN header, PCI byte, mode echo, and PID echo have been stripped.
    The caller is responsible for stripping those bytes (see
    :func:`elm327_obdii._core.elm327_parsing.extract_clean_payload`).

    Returns:
        - ``str`` if ``fmt.map`` is present and the raw value is in the map
        - ``float`` for numeric results (including clamped)
        - ``None`` if the bit field is out of bounds, or
          ``nullmin``/``nullmax`` excludes the raw value, or the map
          has no entry for the raw value
    """
    try:
        bix: int = fmt["bix"]
        length: int = fmt["len"]
    except KeyError as exc:
        raise FmtValidationError(f"fmt is missing required field {exc}") from exc

    if length < 1 or length > 64:
        return None

    start_byte = bix // 8
    end_byte = (bix + length - 1) // 8

    if end_byte >= len(payload):
        return None

    # Extract the byte range, optionally byte-swapped (Intel byte order).
    target_bytes = list(payload[start_byte : end_byte + 1])
    if fmt.get("blsb"):
        target_bytes.reverse()

    # Build the raw integer from the (possibly swapped) bytes, then
    # shift+mask to isolate the field within the byte alignment.
    chunk = 0
    for b in target_bytes:
        chunk = (chunk << 8) | (b & 0xFF)

    chunk_bits = len(target_bytes) * 8
    local_offset = bix % 8
    shift = chunk_bits - local_offset - length
    raw = (chunk >> shift) & ((1 << length) - 1)

    # Two's complement signed interpretation.
    if fmt.get("sign") and (raw >> (length - 1)) & 1:
        raw -= 1 << length

    # Null-range sentinels (OBDb fmt nullmin/nullmax).
    nullmin = fmt.get("nullmin")
    if nullmin is not None and raw < nullmin:
        return None
    nullmax = fmt.get("nullmax")
    if nullmax is not None and raw > nullmax:
        return None

    # Enumeration: return the mapped string, or None if not in map.
    mapping = fmt.get("map")
    if mapping is not None:
        mapped: str | None = mapping.get(str(raw))
        return mapped

    # Linear scaling: raw * mul / div + add
    mul = fmt.get("mul", 1)
    div = fmt.get("div", 1)
    add = fmt.get("add", 0)
    if div == 0:
        return None
    try:
        result = raw * mul / div + add
    except (TypeError, ZeroDivisionError, OverflowError):
        return None

    # Clamping.
    min_val = fmt.get("min")
    if min_val is not None and result < min_val:
        result = min_val
    max_val = fmt.get("max")
    if max_val is not None and result > max_val:
        result = max_val

    return float(result)


def make_fmt_evaluator(
    fmt: dict[str, Any],
) -> Callable[[list[int]], float | str | None]:
    """Validate ``fmt`` once, then return a closure that evaluates it.

    The returned closure takes a clean-payload byte list and returns the
    computed value (or ``None``). Validation runs once at call time;
    runtime evaluation skips the type checks for speed.
    """
    validate_fmt(fmt)

    def evaluate(payload: list[int]) -> float | str | None:
        try:
            return evaluate_fmt(payload, fmt)
        except (KeyError, TypeError, IndexError):
            return None

    return evaluate

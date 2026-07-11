"""ELM327 raw-response parsing and small coercion helpers.

The "dirty array" data contract
-------------------------------
Custom OBD-II formulas in WiCAN, Torque, and RealDash profiles are
written by users looking at raw ELM327 terminal output. When a user
sends ``22 028C`` and sees::

    7E8 05 62 02 8C 1F A0 03 E8

...they count bytes from left to right starting at the PCI byte:
index 0 = ``05`` (PCI), 1 = ``62`` (mode echo), 2 = ``02`` (PID high),
3 = ``8C`` (PID low), 4 = ``1F`` (first payload byte), etc. A formula
like ``B(4) / 2.55`` refers to that 5th byte overall.

This is the de facto industry-standard data contract for custom PID
formulas. py-obdii's ``Response.unparsed`` is a *clean* payload with
PCI/mode/PID bytes stripped - which is correct for standard Mode 01
PIDs (where py-obdii's own typed resolvers handle the scaling), but
*wrong* for custom PIDs whose formulas were authored against the
dirty-array convention.

ELM327 adapters with ``AT CAF1`` (CAN Auto Formatting, the default)
handle ISO 15765-2 multi-frame reassembly in hardware and emit the
reassembled payload as a single text response. The interspersed PCI
bytes that appear at frame boundaries in the dirty array are the
*adapter's* PCI bytes, not raw CAN-frame PCI bytes - they're part
of the ELM327's text output format that formula authors see and
count against.
"""

import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

_OBD_ERROR_TOKENS = ("DATA", "ERROR", "STOPPED", "UNABLE", "BUS")

_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")

# Matches 1-2 digits, decimal point, 1-3 digits (e.g. "14.2V", "12.80V",
# "14.234V"). Negative lookarounds prevent matching longer numbers like
# "123.45".
_VOLTAGE_RE = re.compile(r"(?<!\d)(\d{1,2}\.\d{1,3})(?!\d)")


def extract_dirty_array(raw_response: bytes) -> list[int]:
    """Parse an ELM327 raw text response into a flat list of hex bytes.

    This is the "dirty array" that custom PID formulas (WiCAN, Torque,
    RealDash notation) are written against. It includes PCI bytes,
    mode echoes, and PID echoes at the positions the formula authors
    see when looking at raw ELM327 terminal output.

    Handles:
      - Space-delimited packets (AT S1, default): "7E8 05 41 0C 1A F8"
      - Contiguous hex strings (AT S0, spaces off): "7E805410C1AF8"
      - 11-bit CAN headers (3 hex chars): "7E8"
      - 29-bit CAN headers (8 hex chars): "18DAF110"
      - Multi-line responses (one CAN frame per line)
      - ``>`` prompt character stripping
      - Error token filtering (DATA, ERROR, STOPPED, UNABLE, BUS)

    The CAN header token at the start of each line is skipped - only
    the data bytes (PCI + mode echo + PID echo + payload) are included
    in the returned array.
    """
    dirty_array: list[int] = []
    try:
        raw_str = raw_response.decode("utf-8", errors="ignore")
        lines = [
            line.strip()
            for line in raw_str.splitlines()
            if line.strip() and ">" not in line
        ]
        for line in lines:
            if any(token in line for token in _OBD_ERROR_TOKENS):
                continue

            parts = line.split()

            # Fallback for AT S0 (spaces off) returning contiguous hex strings.
            # A single token longer than 3 chars is a contiguous hex frame -
            # we need to split off the CAN header (3 or 8 chars) and then
            # chunk the rest into byte pairs.
            #
            # Header-length heuristic: 29-bit CAN (ISO 15765-4 / J1939)
            # headers are 8 hex chars and start with "18" (e.g. "18DAF110").
            # 11-bit CAN headers are 3 hex chars (e.g. "7E8"). Checking the
            # "18" prefix is the standard way to distinguish them in
            # spaces-off mode.
            #
            # Note: using >= 8 instead of > 8 so header-only frames (exactly
            # 8 chars, no payload) are still recognized as 29-bit and dropped
            # (no payload bytes to parse) rather than mis-parsed as 11-bit.
            if len(parts) == 1 and len(line) > 3:
                token = parts[0]
                _LOGGER.debug(
                    "Contiguous-hex fallback (AT S0) fired for frame: %r", token
                )
                if len(token) >= 8 and token[:2].upper() == "18":
                    header_len = 8
                else:
                    header_len = 3
                if len(token) > header_len:
                    payload = token[header_len:]
                    parts = [token[:header_len]] + [
                        payload[i : i + 2]
                        for i in range(0, len(payload) - (len(payload) % 2), 2)
                    ]
                    if len(payload) % 2:
                        _LOGGER.debug(
                            "Odd trailing nibble in spaces-off frame: %r", token
                        )

            if len(parts) > 1:
                # First word is the CAN header (e.g., '7E8'). Skip it -
                # the dirty array is data bytes only (PCI + echoes + payload).
                for part in parts[1:]:
                    try:
                        dirty_array.append(int(part, 16))
                    except ValueError:
                        _LOGGER.debug("Non-hex token in frame, skipping: %r", part)

    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not extract dirty array: %s", err)
    return dirty_array


def extract_clean_payload(raw_response: bytes, mode: str) -> list[int]:
    """Parse an ELM327 raw response into the clean payload (data bytes only).

    Strips the CAN header, PCI byte, mode echo, and PID echo — leaving
    only the data bytes that ``fmt.bix`` offsets are measured against.

    For a Mode 01 response ``7E8 04 41 0C 1A F8``:
        dirty array = ``[0x04, 0x41, 0x0C, 0x1A, 0xF8]``
        clean payload = ``[0x1A, 0xF8]``  (2 data bytes after PCI + 41 + 0C)

    For a Mode 22 response ``7ED 05 62 1E 3B 05 31``:
        dirty array = ``[0x05, 0x62, 0x1E, 0x3B, 0x05, 0x31]``
        clean payload = ``[0x05, 0x31]``  (2 data bytes after PCI + 62 + 1E3B)

    The PID echo length depends on the mode:
        Mode 01: 1-byte PID echo → strip 3 bytes (PCI + mode + PID)
        Mode 02: 1-byte PID echo → strip 3 bytes
        Mode 22: 2-byte PID echo → strip 4 bytes (PCI + mode + PIDhi + PIDlo)
        Other:   strip 3 bytes (conservative default)

    Falls back to the dirty array (minus the PCI byte) if the mode
    echo doesn't match — degrades gracefully on malformed responses.
    """
    dirty = extract_dirty_array(raw_response)
    if not dirty:
        return []

    # Expect the second byte to be the mode echo (0x40 + mode_int).
    try:
        mode_int = int(mode, 16)
    except ValueError:
        mode_int = 0

    expected_echo = 0x40 + mode_int
    if len(dirty) >= 2 and dirty[1] == expected_echo:
        if mode_int in (0x01, 0x02, 0x09):
            # 1-byte PID echo
            return dirty[3:] if len(dirty) > 3 else []
        if mode_int == 0x22:
            # 2-byte PID echo
            return dirty[4:] if len(dirty) > 4 else []
        # Unknown mode — strip PCI + mode + 1 PID byte as a guess
        return dirty[3:] if len(dirty) > 3 else []

    # Mode echo mismatch — return everything after the PCI byte.
    return dirty[1:] if len(dirty) > 1 else []


def extract_voltage(raw_response: bytes) -> float | None:
    """Parse a voltage float from an ``AT RV`` raw response."""
    raw_text = raw_response.decode("utf-8", errors="ignore")
    match = _VOLTAGE_RE.search(raw_text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def extract_protocol_number(raw_response: bytes) -> str | None:
    r"""Parse the protocol number from an ``ATDPN`` raw response.

    ``ATDPN`` returns just a number (no ``OK``), so the raw response is
    typically ``b"6\r\r>"`` or similar. The number is a single digit
    1-9 or letter A-C.

    Echo-safe: if the adapter still has command echo on (``ATE1``), the
    response looks like ``b"ATDPN\r6\r\r>"`` - the echoed command line
    is stripped before parsing so we don't misread ``A`` (from "ATDPN")
    as the protocol.
    """
    text = raw_response.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("AT"):
            continue
        for ch in line:
            if ch.isalnum():
                return ch.upper()
    return None


def is_hex(s: str) -> bool:
    """True if ``s`` is a non-empty string of hex digits (no ``0x`` prefix)."""
    if not s:
        return False
    return _HEX_RE.fullmatch(s) is not None


def as_float(v: Any) -> float | None:
    """Coerce ``v`` to float, returning None on failure or empty input."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

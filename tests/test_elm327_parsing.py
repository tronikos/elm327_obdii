r"""Tests for :mod:`elm327_obdii._core.elm327_parsing`.

Raw-response fixtures are real ELM327 text output, mirroring the
byte-string style used by the py-obdii test suite
(``b"7E8 04 41 0C 40 80\\r>"``). The dirty-array contract is what
custom PID formulas are authored against - PCI bytes, mode echoes, and
PID echoes at the positions a user sees in a terminal.
"""

import pytest

from elm327_obdii._core.elm327_parsing import (
    as_float,
    extract_clean_payload,
    extract_dirty_array,
    extract_protocol_number,
    extract_voltage,
    is_hex,
)


class TestExtractDirtyArray:
    """Parse ELM327 raw text into a flat list of hex bytes."""

    def test_single_frame_11bit(self) -> None:
        """Standard 11-bit single-frame response: header + 4 data bytes."""
        raw = b"7E8 04 41 0C 1A F8\r>"
        assert extract_dirty_array(raw) == [0x04, 0x41, 0x0C, 0x1A, 0xF8]

    def test_single_frame_29bit(self) -> None:
        """29-bit CAN header (8 hex chars, 18DAF110 prefix)."""
        raw = b"18DAF110 05 41 0C 41 C2\r>"
        assert extract_dirty_array(raw) == [0x05, 0x41, 0x0C, 0x41, 0xC2]

    def test_multi_line_response(self) -> None:
        """Multi-frame ISO-TP response (AT CAF1 reassembles in hardware)."""
        raw = b"7E8 10 14 49 02 01 57 56 57\r7E8 21 5A 5A 5A 31 4A 4D 33\r7E8 22 36 33 39 37 36 00 00\r>"
        result = extract_dirty_array(raw)
        assert result[:5] == [0x10, 0x14, 0x49, 0x02, 0x01]
        # The dirty array includes all data bytes from all frames (headers stripped)
        assert len(result) == 24

    def test_strips_prompt_char(self) -> None:
        """The '>' prompt character is stripped, not parsed as data."""
        raw = b"7E8 03 41 05 7C\r\r>"
        assert extract_dirty_array(raw) == [0x03, 0x41, 0x05, 0x7C]

    def test_filters_error_tokens(self) -> None:
        """Lines containing OBD error tokens are skipped."""
        raw = b"SEARCHING...\r7E8 03 41 05 7C\rUNABLE TO CONNECT\r>"
        assert extract_dirty_array(raw) == [0x03, 0x41, 0x05, 0x7C]

    def test_filters_data_error(self) -> None:
        """'DATA ERROR' lines are filtered, valid frames survive."""
        raw = b"DATA ERROR\r7E8 03 41 05 7C\r>"
        assert extract_dirty_array(raw) == [0x03, 0x41, 0x05, 0x7C]

    def test_contiguous_hex_spaces_off_11bit(self) -> None:
        """AT S0 (spaces off) returns contiguous hex; 11-bit header split."""
        raw = b"7E804410C1AF8\r>"
        assert extract_dirty_array(raw) == [0x04, 0x41, 0x0C, 0x1A, 0xF8]

    def test_contiguous_hex_spaces_off_29bit(self) -> None:
        """AT S0 with 29-bit header (18 prefix) splits 8-char header."""
        raw = b"18DAF11005410C41C2\r>"
        result = extract_dirty_array(raw)
        assert result == [0x05, 0x41, 0x0C, 0x41, 0xC2]

    def test_empty_response(self) -> None:
        """Empty input returns an empty list."""
        assert extract_dirty_array(b"") == []

    def test_prompt_only(self) -> None:
        """A lone prompt returns an empty list."""
        assert extract_dirty_array(b">") == []

    def test_non_hex_token_skipped(self) -> None:
        """Non-hex tokens within a frame are skipped, not fatal."""
        raw = b"7E8 ZZ 41 0C 1A F8\r>"
        assert extract_dirty_array(raw) == [0x41, 0x0C, 0x1A, 0xF8]

    def test_multi_ecu_response(self) -> None:
        """Multiple ECUs responding with different headers are all parsed."""
        raw = b"7E8 04 41 0C 40 80\r7E2 04 41 0C 40 40\r>"
        result = extract_dirty_array(raw)
        # Both frames' data bytes are concatenated into the dirty array
        assert len(result) == 10

    def test_custom_pid_response(self) -> None:
        """A Mode 22 custom-PID response with ATSH/ATCRA set."""
        raw = b"7ED 05 62 02 8C 1F A0\r>"
        assert extract_dirty_array(raw) == [0x05, 0x62, 0x02, 0x8C, 0x1F, 0xA0]


class TestExtractVoltage:
    """Parse a voltage float from an AT RV response."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"14.2V\r>", 14.2),
            (b"12.80V\r>", 12.80),
            (b"14.234V\r>", 14.234),
            (b"AT RV\r14.2V\r\r>", 14.2),
            (b"9.5V\r>", 9.5),
        ],
        ids=[
            "one-decimal",
            "two-decimals",
            "three-decimals",
            "with-echo",
            "low-voltage",
        ],
    )
    def test_valid_voltage(self, raw: bytes, expected: float) -> None:
        """Parseable voltages return the float value."""
        assert extract_voltage(raw) == pytest.approx(expected)

    def test_no_voltage_returns_none(self) -> None:
        """No voltage pattern returns None."""
        assert extract_voltage(b"NO DATA\r>") is None

    def test_empty_returns_none(self) -> None:
        """Empty input returns None."""
        assert extract_voltage(b"") is None

    def test_long_number_not_matched(self) -> None:
        """A 3-digit integer part (e.g. '123.45') is not matched as voltage."""
        assert extract_voltage(b"123.45V\r>") is None


class TestExtractProtocolNumber:
    """Parse the protocol number from an ATDPN response."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"6\r\r>", "6"),
            (b"7\r\r>", "7"),
            (b"8\r\r>", "8"),
            (b"9\r\r>", "9"),
            (b"A\r\r>", "A"),
            (b"ATDPN\r6\r\r>", "6"),
            (b"ATDPN\r7\r\r>", "7"),
        ],
        ids=["proto-6", "proto-7", "proto-8", "proto-9", "proto-A", "echo-6", "echo-7"],
    )
    def test_valid_protocol(self, raw: bytes, expected: str) -> None:
        """Protocol numbers are extracted as uppercased single chars."""
        assert extract_protocol_number(raw) == expected

    def test_empty_returns_none(self) -> None:
        """Empty input returns None."""
        assert extract_protocol_number(b"") is None

    def test_no_alnum_returns_none(self) -> None:
        """Non-alphanumeric input returns None."""
        assert extract_protocol_number(b"\r\r>") is None

    def test_strips_at_echo(self) -> None:
        """The echoed 'ATDPN' line is not misread as protocol 'A'."""
        assert extract_protocol_number(b"ATDPN\r6\r\r>") == "6"


class TestIsHex:
    """Hex-string validation."""

    @pytest.mark.parametrize(
        "s",
        ["7E8", "7DF", "028C", "ABCDEF", "0123456789", "abcdef"],
        ids=["7e8", "7df", "028c", "uppercase", "digits", "lowercase"],
    )
    def test_valid_hex(self, s: str) -> None:
        """Valid hex strings return True."""
        assert is_hex(s) is True

    @pytest.mark.parametrize(
        "s",
        ["", "0x7E8", "7E8 ", " 7E8", "GHIJ", "7E8\r", "7E8>", "ZZ"],
        ids=[
            "empty",
            "0x-prefix",
            "trailing-space",
            "leading-space",
            "non-hex",
            "cr",
            "prompt",
            "zz",
        ],
    )
    def test_invalid_hex(self, s: str) -> None:
        """Non-hex strings return False."""
        assert is_hex(s) is False


class TestAsFloat:
    """Coerce arbitrary values to float."""

    @pytest.mark.parametrize(
        ("v", "expected"),
        [
            (1, 1.0),
            (1.5, 1.5),
            ("3.14", 3.14),
            ("42", 42.0),
            (0, 0.0),
        ],
        ids=["int", "float", "float-str", "int-str", "zero"],
    )
    def test_valid(self, v: object, expected: float) -> None:
        """Coercible values return the float."""
        assert as_float(v) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "v",
        [None, "", "abc", [], {}],
        ids=["none", "empty-str", "non-numeric-str", "list", "dict"],
    )
    def test_invalid_returns_none(self, v: object) -> None:
        """Non-coercible values return None."""
        assert as_float(v) is None


class TestExtractCleanPayload:
    """Parse ELM327 raw response into the clean payload (data bytes only)."""

    def test_mode22_single_frame(self) -> None:
        """Mode 22 response: strip PCI + mode echo + 2-byte PID echo."""
        raw = b"7ED 05 62 1E 3B 05 31\r>"
        assert extract_clean_payload(raw, "22") == [0x05, 0x31]

    def test_mode01_single_frame(self) -> None:
        """Mode 01 response: strip PCI + mode echo + 1-byte PID echo."""
        raw = b"7E8 04 41 0C 1A F8\r>"
        assert extract_clean_payload(raw, "01") == [0x1A, 0xF8]

    def test_mode22_multi_byte_pid(self) -> None:
        """Mode 22 with a longer PID payload."""
        raw = b"7ED 05 62 02 8C 1F A0\r>"
        assert extract_clean_payload(raw, "22") == [0x1F, 0xA0]

    def test_empty_response(self) -> None:
        """Empty input returns empty list."""
        assert extract_clean_payload(b"", "22") == []

    def test_mode_echo_mismatch_fallback(self) -> None:
        """If the mode echo doesn't match, fall back to dirty[1:]."""
        raw = b"7ED 05 00 1E 3B 05 31\r>"  # mode echo 0x00, not 0x62
        result = extract_clean_payload(raw, "22")
        # Fallback: strip PCI only → [0x00, 0x1E, 0x3B, 0x05, 0x31]
        assert result == [0x00, 0x1E, 0x3B, 0x05, 0x31]

    def test_prompt_only(self) -> None:
        """A lone prompt returns empty list."""
        assert extract_clean_payload(b">", "22") == []

    def test_strips_error_tokens(self) -> None:
        """Error tokens are filtered before clean payload extraction."""
        raw = b"SEARCHING...\r7E8 04 41 0C 1A F8\r>"
        assert extract_clean_payload(raw, "01") == [0x1A, 0xF8]

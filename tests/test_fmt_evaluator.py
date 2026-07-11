"""Tests for :mod:`elm327_obdii._core.fmt_evaluator`.

Covers the structured ``fmt`` dict evaluator: bit extraction (Motorola
numbering), linear scaling, signed interpretation, byte-swap (blsb),
clamping, null sentinels, and enumerations.

Test fixtures are drawn from OBDb's real test cases (e.g. the e-Golf
HV battery voltage at ``div: 4`` yielding 332.25V, and the SAE J1979
``CCM_SUP`` signal at ``bix: 13``).
"""

import pytest

from elm327_obdii import (
    FmtValidationError,
    evaluate_fmt,
    make_fmt_evaluator,
    validate_fmt,
)


class TestValidateFmt:
    """Schema validation of the fmt dict."""

    def test_minimal_valid(self) -> None:
        """A fmt with only bix and len is valid."""
        validate_fmt({"bix": 0, "len": 8})

    def test_missing_bix(self) -> None:
        """Missing bix raises."""
        with pytest.raises(FmtValidationError, match="bix"):
            validate_fmt({"len": 8})

    def test_missing_len(self) -> None:
        """Missing len raises."""
        with pytest.raises(FmtValidationError, match="len"):
            validate_fmt({"bix": 0})

    def test_negative_bix(self) -> None:
        """Negative bix raises."""
        with pytest.raises(FmtValidationError, match="non-negative"):
            validate_fmt({"bix": -1, "len": 8})

    def test_zero_len(self) -> None:
        """Zero len raises."""
        with pytest.raises(FmtValidationError, match="positive"):
            validate_fmt({"bix": 0, "len": 0})

    def test_len_over_64(self) -> None:
        """Len > 64 raises."""
        with pytest.raises(FmtValidationError, match="<= 64"):
            validate_fmt({"bix": 0, "len": 65})

    def test_div_zero(self) -> None:
        """Zero div raises."""
        with pytest.raises(FmtValidationError, match="zero"):
            validate_fmt({"bix": 0, "len": 8, "div": 0})

    def test_not_a_dict(self) -> None:
        """Non-dict raises."""
        with pytest.raises(FmtValidationError, match="dict"):
            validate_fmt("not a dict")  # type: ignore[arg-type]

    def test_map_must_be_dict(self) -> None:
        """Non-dict map raises."""
        with pytest.raises(FmtValidationError, match="map"):
            validate_fmt({"bix": 0, "len": 8, "map": "not a dict"})

    def test_valid_full_fmt(self) -> None:
        """A fully-populated fmt is valid."""
        validate_fmt(
            {
                "bix": 32,
                "len": 16,
                "mul": 1.5,
                "div": 4,
                "add": -511,
                "sign": True,
                "blsb": True,
                "min": -200,
                "max": 200,
                "map": {"0": "Off", "1": "On"},
            }
        )


class TestEvaluateFmtBasic:
    """Basic bit extraction and scaling."""

    @pytest.mark.parametrize(
        ("payload", "fmt", "expected"),
        [
            # Single byte, no scaling
            ([0x41], {"bix": 0, "len": 8}, 0x41),
            # Single byte, divide
            ([0xFF], {"bix": 0, "len": 8, "div": 2.55}, 100.0),
            # 16-bit big-endian, divide (e-Golf voltage)
            ([0x05, 0x31], {"bix": 0, "len": 16, "div": 4}, 332.25),
            # 16-bit with clamping
            ([0x05, 0x31], {"bix": 0, "len": 16, "div": 4, "max": 100}, 100.0),
            # 32-bit
            ([0x00, 0x01, 0x00, 0x00], {"bix": 0, "len": 32}, 65536.0),
            # Multiply
            ([0x0A], {"bix": 0, "len": 8, "mul": 1.8}, 18.0),
            # Add offset
            ([0x0A], {"bix": 0, "len": 8, "add": -40}, -30.0),
            # Combined mul/add: 100*1.8 + (-40) = 140
            ([0x64], {"bix": 0, "len": 8, "mul": 1.8, "add": -40}, 140.0),
        ],
        ids=[
            "single-byte",
            "scaled-soc",
            "egolf-voltage",
            "clamped",
            "32bit",
            "multiply",
            "add-offset",
            "combined",
        ],
    )
    def test_evaluate(self, payload: list[int], fmt: dict, expected: float) -> None:
        """The evaluator returns the expected value."""
        result = evaluate_fmt(payload, fmt)
        assert result is not None
        assert pytest.approx(result) == expected

    def test_returns_float(self) -> None:
        """Numeric results are always float."""
        result = evaluate_fmt([42], {"bix": 0, "len": 8})
        assert isinstance(result, float)
        assert result == 42.0


class TestEvaluateFmtBitOffset:
    """Bit offset extraction (Motorola numbering)."""

    def test_ccm_sup_signal(self) -> None:
        """SAE J1979 CCM_SUP at bix=13, len=1 (from OBDb test case)."""
        # Clean payload [0x00, 0x04], bix=13 → byte 1, Motorola bit 5
        # Motorola bit 5 of 0x04 = (0x04 >> (7-5)) & 1 = (0x04 >> 2) & 1 = 1
        result = evaluate_fmt([0x00, 0x04], {"bix": 13, "len": 1})
        assert result == 1.0

    def test_mil_signal(self) -> None:
        """SAE J1979 MIL at bix=0, len=1 (MSB of byte 0)."""
        # 0x80 = 0b10000000, Motorola bit 0 (MSB) = 1
        assert evaluate_fmt([0x80], {"bix": 0, "len": 1}) == 1.0
        # 0x00 = MIL off
        assert evaluate_fmt([0x00], {"bix": 0, "len": 1}) == 0.0

    def test_sub_byte_field(self) -> None:
        """A 5-bit field at bix=3 (bits 3-7 of byte 0)."""
        # 0xF8 = 0b11111000, bix=3, len=5, shift=8-3-5=0, raw = 0xF8 & 0x1F = 24
        result = evaluate_fmt([0xF8], {"bix": 3, "len": 5})
        assert result == 24.0

    def test_cross_byte_field(self) -> None:
        """A 13-bit field at bix=11 (spans bytes 1-2)."""
        # bix=11, len=13 → start_byte=1, end_byte=2
        # target_bytes=[0x07, 0xFF], chunk=0x07FF=2047
        # local_offset=11%8=3, shift=16-3-13=0, raw=2047&0x1FFF=2047
        result = evaluate_fmt([0x00, 0x07, 0xFF], {"bix": 11, "len": 13})
        assert result == 2047.0


class TestEvaluateFmtSigned:
    """Signed (two's complement) interpretation."""

    def test_signed_byte_negative(self) -> None:
        """0xFF as signed byte = -1."""
        result = evaluate_fmt([0xFF], {"bix": 0, "len": 8, "sign": True})
        assert result == -1.0

    def test_signed_byte_positive(self) -> None:
        """0x7F as signed byte = 127."""
        result = evaluate_fmt([0x7F], {"bix": 0, "len": 8, "sign": True})
        assert result == 127.0

    def test_signed_16bit_negative(self) -> None:
        """0xFFFF as signed 16-bit = -1."""
        result = evaluate_fmt([0xFF, 0xFF], {"bix": 0, "len": 16, "sign": True})
        assert result == -1.0

    def test_signed_with_offset(self) -> None:
        """Signed value with additive offset (e-Golf current: div=4, add=-511)."""
        # raw=0x07EB=2027, signed (len=16, MSB not set) = 2027
        # 2027/4 + (-511) = 506.75 - 511 = -4.25
        result = evaluate_fmt(
            [0x07, 0xEB],
            {
                "bix": 0,
                "len": 16,
                "div": 4,
                "add": -511,
                "sign": True,
            },
        )
        assert pytest.approx(result) == -4.25


class TestEvaluateFmtBlsb:
    """Byte-swap (Intel byte order) via blsb flag."""

    def test_blsb_swaps_bytes(self) -> None:
        """blsb=true reverses bytes before extraction."""
        # Without blsb: [0x12, 0x34] → 0x1234 = 4660
        result = evaluate_fmt([0x12, 0x34], {"bix": 0, "len": 16})
        assert result == 4660.0
        # With blsb: bytes reversed → [0x34, 0x12] → 0x3412 = 13330
        result = evaluate_fmt([0x12, 0x34], {"bix": 0, "len": 16, "blsb": True})
        assert result == 13330.0

    def test_blsb_4byte(self) -> None:
        """Blsb on a 32-bit field reverses all 4 bytes."""
        # [0x01, 0x02, 0x03, 0x04] → reversed [0x04, 0x03, 0x02, 0x01] = 0x04030201
        result = evaluate_fmt(
            [0x01, 0x02, 0x03, 0x04],
            {
                "bix": 0,
                "len": 32,
                "blsb": True,
            },
        )
        assert result == 0x04030201


class TestEvaluateFmtEnumeration:
    """Enumeration (map) support."""

    def test_map_returns_string(self) -> None:
        """A fmt with map returns the mapped string."""
        # 0x80 = Motorola bit 0 set → raw=1 → "On"
        result = evaluate_fmt(
            [0x80],
            {
                "bix": 0,
                "len": 1,
                "map": {"0": "Off", "1": "On"},
            },
        )
        assert result == "On"

    def test_map_missing_key_returns_none(self) -> None:
        """A raw value not in the map returns None."""
        result = evaluate_fmt(
            [0x80],
            {
                "bix": 0,
                "len": 1,
                "map": {"0": "Off"},
            },
        )
        assert result is None

    def test_map_ignores_scaling(self) -> None:
        """When map is present, mul/div/add are ignored."""
        result = evaluate_fmt(
            [0x80],
            {
                "bix": 0,
                "len": 1,
                "map": {"1": "On"},
                "mul": 100,
                "div": 2,
            },
        )
        assert result == "On"


class TestEvaluateFmtNullSentinels:
    """nullmin/nullmax sentinel support."""

    def test_nullmin_excludes(self) -> None:
        """Raw < nullmin returns None."""
        result = evaluate_fmt(
            [0x05],
            {
                "bix": 0,
                "len": 8,
                "nullmin": 10,
            },
        )
        assert result is None

    def test_nullmax_excludes(self) -> None:
        """Raw > nullmax returns None."""
        result = evaluate_fmt(
            [0xFF],
            {
                "bix": 0,
                "len": 8,
                "nullmax": 200,
            },
        )
        assert result is None

    def test_nullmin_boundary_inclusive(self) -> None:
        """Raw == nullmin is NOT excluded (only strictly less than)."""
        result = evaluate_fmt(
            [0x0A],
            {
                "bix": 0,
                "len": 8,
                "nullmin": 10,
            },
        )
        assert result == 10.0


class TestEvaluateFmtEdgeCases:
    """Edge cases and error handling."""

    def test_out_of_bounds(self) -> None:
        """Bit field beyond payload returns None."""
        result = evaluate_fmt([0x01], {"bix": 8, "len": 8})
        assert result is None

    def test_empty_payload(self) -> None:
        """Empty payload returns None."""
        result = evaluate_fmt([], {"bix": 0, "len": 8})
        assert result is None

    def test_zero_division_returns_none(self) -> None:
        """Zero div in fmt (shouldn't happen after validation) returns None."""
        # Bypass validation by calling evaluate_fmt directly
        result = evaluate_fmt([0x01], {"bix": 0, "len": 8, "div": 0})
        assert result is None


class TestMakeFmtEvaluator:
    """The closure factory."""

    def test_returns_callable(self) -> None:
        """make_fmt_evaluator returns a callable."""
        evaluator = make_fmt_evaluator({"bix": 0, "len": 8, "div": 2})
        assert callable(evaluator)

    def test_validates_on_creation(self) -> None:
        """Invalid fmt raises on creation."""
        with pytest.raises(FmtValidationError):
            make_fmt_evaluator({"bix": -1, "len": 8})

    def test_closure_evaluates(self) -> None:
        """The closure evaluates payloads."""
        evaluator = make_fmt_evaluator({"bix": 0, "len": 16, "div": 4})
        result = evaluator([0x05, 0x31])
        assert pytest.approx(result) == 332.25

    def test_closure_returns_none_on_error(self) -> None:
        """The closure returns None on runtime errors."""
        evaluator = make_fmt_evaluator({"bix": 0, "len": 8})
        result = evaluator([])
        assert result is None

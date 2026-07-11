"""Tests for :mod:`elm327_obdii.forms`.

Covers the config-flow form helpers including the structured ``fmt``
editor and the shorthand string parser.
"""

import pytest

from elm327_obdii._core.schema import CustomPid
from elm327_obdii.forms import (
    empty_form_defaults,
    fmt_to_string,
    form_input_to_fmt,
    form_input_to_fmt_from_hybrid,
    parse_formula_string,
    pid_to_form_defaults,
    user_input_to_form_defaults,
)


class TestParseFormulaString:
    """Shorthand string → fmt dict parser."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("B(0)", {"bix": 0, "len": 8}),
            ("B(4)", {"bix": 32, "len": 8}),
            ("B(4)/2.55", {"bix": 32, "len": 8, "div": 2.55}),
            ("B(4)*1.8", {"bix": 32, "len": 8, "mul": 1.8}),
            ("S(3)", {"bix": 24, "len": 8, "sign": True}),
            ("B(4, 5)", {"bix": 32, "len": 16}),
            ("S(0, 1)", {"bix": 0, "len": 16, "sign": True}),
            ("B(4, 5)/4", {"bix": 32, "len": 16, "div": 4.0}),
            ("BIT(2, 0)", {"bix": 16, "len": 1}),
            ("BIT(2, 3)", {"bix": 19, "len": 1}),
        ],
        ids=[
            "single-byte",
            "single-byte-offset",
            "divide",
            "multiply",
            "signed",
            "two-byte",
            "signed-two-byte",
            "two-byte-divide",
            "bit",
            "bit-offset",
        ],
    )
    def test_valid_patterns(self, source: str, expected: dict) -> None:
        """Valid shorthand patterns parse to the expected fmt dict."""
        assert parse_formula_string(source) == expected

    def test_empty_returns_none(self) -> None:
        """An empty string returns None."""
        assert parse_formula_string("") is None

    def test_complex_returns_none(self) -> None:
        """Multi-field formulas return None (no string fallback)."""
        assert parse_formula_string("B(0) + B(1)") is None
        assert parse_formula_string("B(0) ** 2") is None
        assert parse_formula_string("(B(0)-40)*1.8+32") is None

    def test_whitespace_normalized(self) -> None:
        """Whitespace is stripped before matching."""
        assert parse_formula_string("  B(4) / 2.55  ") == {
            "bix": 32,
            "len": 8,
            "div": 2.55,
        }


class TestFormInputToFmt:
    """Config-flow form input → fmt dict."""

    def test_minimal(self) -> None:
        """Minimal form input produces a minimal fmt."""
        fmt = form_input_to_fmt({"bix": 0, "len": 8})
        assert fmt == {"bix": 0, "len": 8}

    def test_with_scaling(self) -> None:
        """Scaling fields are included when non-default."""
        fmt = form_input_to_fmt({"bix": 0, "len": 16, "mul": 1.8, "div": 4, "add": -40})
        assert fmt == {"bix": 0, "len": 16, "mul": 1.8, "div": 4.0, "add": -40.0}

    def test_default_scaling_omitted(self) -> None:
        """Default mul=1, div=1, add=0 are omitted."""
        fmt = form_input_to_fmt({"bix": 0, "len": 8, "mul": 1, "div": 1, "add": 0})
        assert fmt == {"bix": 0, "len": 8}

    def test_sign_and_blsb(self) -> None:
        """Sign and blsb are included when True."""
        fmt = form_input_to_fmt({"bix": 0, "len": 16, "sign": True, "blsb": True})
        assert fmt == {"bix": 0, "len": 16, "sign": True, "blsb": True}

    def test_min_max(self) -> None:
        """min/max are included when provided."""
        fmt = form_input_to_fmt({"bix": 0, "len": 8, "min": -200, "max": 200})
        assert fmt == {"bix": 0, "len": 8, "min": -200.0, "max": 200.0}

    def test_map_text_parsed(self) -> None:
        """map_text is parsed to a map dict."""
        fmt = form_input_to_fmt(
            {
                "bix": 0,
                "len": 1,
                "map_text": "0=Off\n1=On",
            }
        )
        assert fmt["map"] == {"0": "Off", "1": "On"}

    def test_invalid_bix_raises(self) -> None:
        """Negative bix raises ValueError."""
        with pytest.raises(ValueError, match="bix"):
            form_input_to_fmt({"bix": -1, "len": 8})

    def test_invalid_len_raises(self) -> None:
        """Zero len raises ValueError."""
        with pytest.raises(ValueError, match="len"):
            form_input_to_fmt({"bix": 0, "len": 0})

    def test_zero_div_raises(self) -> None:
        """Zero div raises ValueError."""
        with pytest.raises(ValueError, match="div"):
            form_input_to_fmt({"bix": 0, "len": 8, "div": 0})


class TestFormDefaults:
    """Form default helpers."""

    def test_empty_form_defaults(self) -> None:
        """empty_form_defaults returns all expected keys."""
        defaults = empty_form_defaults()
        assert "bix" in defaults
        assert "len" in defaults
        assert "mul" in defaults
        assert "div" in defaults
        assert defaults["bix"] == 0
        assert defaults["len"] == 8

    def test_pid_to_form_defaults(self) -> None:
        """pid_to_form_defaults extracts fmt fields from a CustomPid."""
        pid = CustomPid(
            id="test",
            name="Test",
            mode="22",
            query="028C",
            fmt={"bix": 32, "len": 16, "div": 4, "sign": True},
        )
        defaults = pid_to_form_defaults(pid)
        assert defaults["bix"] == 32
        assert defaults["len"] == 16
        assert defaults["div"] == 4
        assert defaults["sign"] is True

    def test_user_input_to_form_defaults(self) -> None:
        """user_input_to_form_defaults normalizes input types."""
        defaults = user_input_to_form_defaults(
            {
                "bix": "32",
                "len": "16",
                "mul": "1.8",
            }
        )
        assert defaults["bix"] == 32
        assert defaults["len"] == 16
        assert defaults["mul"] == 1.8


class TestFmtToString:
    """fmt → formula string rendering for display."""

    @pytest.mark.parametrize(
        ("fmt", "expected"),
        [
            ({"bix": 0, "len": 8}, "B(0)"),
            ({"bix": 32, "len": 8}, "B(4)"),
            ({"bix": 32, "len": 8, "div": 2.55}, "B(4)/2.55"),
            ({"bix": 0, "len": 8, "mul": 1.8}, "B(0)*1.8"),
            ({"bix": 24, "len": 8, "sign": True}, "S(3)"),
            ({"bix": 0, "len": 16}, "B(0, 1)"),
            ({"bix": 0, "len": 16, "div": 4}, "B(0, 1)/4"),
            ({"bix": 32, "len": 16, "sign": True}, "S(4, 5)"),
        ],
        ids=[
            "single-byte",
            "offset-byte",
            "divide",
            "multiply",
            "signed",
            "two-byte",
            "two-byte-divide",
            "signed-two-byte",
        ],
    )
    def test_expressible(self, fmt: dict, expected: str) -> None:
        """Expressible fmts render back to the shorthand string."""
        assert fmt_to_string(fmt) == expected

    @pytest.mark.parametrize(
        "fmt",
        [
            {"bix": 3, "len": 5},  # sub-byte
            {"bix": 11, "len": 13},  # cross-byte non-aligned
            {"bix": 0, "len": 8, "map": {"0": "Off"}},  # enum
            {"bix": 0, "len": 16, "blsb": True},  # byte-swap
            {"bix": 0, "len": 8, "mul": 1.8, "add": -40},  # complex scaling
            {"bix": 0, "len": 8, "nullmin": 10},  # null sentinel
        ],
        ids=["sub-byte", "cross-byte", "enum", "blsb", "complex-scaling", "nullmin"],
    )
    def test_not_expressible(self, fmt: dict) -> None:
        """Non-string-expressible fmts return empty string."""
        assert fmt_to_string(fmt) == ""

    def test_empty_fmt(self) -> None:
        """An empty fmt renders to an empty string."""
        assert fmt_to_string({}) == ""


class TestFormInputToFmtFromHybrid:
    """The hybrid form: string field + structured fields."""

    def test_string_takes_priority(self) -> None:
        """When formula string parses, it's used (structured ignored)."""
        fmt = form_input_to_fmt_from_hybrid(
            {
                "formula": "B(4)/2.55",
                "bix": 0,
                "len": 8,
            }
        )
        assert fmt == {"bix": 32, "len": 8, "div": 2.55}

    def test_structured_fallback(self) -> None:
        """When formula string is empty, structured fields are used."""
        fmt = form_input_to_fmt_from_hybrid(
            {
                "formula": "",
                "bix": 32,
                "len": 16,
                "div": 4,
            }
        )
        assert fmt == {"bix": 32, "len": 16, "div": 4.0}

    def test_unparseable_string_falls_back(self) -> None:
        """When formula string doesn't parse, structured fields are used."""
        fmt = form_input_to_fmt_from_hybrid(
            {
                "formula": "B(0) + B(1)",
                "bix": 0,
                "len": 8,
            }
        )
        assert fmt == {"bix": 0, "len": 8}

    def test_string_with_min_max_merged(self) -> None:
        """min/max from structured fields merge into parsed fmt."""
        fmt = form_input_to_fmt_from_hybrid(
            {
                "formula": "B(4)/2.55",
                "min": 0,
                "max": 100,
            }
        )
        assert fmt is not None
        assert fmt["min"] == 0.0
        assert fmt["max"] == 100.0

    def test_both_empty_returns_none(self) -> None:
        """No string and no structured fields returns None."""
        fmt = form_input_to_fmt_from_hybrid({"formula": ""})
        # bix defaults to 0, len defaults to 8 — so this produces a fmt
        assert fmt == {"bix": 0, "len": 8}

    def test_invalid_structured_returns_none(self) -> None:
        """Invalid structured input returns None."""
        fmt = form_input_to_fmt_from_hybrid(
            {
                "formula": "",
                "bix": -1,
                "len": 8,
            }
        )
        assert fmt is None

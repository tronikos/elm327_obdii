"""Tests for :mod:`elm327_obdii.profiles.wican`.

Exercises the WiCAN JSON → :class:`ProfileConfig` translation with the
new ``fmt`` dict output. Formula strings are parsed to ``fmt`` dicts
on import; non-expressible formulas are skipped with a warning.
"""

import pytest

from elm327_obdii import import_wican_profile
from elm327_obdii._core.schema import ProfileConfig


class TestImportWicanProfileBasic:
    """Round-trip translation of a representative WiCAN profile."""

    def test_minimal_profile(self) -> None:
        """A profile with one custom PID translates correctly."""
        raw = {
            "car_model": "Test: Minimal",
            "init": "ATSP6;ATST96;",
            "pids": [
                {
                    "pid": "22028C1",
                    "pid_init": "ATSH7E5;ATCRA7ED;",
                    "parameters": [
                        {
                            "name": "SOC BMS",
                            "expression": "B4/2.5",
                            "unit": "%",
                            "class": "battery",
                        },
                    ],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert isinstance(profile, ProfileConfig)
        assert profile.standard_pids == []
        assert len(profile.custom_pids) == 1
        pid = profile.custom_pids[0]
        assert pid.name == "SOC BMS"
        assert pid.mode == "22"
        assert pid.query == "028C1"
        assert pid.can_header == "7E5"
        assert pid.can_filter == "7ED"
        assert pid.unit == "%"
        assert pid.device_class == "battery"
        # B4/2.5 in Mode 22: dirty[4] → clean[0], bix=0, len=8, div=2.5
        assert pid.fmt == {"bix": 0, "len": 8, "div": 2.5}
        assert pid.source == "import:wican:Test: Minimal"

    def test_empty_profile(self) -> None:
        """An empty profile produces empty lists."""
        raw = {"car_model": "Empty", "pids": []}
        profile = import_wican_profile(raw)
        assert profile.standard_pids == []
        assert profile.custom_pids == []

    def test_non_dict_raises_type_error(self) -> None:
        """A non-dict input raises TypeError."""
        with pytest.raises(TypeError, match="dict"):
            import_wican_profile(["not", "a", "dict"])  # type: ignore[arg-type]


class TestFormulaTranslation:
    """WiCAN string expression → fmt dict translation."""

    @pytest.mark.parametrize(
        ("wican_expr", "mode", "expected_fmt"),
        [
            # Single byte: B4/2.5 in Mode 22 → bix=0, len=8, div=2.5
            ("B4/2.5", "22", {"bix": 0, "len": 8, "div": 2.5}),
            # Single byte no scaling: B4 → bix=0, len=8
            ("B4", "22", {"bix": 0, "len": 8}),
            # Signed byte: S3 → bix=0, len=8, sign=True (Mode 22: 3-4=-1→0)
            ("S3", "22", {"bix": 0, "len": 8, "sign": True}),
            # Multi-byte slice: [B5:B6]/100 → bix=8, len=16, div=100
            ("[B5:B6]/100", "22", {"bix": 8, "len": 16, "div": 100}),
            # Signed slice: [S5:S6] → bix=8, len=16, sign=True
            ("[S5:S6]", "22", {"bix": 8, "len": 16, "sign": True}),
            # Bit extraction: B2:0 → bix=0, len=1 (Mode 22: (2-4)*8+0 = -16→0)
            ("B2:0", "22", {"bix": 0, "len": 1}),
            # Manual 2-byte word: (B4*256)+B5 → bix=0, len=16
            ("(B4*256)+B5", "22", {"bix": 0, "len": 16}),
            # Manual 2-byte with shift: (B4<<8)+B5
            ("(B4<<8)+B5", "22", {"bix": 0, "len": 16}),
            # Manual 3-byte: (B4*65536)+(B5*256)+B6
            ("(B4*65536)+(B5*256)+B6", "22", {"bix": 0, "len": 24}),
            # Manual 4-byte: (B4<<24)+(B5<<16)+(B6<<8)+B7
            ("(B4<<24)+(B5<<16)+(B6<<8)+B7", "22", {"bix": 0, "len": 32}),
            # With multiply: B4*1.8
            ("B4*1.8", "22", {"bix": 0, "len": 8, "mul": 1.8}),
            # With add: B4-40
            ("B4-40", "22", {"bix": 0, "len": 8, "add": -40.0}),
            # Mode 01: B0 → bix=0 (dirty[0]-3=-3→0, but clamped)
            ("B0", "01", {"bix": 0, "len": 8}),
            # Mode 01: B3 → bix=0 (dirty[3]-3=0)
            ("B3", "01", {"bix": 0, "len": 8}),
            # Mode 01: B4 → bix=8 (dirty[4]-3=1, 1*8=8)
            ("B4", "01", {"bix": 8, "len": 8}),
        ],
        ids=[
            "single-byte-div",
            "single-byte",
            "signed-byte",
            "slice-div",
            "signed-slice",
            "bit-extract",
            "manual-word2",
            "manual-word2-shift",
            "manual-word3",
            "manual-word4",
            "multiply",
            "add-offset",
            "mode01-b0",
            "mode01-b3",
            "mode01-b4",
        ],
    )
    def test_formula_to_fmt(
        self, wican_expr: str, mode: str, expected_fmt: dict
    ) -> None:
        """Each WiCAN expression translates to the expected fmt dict."""
        raw = {
            "car_model": "Test",
            "pids": [
                {
                    "pid": mode + "FFFF",
                    "parameters": [{"name": "P", "expression": wican_expr}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert len(profile.custom_pids) == 1
        assert profile.custom_pids[0].fmt == expected_fmt

    def test_non_expressible_formula_skipped(self) -> None:
        """A multi-field formula (B4+B5) is skipped."""
        raw = {
            "car_model": "Test",
            "pids": [
                {
                    "pid": "22FFFF",
                    "parameters": [{"name": "P", "expression": "B4+B5"}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert profile.custom_pids == []

    def test_non_contiguous_manual_word_skipped(self) -> None:
        """A manual word with a gap (B6,B7,B9 — skips B8) is skipped."""
        raw = {
            "car_model": "Test",
            "pids": [
                {
                    "pid": "22FFFF",
                    "parameters": [
                        {"name": "P", "expression": "(B6*65536)+(B7*256)+B9"}
                    ],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert profile.custom_pids == []

    def test_empty_expression_skipped(self) -> None:
        """An empty expression produces no custom PID."""
        raw = {
            "car_model": "Test",
            "pids": [
                {"pid": "22FFFF", "parameters": [{"name": "P", "expression": ""}]},
            ],
        }
        profile = import_wican_profile(raw)
        assert profile.custom_pids == []


class TestParsePidInit:
    """ATSH/ATCRA parsing and ATH0/ATS0 filtering."""

    def test_sh_and_cra_extracted(self) -> None:
        """ATSH and ATCRA are extracted as header and filter."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "22FFFF",
                    "pid_init": "ATSH7E5;ATCRA7ED;",
                    "parameters": [{"name": "P", "expression": "B0"}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        pid = profile.custom_pids[0]
        assert pid.can_header == "7E5"
        assert pid.can_filter == "7ED"
        assert pid.init_extra is None

    def test_extra_init_preserved(self) -> None:
        """Non-ATSH/ATCRA commands go into init_extra."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "22FFFF",
                    "pid_init": "ATSH7E5;ATST64;ATFCSM0;",
                    "parameters": [{"name": "P", "expression": "B0"}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        pid = profile.custom_pids[0]
        assert pid.init_extra is not None
        assert "ATST64" in pid.init_extra or "ATST 64" in pid.init_extra
        assert "ATFCSM0" in pid.init_extra or "ATFCSM 0" in pid.init_extra

    def test_ath0_filtered(self) -> None:
        """AT H0 is filtered from pid_init."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "22FFFF",
                    "pid_init": "ATSH7E5;AT H0;",
                    "parameters": [{"name": "P", "expression": "B0"}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        pid = profile.custom_pids[0]
        assert pid.can_header == "7E5"
        if pid.init_extra:
            assert "H0" not in pid.init_extra.replace(" ", "").upper()

    def test_header_uppercased(self) -> None:
        """SH/CRA arguments are uppercased."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "22FFFF",
                    "pid_init": "ATSH7e5;ATCRA7ed;",
                    "parameters": [{"name": "P", "expression": "B0"}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        pid = profile.custom_pids[0]
        assert pid.can_header == "7E5"
        assert pid.can_filter == "7ED"


class TestReverseDeDup:
    """Mode 01 PIDs are promoted to standard_pids."""

    def test_mode01_pid_promoted(self) -> None:
        """A Mode 01 PID with a known name is promoted to standard."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "010C",  # ENGINE_SPEED
                    "parameters": [{"name": "RPM", "expression": "B0"}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert "ENGINE_SPEED" in profile.standard_pids
        assert profile.custom_pids == []

    def test_unknown_mode01_pid_stays_custom(self) -> None:
        """An unknown Mode 01 PID stays in custom_pids."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "01FF",
                    "parameters": [{"name": "Unknown", "expression": "B0"}],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert profile.standard_pids == []
        assert len(profile.custom_pids) == 1


class TestParametersShape:
    """The parameters field can be a list or a Torque-style dict."""

    def test_list_of_dicts(self) -> None:
        """Modern list-of-dicts form."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "22FFFF",
                    "parameters": [
                        {"name": "A", "expression": "B0", "unit": "%"},
                        {"name": "B", "expression": "B1", "unit": "V"},
                    ],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert len(profile.custom_pids) == 2

    def test_duplicate_param_names_get_unique_ids(self) -> None:
        """Two parameters with the same name must not collide on pid_id."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "22FFFF",
                    "parameters": [
                        {"name": "SOC", "expression": "B0"},
                        {"name": "SOC", "expression": "B1"},
                    ],
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert len(profile.custom_pids) == 2
        ids = {p.id for p in profile.custom_pids}
        assert len(ids) == 2

    def test_torque_style_dict(self) -> None:
        """Torque-style {name: expression} shorthand."""
        raw = {
            "car_model": "T",
            "pids": [
                {
                    "pid": "22FFFF",
                    "parameters": {"SOC": "B0/2.55", "Temp": "B1-40"},
                }
            ],
        }
        profile = import_wican_profile(raw)
        assert len(profile.custom_pids) == 2
        names = {p.name for p in profile.custom_pids}
        assert names == {"SOC", "Temp"}

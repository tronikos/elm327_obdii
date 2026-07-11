"""Tests for :mod:`elm327_obdii.profiles.obdb`.

Exercises the OBDb signal definition → :class:`ProfileConfig` translation
with the structured ``fmt`` dict. OBDb's ``fmt`` maps directly to our
internal representation with no translation loss.
"""

import pytest

from elm327_obdii import import_obdb_profile
from elm327_obdii._core.schema import ProfileConfig


def _egolf_voltage_signal() -> dict:
    """A real OBDb signal from the e-Golf (HV battery voltage)."""
    return {
        "bitLength": 16,
        "bitOffset": 0,
        "cmd": {"22": "1E3B"},
        "debug": False,
        "eax": "",
        "fmt": {"div": 4, "len": 16, "max": 1000, "unit": "volts"},
        "hdr": "7E5",
        "id": "EGOLF_HVBAT_VOLTS",
        "make": "Volkswagen",
        "model": "e-Golf",
        "name": "HV battery voltage",
        "path": "Battery",
        "pid": "22",
        "unit": "volts",
    }


def _enum_signal() -> dict:
    """An OBDb enumeration signal (hybrid/EV charging state)."""
    return {
        "bitLength": 1,
        "bitOffset": 0,
        "cmd": {"01": "9A"},
        "fmt": {
            "len": 1,
            "map": {
                "0": {"description": "Charge sustaining", "value": "CSM"},
                "1": {"description": "Charge depleting", "value": "CDM"},
            },
        },
        "hdr": "7E0",
        "id": "HEV_MODE",
        "make": "SAEJ1979",
        "model": "",
        "name": "Hybrid/EV charging state",
        "path": "Battery.Generic",
        "pid": "01",
        "unit": "",
    }


class TestImportObdbProfileBasic:
    """Basic translation of OBDb signals to ProfileConfig."""

    def test_single_signal(self) -> None:
        """One signal produces one custom PID."""
        profile = import_obdb_profile([_egolf_voltage_signal()])
        assert isinstance(profile, ProfileConfig)
        assert profile.standard_pids == []
        assert len(profile.custom_pids) == 1
        pid = profile.custom_pids[0]
        assert pid.id == "EGOLF_HVBAT_VOLTS"
        assert pid.name == "HV battery voltage"
        assert pid.mode == "22"
        assert pid.query == "1E3B"
        assert pid.can_header == "7E5"
        assert pid.unit == "V"  # translated from "volts"
        assert pid.fmt["bix"] == 0
        assert pid.fmt["len"] == 16
        assert pid.fmt["div"] == 4
        assert "max" in pid.fmt
        assert pid.source == "import:obdb:Volkswagen:e-Golf"

    def test_empty_signals(self) -> None:
        """An empty signal list produces an empty profile."""
        profile = import_obdb_profile([])
        assert profile.standard_pids == []
        assert profile.custom_pids == []

    def test_malformed_signal_skipped(self) -> None:
        """A malformed signal is skipped, not fatal."""
        profile = import_obdb_profile(
            [
                {"id": "bad", "fmt": "not a dict"},  # malformed
                _egolf_voltage_signal(),
            ]
        )
        assert len(profile.custom_pids) == 1
        assert profile.custom_pids[0].id == "EGOLF_HVBAT_VOLTS"


class TestFmtExtraction:
    """The fmt dict is extracted and normalized correctly."""

    def test_fmt_fields_preserved(self) -> None:
        """All fmt fields are preserved."""
        signal = {
            "cmd": {"22": "1E3D"},
            "fmt": {
                "bix": 16,
                "len": 16,
                "div": 4,
                "add": -511,
                "sign": True,
                "min": -200,
                "max": 200,
            },
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": "amps",
        }
        profile = import_obdb_profile([signal])
        fmt = profile.custom_pids[0].fmt
        assert fmt["bix"] == 16
        assert fmt["len"] == 16
        assert fmt["div"] == 4
        assert fmt["add"] == -511
        assert fmt["sign"] is True
        assert fmt["min"] == -200
        assert fmt["max"] == 200

    def test_blsb_preserved(self) -> None:
        """The blsb flag is preserved."""
        signal = {
            "cmd": {"22": "1009"},
            "fmt": {"blsb": True, "len": 16, "sign": True, "max": 7000},
            "hdr": "7E1",
            "id": "TEST",
            "make": "Audi",
            "model": "Q3",
            "name": "Test",
            "unit": "rpm",
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].fmt["blsb"] is True

    def test_bitoffset_fallback(self) -> None:
        """BitOffset is used as fallback when bix is absent."""
        signal = {
            "bitOffset": 32,
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 16, "div": 4},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": "volts",
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].fmt["bix"] == 32

    def test_enum_map_normalized(self) -> None:
        """The map dict is normalized to {str: str}."""
        profile = import_obdb_profile([_enum_signal()])
        fmt = profile.custom_pids[0].fmt
        assert "map" in fmt
        assert fmt["map"]["0"] == "CSM"
        assert fmt["map"]["1"] == "CDM"


class TestUnitTranslation:
    """OBDb long unit names → HA abbreviations."""

    @pytest.mark.parametrize(
        ("obdb_unit", "expected"),
        [
            ("volts", "V"),
            ("amps", "A"),
            ("kilowattHours", "kWh"),
            ("ampereHours", "Ah"),
            ("kilometers", "km"),
            ("miles", "mi"),
            ("celsius", "°C"),
            ("fahrenheit", "°F"),
            ("percent", "%"),
            ("kilometersPerHour", "km/h"),
            ("milesPerHour", "mph"),
            ("kilopascal", "kPa"),
            ("psi", "psi"),
            ("revolutionsPerMinute", "rpm"),
            ("seconds", "s"),
            ("minutes", "min"),
            ("hours", "h"),
        ],
    )
    def test_unit_translation(self, obdb_unit: str, expected: str) -> None:
        """Each OBDb unit name maps to the expected HA abbreviation."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8, "unit": obdb_unit},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": obdb_unit,
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].unit == expected

    def test_unknown_unit_passthrough(self) -> None:
        """Unknown unit names pass through unchanged."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8, "unit": "frobnicates"},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": "frobnicates",
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].unit == "frobnicates"

    def test_none_unit(self) -> None:
        """A None unit stays None."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": None,
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].unit is None


class TestDeviceClassMapping:
    """suggestedMetric → device_class mapping."""

    @pytest.mark.parametrize(
        ("suggested", "expected_dc"),
        [
            ("stateOfCharge", "battery"),
            ("odometer", "distance"),
            ("electricRange", "distance"),
            ("vehicleSpeed", "speed"),
            ("batteryVoltage", "voltage"),
            ("batteryCurrent", "current"),
            ("batteryTemperature", "temperature"),
            ("power", "power"),
            ("energy", "energy"),
        ],
    )
    def test_suggested_metric_mapping(self, suggested: str, expected_dc: str) -> None:
        """SuggestedMetric maps to the expected device_class."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "suggestedMetric": suggested,
            "unit": "volts",
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].device_class == expected_dc

    def test_enum_has_no_device_class(self) -> None:
        """Enumeration signals have no device_class."""
        profile = import_obdb_profile([_enum_signal()])
        assert profile.custom_pids[0].device_class is None

    def test_unit_fallback_for_device_class(self) -> None:
        """Without suggestedMetric, unit is used for device_class."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": "volts",
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].device_class == "voltage"


class TestYearFiltering:
    """Model-year filtering via modelYears and dbgfilter."""

    def test_no_year_filter_imports_all(self) -> None:
        """Without selected_year, all signals are imported."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "modelYears": [2017, 2017],
        }
        profile = import_obdb_profile([signal])
        assert len(profile.custom_pids) == 1

    def test_year_match_imports(self) -> None:
        """A matching year imports the signal."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "modelYears": [2017, 2020],
        }
        profile = import_obdb_profile([signal], selected_year=2019)
        assert len(profile.custom_pids) == 1

    def test_year_mismatch_skips(self) -> None:
        """A non-matching year skips the signal."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "modelYears": [2017, 2017],
        }
        profile = import_obdb_profile([signal], selected_year=2019)
        assert len(profile.custom_pids) == 0

    def test_model_years_stored(self) -> None:
        """ModelYears are stored on the CustomPid."""
        signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 8},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "modelYears": [2017, 2020],
        }
        profile = import_obdb_profile([signal])
        assert profile.custom_pids[0].model_years == [2017, 2020]


class TestRepoIntegration:
    """Per-vehicle repo default.json provides rax and dbgfilter."""

    def test_rax_from_repo(self) -> None:
        """Rax from the repo becomes can_filter."""
        matrix_signal = {
            "cmd": {"22": "1E3B"},
            "fmt": {"len": 16, "div": 4},
            "hdr": "7E5",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": "volts",
        }
        repo_default = {
            "commands": [
                {
                    "hdr": "7E5",
                    "rax": "7ED",
                    "cmd": {"22": "1E3B"},
                    "signals": [{"id": "TEST", "fmt": {"len": 16, "div": 4}}],
                }
            ],
        }
        profile = import_obdb_profile([matrix_signal], repo_default=repo_default)
        assert profile.custom_pids[0].can_filter == "7ED"

    def test_no_repo_no_filter(self) -> None:
        """Without repo_default, can_filter is None."""
        profile = import_obdb_profile([_egolf_voltage_signal()])
        assert profile.custom_pids[0].can_filter is None

    def test_fcm1_becomes_init_extra(self) -> None:
        """fcm1=true from repo adds ATFCSM1 to init_extra."""
        matrix_signal = {
            "cmd": {"22": "2AB2"},
            "fmt": {"len": 32, "div": 1310770},
            "hdr": "710",
            "id": "TEST",
            "make": "VW",
            "model": "e-Golf",
            "name": "Test",
            "unit": "kilowattHours",
        }
        repo_default = {
            "commands": [
                {
                    "hdr": "710",
                    "rax": "77A",
                    "fcm1": True,
                    "cmd": {"22": "2AB2"},
                    "signals": [{"id": "TEST", "fmt": {"len": 32, "div": 1310770}}],
                }
            ],
        }
        profile = import_obdb_profile([matrix_signal], repo_default=repo_default)
        assert profile.custom_pids[0].init_extra is not None
        assert "ATFCSM1" in profile.custom_pids[0].init_extra

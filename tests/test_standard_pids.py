"""Tests for :mod:`elm327_obdii._core.standard_pids`.

Covers the heuristic entity-metadata mappers (icon, device_class,
state_class), the supported-PID bitmap scan, and the
RECOMMENDED_DEFAULTS validation against the live obdii registry.
"""

from unittest.mock import MagicMock

import pytest

from elm327_obdii import RECOMMENDED_DEFAULTS
from elm327_obdii._core.standard_pids import (
    get_list_of_units,
    get_standard_command,
    is_supported_pids_bitmap,
    propose_device_class,
    propose_icon,
    propose_state_class,
    scan_supported_pids,
)


class TestRecommendedDefaults:
    """RECOMMENDED_DEFAULTS must reference real obdii commands."""

    def test_all_defaults_exist_in_registry(self) -> None:
        """Every name in RECOMMENDED_DEFAULTS is a real Mode 01 command."""
        for name in RECOMMENDED_DEFAULTS:
            cmd = get_standard_command(name)
            assert cmd is not None, f"{name} not in obdii registry"

    def test_defaults_are_non_empty(self) -> None:
        """The defaults list is populated."""
        assert len(RECOMMENDED_DEFAULTS) >= 1

    def test_defaults_are_unique(self) -> None:
        """No duplicate entries."""
        assert len(RECOMMENDED_DEFAULTS) == len(set(RECOMMENDED_DEFAULTS))

    def test_defaults_include_fuel_level(self) -> None:
        """FUEL_LEVEL is the most useful PID for a parked car."""
        assert "FUEL_LEVEL" in RECOMMENDED_DEFAULTS

    def test_defaults_exclude_transient_engine_pids(self) -> None:
        """PIDs that read 0 when parked (rpm, speed, load) are not defaulted."""
        assert "ENGINE_SPEED" not in RECOMMENDED_DEFAULTS
        assert "VEHICLE_SPEED" not in RECOMMENDED_DEFAULTS
        assert "ENGINE_LOAD" not in RECOMMENDED_DEFAULTS
        assert "MAF_RATE" not in RECOMMENDED_DEFAULTS
        assert "ENGINE_RUN_TIME" not in RECOMMENDED_DEFAULTS


class TestGetStandardCommand:
    """Lookup of standard commands by canonical name."""

    def test_known_name(self) -> None:
        """A known name returns the command."""
        cmd = get_standard_command("ENGINE_SPEED")
        assert cmd is not None
        assert cmd.name == "ENGINE_SPEED"

    def test_unknown_name_returns_none(self) -> None:
        """An unknown name returns None, not an exception."""
        assert get_standard_command("NONEXISTENT_PID") is None


class TestIsSupportedPidsBitmap:
    """Bitmap PID detection (metadata, not user-trackable)."""

    @pytest.mark.parametrize(
        "name",
        [
            "SUPPORTED_PIDS_A",
            "SUPPORTED_PIDS_B",
            "SUPPORTED_PIDS_C",
            "SUPPORTED_PIDS_D",
            "SUPPORTED_PIDS_E",
            "SUPPORTED_PIDS_F",
            "SUPPORTED_PIDS_G",
        ],
    )
    def test_bitmap_names(self, name: str) -> None:
        """All SUPPORTED_PIDS_* names are recognized as bitmaps."""
        assert is_supported_pids_bitmap(name) is True

    def test_non_bitmap_name(self) -> None:
        """A regular PID name is not a bitmap."""
        assert is_supported_pids_bitmap("ENGINE_SPEED") is False


class TestProposeDeviceClass:
    """Heuristic device-class mapping (P7 fix: no power_factor for %)."""

    def test_temperature_pid(self) -> None:
        """Temperature PIDs map to 'temperature'."""
        cmd = get_standard_command("ENGINE_COOLANT_TEMP")
        assert cmd is not None
        assert propose_device_class(cmd) == "temperature"

    def test_voltage_pid(self) -> None:
        """Voltage PIDs map to 'voltage'."""
        cmd = get_standard_command("VEHICLE_VOLTAGE")
        assert cmd is not None
        assert propose_device_class(cmd) == "voltage"

    def test_speed_pid(self) -> None:
        """Speed PIDs map to 'speed'."""
        cmd = get_standard_command("VEHICLE_SPEED")
        assert cmd is not None
        assert propose_device_class(cmd) == "speed"

    def test_pressure_pid(self) -> None:
        """Pressure PIDs map to 'pressure'."""
        cmd = get_standard_command("FUEL_PRESSURE")
        assert cmd is not None
        assert propose_device_class(cmd) == "pressure"

    def test_fuel_level_maps_to_battery(self) -> None:
        """FUEL_LEVEL maps to 'battery' (0-100% range)."""
        cmd = get_standard_command("FUEL_LEVEL")
        assert cmd is not None
        assert propose_device_class(cmd) == "battery"

    def test_percentage_pid_not_power_factor(self) -> None:
        """P7 fix: percentage PIDs (ENGINE_LOAD) do NOT map to power_factor."""
        cmd = get_standard_command("ENGINE_LOAD")
        assert cmd is not None
        dc = propose_device_class(cmd)
        assert dc != "power_factor"

    def test_duration_pid(self) -> None:
        """ENGINE_RUN_TIME maps to 'duration'."""
        cmd = get_standard_command("ENGINE_RUN_TIME")
        assert cmd is not None
        assert propose_device_class(cmd) == "duration"


class TestProposeStateClass:
    """Heuristic state-class mapping."""

    def test_engine_speed_is_measurement(self) -> None:
        """ENGINE_SPEED is an instantaneous measurement."""
        cmd = get_standard_command("ENGINE_SPEED")
        assert cmd is not None
        assert propose_state_class(cmd) == "measurement"

    def test_run_time_is_total_increasing(self) -> None:
        """ENGINE_RUN_TIME is a monotonic counter."""
        cmd = get_standard_command("ENGINE_RUN_TIME")
        assert cmd is not None
        assert propose_state_class(cmd) == "total_increasing"

    def test_status_pid_returns_none(self) -> None:
        """Status/encoded PIDs return None (no state class)."""
        cmd = get_standard_command("STATUS_DTC")
        if cmd is not None:
            assert propose_state_class(cmd) is None


class TestProposeIcon:
    """Heuristic icon mapping."""

    def test_temperature_icon(self) -> None:
        """Temperature PIDs get a thermometer icon."""
        cmd = get_standard_command("ENGINE_COOLANT_TEMP")
        assert cmd is not None
        assert propose_icon(cmd) == "mdi:thermometer"

    def test_speed_icon(self) -> None:
        """Speed PIDs get a speedometer icon."""
        cmd = get_standard_command("VEHICLE_SPEED")
        assert cmd is not None
        assert propose_icon(cmd) == "mdi:speedometer"

    def test_engine_speed_icon(self) -> None:
        """ENGINE_SPEED contains 'SPEED' so it gets the speedometer icon."""
        cmd = get_standard_command("ENGINE_SPEED")
        assert cmd is not None
        assert propose_icon(cmd) == "mdi:speedometer"

    def test_voltage_icon(self) -> None:
        """Voltage PIDs get a flash icon."""
        cmd = get_standard_command("VEHICLE_VOLTAGE")
        assert cmd is not None
        assert propose_icon(cmd) == "mdi:flash"


class TestGetListOfUnits:
    """Normalize command.units to a list."""

    def test_string_unit(self) -> None:
        """A string unit becomes a single-element list."""
        cmd = get_standard_command("ENGINE_SPEED")
        assert cmd is not None
        units = get_list_of_units(cmd)
        assert isinstance(units, list)
        assert len(units) >= 1

    def test_none_unit(self) -> None:
        """A None unit returns an empty list."""
        cmd = MagicMock()
        cmd.units = None
        assert get_list_of_units(cmd) == []


class TestScanSupportedPids:
    """The Mode 01 PID bitmap scan.

    Uses a mock connection to simulate ECU responses. The scan walks
    PIDs 0x00, 0x20, 0x40, ... and decodes the 32-bit bitmap.
    """

    def test_scan_returns_supported_names(self) -> None:
        """A scan with a valid bitmap returns the supported command names."""

        def fake_query(cmd):
            resp = MagicMock()
            resp.value = None
            if cmd.name == "SUPPORTED_PIDS_A":
                # Bit 31 (PID 0x0C = ENGINE_SPEED) + bit 29 (PID 0x0D = VEHICLE_SPEED)
                resp.value = [0x0C, 0x0D]
            return resp

        conn = MagicMock()
        conn.query = fake_query
        result = scan_supported_pids(conn)
        assert "ENGINE_SPEED" in result
        assert "VEHICLE_SPEED" in result

    def test_scan_stops_on_empty_response(self) -> None:
        """An empty value stops the bitmap chain."""
        conn = MagicMock()
        resp = MagicMock()
        resp.value = None
        conn.query.return_value = resp
        result = scan_supported_pids(conn)
        assert result == []

    def test_scan_stops_on_no_next_block(self) -> None:
        """If the MSB (next-block indicator) is clear, the scan stops."""

        def fake_query(cmd):
            resp = MagicMock()
            if cmd.name == "SUPPORTED_PIDS_A":
                # Only PID 0x0C, no next-block bit (PID 0x20)
                resp.value = [0x0C]
            else:
                resp.value = None
            return resp

        conn = MagicMock()
        conn.query = fake_query
        result = scan_supported_pids(conn)
        assert "ENGINE_SPEED" in result

    def test_scan_deduplicates(self) -> None:
        """Duplicate PIDs across bitmap blocks are deduplicated."""

        def fake_query(cmd):
            resp = MagicMock()
            if cmd.name == "SUPPORTED_PIDS_A":
                resp.value = [0x0C, 0x20]  # ENGINE_SPEED + next-block
            elif cmd.name == "SUPPORTED_PIDS_B":
                resp.value = [0x0C]  # duplicate
            else:
                resp.value = None
            return resp

        conn = MagicMock()
        conn.query = fake_query
        result = scan_supported_pids(conn)
        assert result.count("ENGINE_SPEED") == 1

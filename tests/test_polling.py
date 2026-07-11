"""Tests for :mod:`elm327_obdii.polling`.

Covers the polling state machine, the voltage-gate hysteresis, and the
CAN-context cursor logic. The Poller is driven with a mock obdii
Connection so no real BLE/serial hardware is needed.
"""

from unittest.mock import MagicMock, patch

from bleak.exc import BleakError

from elm327_obdii._core.can_context import CanContext
from elm327_obdii._core.schema import CustomPid, ProfileConfig
from elm327_obdii.polling import (
    Poller,
    PollerConfig,
    PollingState,
    PollResult,
    _apply_can_context,
    _build_query_plan_from_profile,
    _reset_to_default_addressing,
)


def _make_config(
    profile: ProfileConfig | None = None,
    atrv_supported: bool = True,
    voltage_check: bool = True,
    voltage_on: float = 12.5,
    voltage_off: float = 11.8,
    grace_seconds: int = 30,
) -> PollerConfig:
    """Build a PollerConfig with sensible defaults."""
    return PollerConfig(
        profile=profile or ProfileConfig(),
        atrv_supported=atrv_supported,
        voltage_check_enabled=voltage_check,
        voltage_on=voltage_on,
        voltage_off=voltage_off,
        grace_seconds=grace_seconds,
    )


class TestPollingState:
    """The PollingState enum."""

    def test_initial_state_is_out_of_range(self) -> None:
        """A new Poller starts in OUT_OF_RANGE."""
        poller = Poller(_make_config())
        assert poller.state == PollingState.OUT_OF_RANGE

    def test_is_connected_false_when_no_api(self) -> None:
        """is_connected is False before connect()."""
        poller = Poller(_make_config())
        assert poller.is_connected is False


class TestPollOnceVoltageGate:
    """The AT RV voltage check drives the state machine."""

    def test_high_voltage_transitions_to_car_on(self) -> None:
        """Voltage above on_threshold → CAR_ON."""
        poller = Poller(_make_config())
        poller._api = MagicMock()
        poller._api.is_connected.return_value = True
        rv_resp = MagicMock()
        rv_resp.raw = b"14.2V\r>"
        poller._api.query.return_value = rv_resp
        result = poller.poll_once()
        assert result.state == PollingState.CAR_ON
        assert result.voltage == 14.2

    def test_low_voltage_transitions_to_grace_period(self) -> None:
        """Voltage below off_threshold on first poll → GRACE_PERIOD."""
        poller = Poller(_make_config(grace_seconds=30))
        poller._api = MagicMock()
        poller._api.is_connected.return_value = True
        rv_resp = MagicMock()
        rv_resp.raw = b"10.5V\r>"
        poller._api.query.return_value = rv_resp
        result = poller.poll_once()
        # First low-voltage poll enters the grace period
        assert result.state == PollingState.GRACE_PERIOD
        assert result.voltage == 10.5

    def test_voltage_check_disabled(self) -> None:
        """When voltage_check_enabled is False, state is CAR_ON with no voltage."""
        poller = Poller(_make_config(voltage_check=False))
        poller._api = MagicMock()
        poller._api.is_connected.return_value = True
        result = poller.poll_once()
        assert result.state == PollingState.CAR_ON
        assert result.voltage is None

    def test_atrv_not_supported(self) -> None:
        """When atrv_supported is False, state is CAR_ON with no voltage."""
        poller = Poller(_make_config(atrv_supported=False))
        poller._api = MagicMock()
        poller._api.is_connected.return_value = True
        result = poller.poll_once()
        assert result.state == PollingState.CAR_ON
        assert result.voltage is None

    def test_unparsable_voltage(self) -> None:
        """An unparsable AT RV response yields CAR_ON, None voltage."""
        poller = Poller(_make_config())
        poller._api = MagicMock()
        poller._api.is_connected.return_value = True
        rv_resp = MagicMock()
        rv_resp.raw = b"NO DATA\r>"
        poller._api.query.return_value = rv_resp
        result = poller.poll_once()
        assert result.state == PollingState.CAR_ON
        assert result.voltage is None

    def test_car_off_preserves_state_on_transport_error(self) -> None:
        """A transport error returns the previous state (no crash)."""
        poller = Poller(_make_config())
        poller._state = PollingState.CAR_ON
        poller._api = MagicMock()
        poller._api.is_connected.return_value = True
        poller._api.query.side_effect = BleakError("disconnected")
        result = poller.poll_once()
        # The poller preserves state on transport error
        assert result.state == PollingState.CAR_ON
        assert poller._api is None  # connection reset


class TestApplyCanContext:
    """The CAN-context transition logic (C2 fix: reset to default addressing)."""

    def test_default_context_calls_reset(self) -> None:
        """Transitioning to the default context calls _reset_to_default_addressing."""
        transport = MagicMock()
        api = MagicMock()
        with patch("elm327_obdii.polling._reset_to_default_addressing") as mock_reset:
            _apply_can_context(transport, CanContext(), api)
            mock_reset.assert_called_once_with(transport, api)

    def test_custom_context_sends_sh_and_cra(self) -> None:
        """A custom context sends ATSH and ATCRA."""
        transport = MagicMock()
        api = MagicMock()
        ctx = CanContext(header="7E5", filter="7ED")
        _apply_can_context(transport, ctx, api)
        calls = [call.args[0] for call in transport.write_bytes.call_args_list]
        assert any(b"ATSH7E5" in c for c in calls)
        assert any(b"ATCRA7ED" in c for c in calls)

    def test_extra_init_sent(self) -> None:
        """Extra init commands are split by semicolon and sent individually."""
        transport = MagicMock()
        api = MagicMock()
        ctx = CanContext(extra_init="ATST64;ATFCSM0")
        _apply_can_context(transport, ctx, api)
        calls = [call.args[0] for call in transport.write_bytes.call_args_list]
        assert any(b"ATST64" in c for c in calls)
        assert any(b"ATFCSM0" in c for c in calls)

    def test_extra_init_strips_empty_segments(self) -> None:
        """Empty segments in extra_init are skipped."""
        transport = MagicMock()
        api = MagicMock()
        ctx = CanContext(extra_init="ATST64;; ;ATFCSM0")
        _apply_can_context(transport, ctx, api)
        # Should send 2 commands, not 4 (empty segments skipped)
        assert transport.write_bytes.call_count == 2


class TestResetToDefaultAddressing:
    """The _reset_to_default_addressing function (C2 fix)."""

    def test_11bit_protocol_sends_7df(self) -> None:
        """Protocol 6 (11-bit CAN) resets to ATSH7DF."""
        transport = MagicMock()
        api = MagicMock()
        with patch(
            "elm327_obdii.polling._detect_protocol",
            return_value="6",
        ):
            _reset_to_default_addressing(transport, api)
        calls = [call.args[0] for call in transport.write_bytes.call_args_list]
        assert any(b"ATCRA" in c for c in calls)  # filter cleared
        assert any(b"ATSH7DF" in c for c in calls)  # broadcast header

    def test_29bit_protocol_sends_18db33f1(self) -> None:
        """Protocol 7 (29-bit CAN) resets to ATSH18DB33F1."""
        transport = MagicMock()
        api = MagicMock()
        with patch(
            "elm327_obdii.polling._detect_protocol",
            return_value="7",
        ):
            _reset_to_default_addressing(transport, api)
        calls = [call.args[0] for call in transport.write_bytes.call_args_list]
        assert any(b"ATSH18DB33F1" in c for c in calls)

    def test_unknown_protocol_sends_atd(self) -> None:
        """Unknown protocol falls back to ATD (set all defaults)."""
        transport = MagicMock()
        api = MagicMock()
        with patch(
            "elm327_obdii.polling._detect_protocol",
            return_value=None,
        ):
            _reset_to_default_addressing(transport, api)
        calls = [call.args[0] for call in transport.write_bytes.call_args_list]
        assert any(b"ATD" in c for c in calls)


class TestBuildQueryPlanFromProfile:
    """The profile → query-plan translation."""

    def test_empty_profile(self) -> None:
        """An empty profile produces an empty plan."""
        plan = _build_query_plan_from_profile(ProfileConfig())
        assert plan == []

    def test_standard_pids_only(self) -> None:
        """Standard PIDs produce a single default-context group."""
        profile = ProfileConfig(
            standard_pids=["ENGINE_SPEED", "VEHICLE_SPEED"],
        )
        plan = _build_query_plan_from_profile(profile)
        assert len(plan) == 1
        assert plan[0][0] == CanContext()  # default context
        assert len(plan[0][1]) == 2

    def test_custom_pid_with_invalid_fmt_skipped(self) -> None:
        """A custom PID with an invalid fmt is skipped, not fatal."""
        profile = ProfileConfig(
            custom_pids=[
                CustomPid(
                    id="bad",
                    name="Bad",
                    mode="22",
                    query="FFFF",
                    fmt={"bix": -1, "len": 8},  # invalid bix
                ),
                CustomPid(
                    id="good",
                    name="Good",
                    mode="22",
                    query="FFFF",
                    fmt={"bix": 0, "len": 8},
                ),
            ],
        )
        plan = _build_query_plan_from_profile(profile)
        # Only the valid PID survives
        total_items = sum(len(items) for _, items in plan)
        assert total_items == 1

    def test_unknown_standard_pid_skipped(self) -> None:
        """An unknown standard PID name is skipped with a warning."""
        profile = ProfileConfig(standard_pids=["NONEXISTENT", "ENGINE_SPEED"])
        plan = _build_query_plan_from_profile(profile)
        total_items = sum(len(items) for _, items in plan)
        assert total_items == 1  # only ENGINE_SPEED


class TestPollResult:
    """The PollResult dataclass defaults."""

    def test_defaults(self) -> None:
        """A PollResult with only state has sensible defaults."""
        result = PollResult(state=PollingState.CAR_ON)
        assert result.data == {}
        assert result.any_success is False
        assert result.voltage is None

    def test_with_data(self) -> None:
        """A PollResult can carry data and voltage."""
        result = PollResult(
            state=PollingState.CAR_ON,
            data={"ENGINE_SPEED": 1500.0},
            any_success=True,
            voltage=14.2,
        )
        assert result.data["ENGINE_SPEED"] == 1500.0
        assert result.any_success is True
        assert result.voltage == 14.2

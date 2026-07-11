"""Polling state machine, voltage gate, and query-plan execution.

The :class:`Poller` façade owns the per-config query plan, the polling
state machine, the CAN-context cursor, and the BLE connection. Callers
(the HA coordinator) drive it via :meth:`Poller.connect`,
:meth:`Poller.poll_once`, :meth:`Poller.disconnect`, and
:meth:`Poller.scan_supported_standard_pids`.

This collapses what used to be three separate library calls
(plan-building, voltage check, plan execution) plus a hand-threaded
CAN-context cursor into a single object that owns the cross-cycle
state internally.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import threading
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bluetooth_data_tools import monotonic_time_coarse
from obdii import Command, Connection, Mode, Response
from obdii.transports.transport_base import TransportBase

from ._core.can_context import CanContext, context_for_custom_pid
from ._core.elm327_parsing import extract_protocol_number, extract_voltage
from ._core.fmt_evaluator import FmtValidationError, make_fmt_evaluator
from ._core.query_items import (
    CustomQueryItem,
    QueryItem,
    StandardQueryItem,
    build_query_plan,
)
from ._core.schema import ProfileConfig
from ._core.standard_pids import get_standard_command, scan_supported_pids
from .transport_ble import TransportBLE, TransportError

_LOGGER = logging.getLogger(__name__)


class PollingState(StrEnum):
    """States of the vehicle polling state machine."""

    OUT_OF_RANGE = "out_of_range"
    CAR_ON = "car_on"
    GRACE_PERIOD = "grace_period"
    CAR_OFF = "car_off"


@dataclass(frozen=True)
class PollerConfig:
    """Static configuration for a :class:`Poller`.

    Built once from a config entry's data + options; the Poller reads
    it on every cycle. Voltage thresholds protect the vehicle's 12V
    auxiliary battery - when voltage drops below ``voltage_off`` for
    longer than ``grace_seconds``, the poller transitions to
    :attr:`PollingState.CAR_OFF` and the coordinator slows its poll
    interval accordingly.
    """

    profile: ProfileConfig
    atrv_supported: bool
    voltage_check_enabled: bool
    voltage_on: float
    voltage_off: float
    grace_seconds: int


@dataclass
class PollResult:
    """One polling cycle's outcome.

    The coordinator reads ``state`` to decide the next poll interval
    and ``data`` to populate ``coordinator.data``. ``voltage`` is
    exposed for diagnostics.
    """

    state: PollingState
    data: dict[str, Any] = field(default_factory=dict)
    any_success: bool = False
    voltage: float | None = None


class Poller:
    """Owns: query plan, CAN-context cursor, polling state machine, BLE connection.

    Thread-safe via an internal :class:`threading.Lock` - all public
    methods may be called from the executor pool while the event loop
    reads :attr:`state` and :attr:`is_connected` concurrently.
    """

    def __init__(self, config: PollerConfig) -> None:
        """Build the query plan and initialize state to OUT_OF_RANGE."""
        self._config = config
        self._query_plan = _build_query_plan_from_profile(config.profile)
        self._current_context: CanContext | None = None
        self._state: PollingState = PollingState.OUT_OF_RANGE
        self._grace_start: float | None = None
        self._api: Connection | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> PollingState:
        """Current polling state. Lock-free read; safe from the event loop."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """True if the BLE link to the adapter is up.

        Lock-free read; safe from the event loop. May lag the executor
        thread's view by up to one poll cycle.
        """
        return self._api is not None and self._api.is_connected()

    def connect(
        self,
        ble_device: BLEDevice,
        loop: asyncio.AbstractEventLoop,
        uuid_write: str,
        uuid_read: str,
        timeout: float = 4.0,
    ) -> bool:
        """Open (or reuse) the BLE connection to the adapter. Returns success.

        Idempotent: if a connection is already alive, returns True
        immediately. On failure, closes any stale handle and returns
        False (no exception raised - the caller decides what to do).
        """
        with self._lock:
            if self._api is not None and self._api.is_connected():
                return True
            if self._api is not None:
                with contextlib.suppress(BleakError, OSError, TransportError):
                    self._api.close()
                self._api = None
                self._current_context = None
            self._api = _create_connection(
                ble_device, loop, uuid_write, uuid_read, timeout
            )
            if self._api is None:
                return False
            self._current_context = None
            return True

    def disconnect(self) -> None:
        """Close the BLE connection if open. Safe to call repeatedly."""
        with self._lock:
            if self._api is not None:
                with contextlib.suppress(BleakError, OSError, TransportError):
                    self._api.close()
                self._api = None
                self._current_context = None

    def poll_once(self) -> PollResult:
        """Run one polling cycle. Caller must have connected first.

        On transport failure, resets the connection and returns a
        :class:`PollResult` preserving the previous state. Does NOT
        raise - the coordinator decides whether to surface this as
        :class:`UpdateFailed` (which keeps ``last_update_success``
        accurate in the base coordinator).
        """
        with self._lock:
            if self._api is None or not self._api.is_connected():
                _LOGGER.debug("poll_once: not connected, returning empty result")
                return PollResult(state=self._state)

            try:
                new_state, voltage = self._check_voltage()
                self._state = new_state
                _LOGGER.debug(
                    "poll_once: voltage=%s, new_state=%s", voltage, new_state.value
                )

                data: dict[str, Any] = {}
                any_success = False
                if new_state != PollingState.CAR_OFF:
                    data, any_success, self._current_context = _run_query_plan(
                        self._api,
                        self._query_plan,
                        self._current_context,
                    )
                    _LOGGER.debug(
                        "poll_once: query plan done — any_success=%s, keys=%s",
                        any_success,
                        list(data.keys()),
                    )
                else:
                    _LOGGER.debug("poll_once: skipping query plan (CAR_OFF)")

                return PollResult(
                    state=new_state, data=data, any_success=any_success, voltage=voltage
                )

            except (
                BleakError,
                TimeoutError,
                OSError,
                ConnectionError,
                TransportError,
            ) as err:
                _LOGGER.warning(
                    "Error during polling cycle, resetting connection: %s", err
                )
                with contextlib.suppress(BleakError, OSError, TransportError):
                    self._api.close()
                self._api = None
                self._current_context = None
                return PollResult(state=self._state)

    def scan_supported_standard_pids(self) -> list[str]:
        """Walk the ECU's Mode 01 PID bitmaps and return supported command names.

        Caller must have connected first. Raises :class:`RuntimeError`
        if called while disconnected (the coordinator maps this to an
        :class:`UpdateFailed`).
        """
        with self._lock:
            if self._api is None or not self._api.is_connected():
                raise RuntimeError("Adapter not connected - cannot scan supported PIDs")
            return scan_supported_pids(self._api)

    def _check_voltage(self) -> tuple[PollingState, float | None]:
        """Query battery voltage via AT RV and advance the state machine.

        Assumes ``self._api`` is connected. Returns the new state plus
        the parsed voltage (None if the response was unreadable or the
        voltage check is disabled).
        """
        cfg = self._config
        if not (cfg.atrv_supported and cfg.voltage_check_enabled):
            return PollingState.CAR_ON, None

        assert self._api is not None
        rv_resp: Response[Any] = self._api.query(Command(Mode.AT, "RV"))
        if not rv_resp or not rv_resp.raw:
            _LOGGER.debug("Empty or invalid RV response received")
            return PollingState.CAR_ON, None

        voltage = extract_voltage(rv_resp.raw)
        if voltage is None:
            _LOGGER.debug(
                "Could not parse numeric voltage from RV response: %r",
                rv_resp.raw.decode(errors="ignore"),
            )
            return PollingState.CAR_ON, None

        # Hysteresis: use the higher on_threshold to transition out of
        # CAR_OFF, the lower off_threshold otherwise. On the first poll
        # after init the state is OUT_OF_RANGE, so the lower threshold
        # applies - a single dip below on_threshold won't keep us
        # classified as out of range indefinitely.
        is_running = (
            voltage >= cfg.voltage_on
            if self._state == PollingState.CAR_OFF
            else voltage >= cfg.voltage_off
        )

        if is_running:
            self._grace_start = None
            return PollingState.CAR_ON, voltage

        if self._state == PollingState.CAR_OFF:
            return PollingState.CAR_OFF, voltage

        if self._grace_start is None:
            self._grace_start = monotonic_time_coarse()

        if monotonic_time_coarse() - self._grace_start > cfg.grace_seconds:
            return PollingState.CAR_OFF, voltage

        return PollingState.GRACE_PERIOD, voltage


def _build_query_plan_from_profile(
    profile: ProfileConfig,
) -> list[tuple[CanContext, list[QueryItem]]]:
    """Combine standard + custom PIDs into a context-grouped query plan."""
    items: list[QueryItem] = []

    for name in profile.standard_pids:
        command = get_standard_command(name)
        if command is None:
            _LOGGER.warning(
                "Standard PID %s not found in obdii registry - skipping", name
            )
            continue
        items.append(StandardQueryItem(command_name=name, command=command))

    for pid in profile.custom_pids:
        try:
            command = Command(
                pid.mode,
                pid.query,
                expected_bytes=pid.expected_bytes or 0,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not build obdii.Command for custom PID %s (mode=%s query=%s): %s",
                pid.name,
                pid.mode,
                pid.query,
                err,
            )
            continue
        try:
            evaluator = make_fmt_evaluator(pid.fmt)
        except (FmtValidationError, Exception) as err:  # noqa: BLE001
            _LOGGER.error(
                "Custom PID %s has invalid fmt %r - skipping: %s",
                pid.name,
                pid.fmt,
                err,
            )
            continue
        items.append(
            CustomQueryItem(
                pid=pid,
                command=command,
                evaluator=evaluator,
                context=context_for_custom_pid(pid),
            )
        )

    return build_query_plan(items)


def _create_connection(
    ble_dev: BLEDevice,
    loop: asyncio.AbstractEventLoop,
    uuid_write: str,
    uuid_read: str,
    timeout: float = 4.0,
) -> Connection | None:
    """Create a TransportBLE + Connection. Returns None on failure."""
    transport: TransportBLE | None = None
    try:
        transport = TransportBLE(
            ble_device=ble_dev,
            loop=loop,
            uuid_write=uuid_write,
            uuid_read=uuid_read,
            timeout=timeout,
        )
        return Connection(transport)
    except (BleakError, TimeoutError, OSError, TransportError) as e:
        _LOGGER.warning("Connection failed: %s", e)
        if transport is not None:
            with contextlib.suppress(BleakError, OSError, TransportError):
                transport.close()
        return None


def _apply_can_context(
    transport: TransportBase,
    context: CanContext,
    api: Connection,
) -> None:
    """Send the AT commands needed to transition to ``context``.

    The default :class:`CanContext()` (all fields None) means "adapter
    default addressing" - which must actively clear any previously-set
    ATSH/ATCRA, otherwise a custom PID's header/filter persists into
    the next group's standard-PID pass and breaks every Mode 01 query.
    The reset header is protocol-dependent (11-bit vs 29-bit CAN), so
    the active protocol is probed lazily via :func:`_detect_protocol`
    at reset time - by then the adapter has locked onto whatever
    protocol the profile init or the last group's ``extra_init`` set.
    """
    if context.header is None and context.filter is None and not context.extra_init:
        _reset_to_default_addressing(transport, api)
        return
    if context.header is not None:
        _send_at(transport, f"ATSH{context.header}")
    if context.filter is not None:
        _send_at(transport, f"ATCRA{context.filter}")
    if context.extra_init:
        for cmd in context.extra_init.split(";"):
            cmd = cmd.strip()
            if cmd:
                _send_at(transport, cmd)


def _reset_to_default_addressing(transport: TransportBase, api: Connection) -> None:
    """Clear any custom ATSH/ATCRA so standard Mode 01 queries work again.

    ``ATCRA`` with no argument clears the receive filter (protocol-
    agnostic). The header reset depends on the active protocol because
    the ELM327 interprets ``ATSH<n>`` differently on 11-bit vs 29-bit
    CAN - sending ``ATSH7DF`` on a 29-bit protocol (e.g. ``ATSP7``)
    silently sets the 29-bit header to ``000007DF``, not the broadcast
    address, so the ECU never sees the query.

    Protocol numbers (from ``ATDPN``, probed lazily here so the value
    reflects whatever the last group's init left active):
      6, 8 = 11-bit CAN  -> ``ATSH7DF``  (functional broadcast)
      7, 9 = 29-bit CAN  -> ``ATSH18DB33F1``  (functional broadcast)
      others (1-5, A-C, None)  = ``ATD``  (set all defaults; heavy but correct)
    """
    _send_at(transport, "ATCRA")
    protocol = _detect_protocol(api)
    if protocol in ("6", "8"):
        _send_at(transport, "ATSH7DF")
    elif protocol in ("7", "9"):
        _send_at(transport, "ATSH18DB33F1")
    else:
        # Unknown or non-CAN protocol - ATD is the only safe universal
        # reset. The caller's profile-level init (ATSPn/ATSTn) must be
        # re-issued on the next connect; we accept that cost here because
        # misaddressed queries are worse than a stale timeout setting.
        _LOGGER.warning(
            "Unknown ELM327 protocol %r - issuing ATD to reset addressing; "
            "profile-level init should be re-applied on next connect",
            protocol,
        )
        _send_at(transport, "ATD")


def _detect_protocol(api: Connection) -> str | None:
    """Query ``ATDPN`` and return the protocol number as a single-char string.

    Returns None if the adapter doesn't respond or returns an unparsable
    value. Parsing is delegated to :func:`extract_protocol_number` so the
    echo-stripping and noise-handling logic lives alongside the other
    ELM327 response parsers in :mod:`elm327_obdii._core.elm327_parsing`.
    """
    try:
        resp: Response[Any] = api.query(Command(Mode.AT, "DPN"))
    except (BleakError, TimeoutError, OSError, TransportError) as err:
        _LOGGER.debug("ATDPN query failed, assuming unknown protocol: %s", err)
        return None
    if not resp or not resp.raw:
        return None
    return extract_protocol_number(resp.raw)


def _send_at(transport: TransportBase, command: str) -> None:
    """Write a single AT command + CR, then drain the response."""
    try:
        transport.write_bytes(command.encode() + b"\r")
        transport.read_bytes()
    except (OSError, TimeoutError, TransportError) as err:
        _LOGGER.debug("AT command %r failed: %s", command, err)


def _run_query_plan(
    api: Connection,
    plan: list[tuple[CanContext, list[QueryItem]]],
    current_context: CanContext | None,
) -> tuple[dict[str, Any], bool, CanContext | None]:
    """Walk the query plan, switching CAN context only between groups.

    Returns ``(data, any_success, new_current_context)``. The plan's
    "default-first" ordering (see :func:`build_query_plan`) means the
    cursor carried over from the previous cycle is typically already
    the default context, so no reset fires at the start of a cycle -
    only on actual custom -> default transitions within a cycle.
    """
    res_data: dict[str, Any] = {}
    any_success = False
    ctx = current_context

    for context, items in plan:
        if context != ctx:
            _LOGGER.debug(
                "query_plan: switching context (header=%s, filter=%s, extra=%s)",
                context.header,
                context.filter,
                context.extra_init,
            )
            _apply_can_context(api.transport, context, api)
            ctx = context

        for item in items:
            try:
                value = item.execute(api)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Query %s failed: %s", item.key, err)
                continue
            if value is not None:
                res_data[item.key] = value
                any_success = True
                _LOGGER.debug("Query %s = %s", item.key, value)
            else:
                _LOGGER.debug("Query %s returned None", item.key)

    return res_data, any_success, ctx

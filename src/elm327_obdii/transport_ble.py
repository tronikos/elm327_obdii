"""Bluetooth Low Energy transport for the ELM327 OBD-II adapter.

Subclasses :class:`obdii.transports.transport_base.TransportBase` to
provide a :mod:`bleak`-backed sync+async transport with GATT
notify-based reads, dynamic UUID fallback discovery, and context
manager support.

The sync API (``connect``, ``close``, ``write_bytes``, ``read_bytes``)
is called from the poller on the executor thread. The async API
(``async_connect``, ``async_close``, ``_write``) runs on the event
loop, dispatched via :meth:`_run_coro` which uses
:func:`asyncio.run_coroutine_threadsafe`.
"""

import asyncio
from collections.abc import Coroutine
import concurrent.futures
import contextlib
import logging
from threading import Event, Lock
from time import monotonic
from types import TracebackType
from typing import Any, Self, override

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from obdii.basetypes import MISSING
from obdii.transports.transport_base import TransportBase

_LOGGER = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """Raised by :class:`TransportBLE` for connection/state failures.

    Subclasses :class:`RuntimeError` for backward compatibility, but
    callers should catch :class:`TransportError` specifically to avoid
    swallowing unrelated RuntimeErrors from the obdii library or Python.
    """


class TransportBLE(TransportBase):
    """Bluetooth Low Energy transport implementation."""

    def __init__(
        self,
        ble_device: BLEDevice = MISSING,
        uuid_write: str = MISSING,
        uuid_read: str = MISSING,
        timeout: float = 10.0,
        loop: asyncio.AbstractEventLoop | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the BLE transport."""
        if ble_device is MISSING or uuid_write is MISSING or uuid_read is MISSING:
            raise ValueError(
                f"ble_device ({ble_device}), uuid_write ({uuid_write}) and "
                f"uuid_read ({uuid_read}) must be specified for TransportBLE."
            )

        self.config: dict[str, Any] = {
            "uuid_write": uuid_write,
            "uuid_read": uuid_read,
            "timeout": timeout,
            **kwargs,
        }

        self._ble_device = ble_device
        self._ble_conn: BleakClient | None = None
        self._buffer = bytearray()
        self._lock = Lock()
        self._data_ready = Event()
        self._loop = loop

    @override
    def __repr__(self) -> str:
        """Return representation of TransportBLE."""
        return f"<TransportBLE {self._ble_device}>"

    def _run_coro(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run a coroutine thread-safely in the specified loop."""
        if self._loop is None:
            raise TransportError("Event loop is not running.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self.config["timeout"])
        except (TimeoutError, concurrent.futures.TimeoutError) as exc:
            future.cancel()
            raise TimeoutError("BLE operation timed out") from exc

    def _notify_callback(self, _sender: Any, data: bytearray) -> None:
        """Handle incoming notify notifications from BLE device."""
        with self._lock:
            self._buffer.extend(data)
        self._data_ready.set()

    async def async_connect(self) -> None:
        """Establish BLE connection and enable notify descriptors."""
        _LOGGER.debug(
            "Attempting to connect to BLE device %s (%s)",
            self._ble_device.name,
            self._ble_device.address,
        )

        # Explicitly clear old buffer state to prevent bleeding cross-sessions
        with self._lock:
            self._buffer.clear()
        self._data_ready.clear()

        self._ble_conn = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._ble_device.name or "Unknown Device",
            max_attempts=3,
        )

        services = self._ble_conn.services
        write_char: str | None = None
        read_char: str | None = None

        # 1. Try the configured UUIDs first (exact match, case-insensitive).
        for service in services:
            for char in service.characteristics:
                if char.uuid.lower() == self.config.get("uuid_write", "").lower():
                    write_char = char.uuid
                if char.uuid.lower() == self.config.get("uuid_read", "").lower():
                    read_char = char.uuid

        # 2. Dynamic discovery fallback for adapters with non-standard UUIDs.
        if not write_char or not read_char:
            _LOGGER.debug(
                "Configured characteristics not found - attempting dynamic discovery"
            )
            for service in services:
                # Skip standard BLE SIG services known to cause false-positive
                # matches during fallback discovery: Generic Access (0x1800),
                # Generic Attribute (0x1801), Device Information (0x180A),
                # Battery Service (0x180F). Do NOT skip 0x18F0 - that's a
                # common OBD-II adapter service UUID.
                service_uuid = service.uuid.lower()
                if service_uuid in (
                    "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
                    "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
                    "0000180a-0000-1000-8000-00805f9b34fb",  # Device Information
                    "0000180f-0000-1000-8000-00805f9b34fb",  # Battery Service
                ):
                    continue
                for char in service.characteristics:
                    props = char.properties
                    if not write_char and (
                        "write" in props or "write-without-response" in props
                    ):
                        write_char = char.uuid
                        _LOGGER.debug(
                            "Auto-discovered write characteristic: %s", write_char
                        )
                    if not read_char and ("notify" in props or "indicate" in props):
                        read_char = char.uuid
                        _LOGGER.debug(
                            "Auto-discovered read characteristic: %s", read_char
                        )

        if not write_char or not read_char:
            raise TransportError(
                "Could not locate compatible Read/Write GATT characteristics. "
                "Verify the adapter UUIDs or try a different BLE serial profile."
            )

        # Persist discovered UUIDs so subsequent connect() calls skip re-discovery.
        self.config["uuid_write"] = write_char
        self.config["uuid_read"] = read_char

        await self._ble_conn.start_notify(
            self.config["uuid_read"], self._notify_callback
        )

    async def async_close(self) -> None:
        """Gracefully disconnect from BLE peripheral."""
        if self._ble_conn:
            try:
                if self._ble_conn.is_connected:
                    with contextlib.suppress(BleakError, OSError, TransportError):
                        await self._ble_conn.stop_notify(self.config["uuid_read"])
                    with contextlib.suppress(BleakError, OSError, TransportError):
                        await self._ble_conn.disconnect()
            finally:
                self._ble_conn = None

    async def _write(self, query: bytes) -> None:
        """Write query bytes to GATT character representation."""
        if self._ble_conn is None:
            raise TransportError("BLE connection is not established.")
        await self._ble_conn.write_gatt_char(self.config["uuid_write"], query)

    def get_service_collection(self) -> BleakGATTServiceCollection:
        """Return discovered GATT service collection."""
        if self._ble_conn is None:
            raise TransportError("BLE connection is not established.")
        return self._ble_conn.services

    @override
    def connect(
        self, loop: asyncio.AbstractEventLoop | None = None, **kwargs: Any
    ) -> None:
        """Connect to BLE device blocking-wise."""
        self.config.update(kwargs)

        if loop is not None:
            self._loop = loop

        try:
            self._run_coro(self.async_connect())
        except Exception:
            self.close()  # Cleanup on failure
            raise

    @override
    def close(self) -> None:
        """Disconnect from BLE transport."""
        if self._ble_conn is not None:
            with contextlib.suppress(BleakError, OSError, TransportError):
                self._run_coro(self.async_close())
        # Wake up any reader threads currently blocked in read_bytes.
        self._data_ready.set()

    @override
    def is_connected(self) -> bool:
        """Verify GATT connection status."""
        if self._ble_conn is None:
            return False
        return self._ble_conn.is_connected

    @override
    def write_bytes(self, query: bytes) -> None:
        """Write raw bytes to target write characteristic."""
        if not self.is_connected():
            raise TransportError("BLE is not connected.")
        with self._lock:
            self._buffer.clear()
        self._data_ready.clear()
        self._run_coro(self._write(query))

    @override
    def read_bytes(self, expected_seq: bytes = b">", size: Any = MISSING) -> bytes:
        """Read bytes until the terminal sequence or size limit is satisfied.

        Consuming (deleting) matched bytes from the internal buffer prevents
        stale data from an earlier AT init response bleeding into the next
        query when back-to-back commands arrive faster than write_bytes can
        issue its buffer-clear.
        """
        lenterm = len(expected_seq)
        deadline = monotonic() + self.config["timeout"]

        while True:
            if not self.is_connected():
                raise TransportError("BLE connection lost while reading.")

            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("read timed out.")

            with self._lock:
                snapshot = bytes(self._buffer)

            consumed_len: int | None = None

            # Check for the terminal sequence anywhere in the buffer (not just
            # at the end) so a prompt that arrives mid-chunk is caught promptly.
            idx = snapshot.find(expected_seq)
            if idx != -1:
                consumed_len = idx + lenterm
            elif (
                size is not MISSING and isinstance(size, int) and len(snapshot) >= size
            ):
                consumed_len = size

            if consumed_len is not None:
                with self._lock:
                    ret_bytes = bytes(self._buffer[:consumed_len])
                    del self._buffer[:consumed_len]
                return ret_bytes

            self._data_ready.wait(timeout=remaining)
            self._data_ready.clear()

    def __enter__(self) -> Self:
        """Enter context manager block."""
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context manager block."""
        self.close()

    async def __aenter__(self) -> Self:
        """Enter async context manager block."""
        await self.async_connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit async context manager block."""
        await self.async_close()

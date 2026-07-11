"""Config-flow-time BLE OBD-II adapter probing.

Pure-Python helpers for probing BLE OBD-II adapters during the config
flow: open a TransportBLE + obdii.Connection, query AT RV, scan
supported PIDs, and report back. Also provides GATT characteristic
enumeration for the manual UUID selection UI.

No Home Assistant imports.
"""

import asyncio
import contextlib
from dataclasses import dataclass
import logging

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from obdii import Command, Connection, Mode

from ._core.elm327_parsing import extract_voltage
from ._core.standard_pids import scan_supported_pids
from .transport_ble import TransportBLE, TransportError

_LOGGER = logging.getLogger(__name__)


@dataclass
class ConnectionTestResult:
    """Result of probing a BLE OBD-II adapter.

    ``success`` is None if the connection itself failed; True/False if
    the connection worked and AT RV returned a parseable voltage vs an
    unparsable response. ``scanned_supported`` is the list of supported
    Mode 01 PID names if the scan succeeded, else None.
    """

    success: bool | None
    uuid_write: str
    uuid_read: str
    scanned_supported: list[str] | None


def probe_adapter(
    ble_device: BLEDevice,
    loop: asyncio.AbstractEventLoop,
    uuid_write: str,
    uuid_read: str,
    timeout: float,
) -> ConnectionTestResult:
    """Open a BLE connection, query AT RV, and scan supported PIDs.

    Closes the connection before returning. Safe to call from an
    executor thread.
    """
    conn: Connection | None = None
    final_write = uuid_write
    final_read = uuid_read
    scanned: list[str] | None = None
    resp: object = None

    try:
        transport = TransportBLE(
            ble_device=ble_device,
            loop=loop,
            uuid_write=final_write,
            uuid_read=final_read,
            timeout=timeout,
        )
        conn = Connection(transport)
        final_write = transport.config.get("uuid_write", final_write)
        final_read = transport.config.get("uuid_read", final_read)
        resp = conn.query(Command(Mode.AT, "RV"))

        try:
            scanned = scan_supported_pids(conn)
        except (BleakError, TimeoutError, OSError, KeyError, TransportError) as err:
            _LOGGER.debug("Supported-PID scan during setup failed: %s", err)
            scanned = None
    except (BleakError, TimeoutError, OSError, TransportError) as e:
        _LOGGER.debug("Connection test failed: %s", e)
        return ConnectionTestResult(None, final_write, final_read, None)
    finally:
        if conn:
            with contextlib.suppress(BleakError, OSError, TransportError):
                conn.close()

    success = resp is not None and extract_voltage(resp.raw) is not None
    return ConnectionTestResult(success, final_write, final_read, scanned)


async def async_get_characteristics(
    ble_device: BLEDevice,
) -> list[BleakGATTCharacteristic]:
    """Fetch GATT characteristics via the BLE service cache."""
    client = None
    characteristics: list[BleakGATTCharacteristic] = []
    try:
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.name or "Unknown Device",
            max_attempts=2,
        )
        for service in client.services:
            characteristics.extend(service.characteristics)
    except (BleakError, TimeoutError, OSError) as err:
        _LOGGER.error("Failed to fetch GATT characteristics: %s", err)
        return []
    else:
        return characteristics
    finally:
        if client is not None:
            with contextlib.suppress(BleakError, OSError, TransportError):
                await client.disconnect()

"""Standalone CLI tool for testing the elm327_obdii library.

Run with::

    python -m elm327_obdii

First run: scans for BLE OBD-II adapters, lets the user pick one,
optionally imports a vehicle profile (WiCAN or OBDb), probes the
adapter to confirm connectivity and discover GATT UUIDs, and saves
the resulting config to ``~/.elm327_obdii_config.json``.

Subsequent runs: loads the config, connects to the adapter, and polls
in a loop, printing each poll's state, voltage, and decoded data keys.

Designed for testing the library directly on a Windows 10 laptop with
a BLE adapter. Cross-platform (also works on Linux/macOS).

The CLI is intentionally self-contained: it uses only the public API
exported from :mod:`elm327_obdii` (no Home Assistant imports) and
delegates all BLE transport + state machine + query plan work to the
:class:`elm327_obdii.Poller` façade.
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
from typing import Any

import aiohttp
from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from . import (
    RECOMMENDED_DEFAULTS,
    Poller,
    PollerConfig,
    PollingState,
    PollResult,
    ProfileConfig,
    fetch_obdb_matrix,
    fetch_obdb_repo_default_json,
    fetch_wican_profiles,
    format_sensor_value,
    import_obdb_profile,
    import_wican_profile,
    probe_adapter,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".elm327_obdii_config.json"

# Poll intervals (seconds) by state machine state, when --interval is
# not given. CAR_OFF polls much slower to avoid draining the 12V aux
# battery while the vehicle is parked.
DEFAULT_INTERVALS: dict[PollingState, float] = {
    PollingState.CAR_ON: 10.0,
    PollingState.GRACE_PERIOD: 10.0,
    PollingState.OUT_OF_RANGE: 10.0,
    PollingState.CAR_OFF: 300.0,
}

# Default voltage thresholds (V) and grace period (s). The voltage gate
# protects the vehicle's 12V auxiliary battery — when voltage drops
# below ``voltage_off`` for longer than ``grace_seconds``, the poller
# transitions to CAR_OFF and slows its poll interval.
DEFAULT_VOLTAGE_ON = 13.1
DEFAULT_VOLTAGE_OFF = 13.0
DEFAULT_GRACE_SECONDS = 30

# Placeholder UUIDs passed to probe_adapter. TransportBLE falls back to
# dynamic GATT-characteristic discovery if these aren't found, so the
# real UUIDs are returned in the ConnectionTestResult regardless.
DEFAULT_UUID_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"
DEFAULT_UUID_READ = "0000fff1-0000-1000-8000-00805f9b34fb"


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argument parser and parse ``argv``."""
    parser = argparse.ArgumentParser(
        prog="python -m elm327_obdii",
        description=(
            "Test the elm327_obdii library directly against a BLE OBD-II "
            "adapter. First run launches a setup wizard; subsequent runs "
            "load the saved config and poll."
        ),
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable debug logging for elm327_obdii and obdii loggers.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Re-run the setup wizard (delete old config, start fresh).",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="Override config and use no profile (standard PIDs only).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Override poll interval in seconds (default: from state machine).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (don't loop).",
    )
    parser.add_argument(
        "--list-pids",
        action="store_true",
        help="List all PIDs in the query plan and exit.",
    )
    return parser.parse_args(argv)


def _setup_logging(debug: bool) -> None:
    """Configure root + library loggers."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if debug:
        # The library's loggers default to WARNING; bump them to DEBUG so
        # the user can see voltage checks, query-plan context switches,
        # and per-PID values as they happen.
        for name in ("elm327_obdii", "obdii"):
            logging.getLogger(name).setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any] | None:
    """Load config from a JSON file. Returns None if missing or unreadable."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.warning("Could not load config from %s: %s", path, err)
        return None
    if not isinstance(data, dict):
        _LOGGER.warning("Config at %s is not a JSON object", path)
        return None
    return data


def save_config(path: Path, config: dict[str, Any]) -> None:
    """Save config to a JSON file (creates parent dirs as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, sort_keys=True)


def _poller_config_from_dict(cfg: dict[str, Any]) -> PollerConfig:
    """Reconstruct a PollerConfig from the saved config dict."""
    profile_dict = cfg.get("profile")
    if profile_dict:
        profile = ProfileConfig.from_dict(profile_dict)
    else:
        profile = ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS))
    return PollerConfig(
        profile=profile,
        atrv_supported=bool(cfg.get("atrv_supported", True)),
        voltage_check_enabled=bool(cfg.get("voltage_check", True)),
        voltage_on=float(cfg.get("voltage_on", DEFAULT_VOLTAGE_ON)),
        voltage_off=float(cfg.get("voltage_off", DEFAULT_VOLTAGE_OFF)),
        grace_seconds=int(cfg.get("grace_seconds", DEFAULT_GRACE_SECONDS)),
    )


def _poller_config_to_dict(
    poller_config: PollerConfig,
    address: str,
    uuid_write: str,
    uuid_read: str,
) -> dict[str, Any]:
    """Serialize a PollerConfig + adapter info to a JSON-compatible dict."""
    return {
        "address": address,
        "uuid_write": uuid_write,
        "uuid_read": uuid_read,
        "atrv_supported": poller_config.atrv_supported,
        "voltage_check": poller_config.voltage_check_enabled,
        "voltage_on": poller_config.voltage_on,
        "voltage_off": poller_config.voltage_off,
        "grace_seconds": poller_config.grace_seconds,
        "profile": poller_config.profile.to_dict(),
    }


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


def _prompt_choice(prompt: str, max_val: int) -> int:
    """Prompt for an integer in [1, max_val]. Loops until valid."""
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            print()
            sys.exit(1)
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            print(f"  Not a number: {raw!r}", file=sys.stderr)
            continue
        if 1 <= n <= max_val:
            return n
        print(f"  Out of range (1..{max_val}).", file=sys.stderr)


async def _scan_for_obd_devices(scan_timeout: float = 10.0) -> list[BLEDevice]:
    """Scan for BLE devices whose name advertises OBD or ELM327."""
    print(f"Scanning for BLE devices ({scan_timeout}s)...")
    try:
        devices = await BleakScanner.discover(timeout=scan_timeout)
    except Exception as err:  # noqa: BLE001
        # Bleak raises a variety of errors depending on the platform
        # (DBus connect failures on Linux, WinRT errors on Windows, etc).
        # Surface a friendly message so the user knows it's a BLE-stack
        # issue, not a library bug.
        print(f"BLE scan failed: {err}", file=sys.stderr)
        print(
            "Make sure Bluetooth is enabled and the adapter is in range.",
            file=sys.stderr,
        )
        return []
    matches: list[BLEDevice] = []
    for dev in devices:
        name = (dev.name or "").upper()
        if "OBD" in name or "ELM327" in name:
            matches.append(dev)
    if not matches:
        print("No OBD/ELM327 devices found in scan.")
        if devices:
            print(
                "All discovered devices (in case your adapter uses a different name):"
            )
            for dev in devices:
                print(f"  {dev.address}  {dev.name!r}")
        return []
    matches.sort(key=lambda d: (d.name or "", d.address))
    return matches


def _pick_device(devices: list[BLEDevice]) -> BLEDevice:
    """Print a numbered list and let the user pick one."""
    print("\nDiscovered OBD/ELM327 adapters:")
    for i, dev in enumerate(devices, start=1):
        rssi = getattr(dev, "rssi", "?")
        print(f"  {i}. {dev.name!r}  [{dev.address}]  RSSI={rssi}")
    n = _prompt_choice("Pick a device (number): ", len(devices))
    return devices[n - 1]


async def _pick_wican_profile() -> ProfileConfig:
    """Fetch WiCAN profiles, let the user pick one, return a ProfileConfig."""
    print("\nFetching WiCAN profiles...")
    async with aiohttp.ClientSession() as session:
        profiles = await fetch_wican_profiles(session)
    if not profiles:
        print("  No WiCAN profiles available; using recommended defaults.")
        return ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS))
    car_models = sorted(profiles.keys())
    print(f"\nAvailable WiCAN profiles ({len(car_models)}):")
    for i, cm in enumerate(car_models, start=1):
        print(f"  {i}. {cm}")
    n = _prompt_choice("Pick a profile (number): ", len(car_models))
    raw = profiles[car_models[n - 1]]
    try:
        profile = import_wican_profile(raw)
    except Exception as err:  # noqa: BLE001
        print(f"  Failed to import profile: {err}", file=sys.stderr)
        return ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS))
    print(
        f"  Imported {len(profile.standard_pids)} standard + "
        f"{len(profile.custom_pids)} custom PIDs."
    )
    return profile


async def _pick_obdb_profile() -> ProfileConfig:
    """Fetch OBDb matrix, walk make -> model -> year, return a ProfileConfig."""
    print("\nFetching OBDb matrix (this can take a few seconds)...")
    async with aiohttp.ClientSession() as session:
        matrix = await fetch_obdb_matrix(session)
    if not matrix:
        print("  OBDb matrix unavailable; using recommended defaults.")
        return ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS))

    makes = sorted({make for make, _ in matrix})
    print(f"\nAvailable makes ({len(makes)}):")
    for i, m in enumerate(makes, start=1):
        print(f"  {i}. {m}")
    n = _prompt_choice("Pick a make (number): ", len(makes))
    make = makes[n - 1]

    models = sorted({m for mk, m in matrix if mk == make})
    print(f"\n{make} models ({len(models)}):")
    for i, m in enumerate(models, start=1):
        print(f"  {i}. {m}")
    n = _prompt_choice("Pick a model (number): ", len(models))
    model = models[n - 1]
    signals = matrix[(make, model)]
    print(f"\n{make} {model}: {len(signals)} signals in matrix.")

    # Collect candidate years from matrix signal modelYears.
    years: set[int] = set()
    for sig in signals:
        my = sig.get("modelYears")
        if isinstance(my, list) and my:
            try:
                if len(my) == 1:
                    years.add(int(my[0]))
                else:
                    lo, hi = int(my[0]), int(my[-1])
                    years.update(range(lo, hi + 1))
            except (ValueError, TypeError):
                continue
    selected_year: int | None = None
    if years:
        sorted_years = sorted(years)
        print(f"\n{make} {model} model years (from matrix):")
        for i, y in enumerate(sorted_years, start=1):
            print(f"  {i}. {y}")
        print(f"  {len(sorted_years) + 1}. All years (no filter)")
        n = _prompt_choice("Pick a year (number): ", len(sorted_years) + 1)
        if n <= len(sorted_years):
            selected_year = sorted_years[n - 1]

    print(f"\nFetching OBDb repo for {make} {model}...")
    async with aiohttp.ClientSession() as session:
        repo_default = await fetch_obdb_repo_default_json(session, make, model)
    if repo_default is None:
        print("  (no per-vehicle repo default.json — matrix-only import)")

    try:
        profile = import_obdb_profile(
            matrix_signals=signals,
            repo_default=repo_default,
            selected_year=selected_year,
        )
    except Exception as err:  # noqa: BLE001
        print(f"  Failed to import profile: {err}", file=sys.stderr)
        return ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS))

    print(f"  Imported {len(profile.custom_pids)} custom PIDs.")
    return profile


async def _pick_profile() -> ProfileConfig:
    """Ask the user which profile source to use; never returns None."""
    print("\nVehicle profile options:")
    print("  1. None (standard PIDs only — RECOMMENDED_DEFAULTS)")
    print("  2. Import from WiCAN")
    print("  3. Import from OBDb")
    choice = _prompt_choice("Pick an option (number): ", 3)
    if choice == 1:
        return ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS))
    if choice == 2:
        return await _pick_wican_profile()
    return await _pick_obdb_profile()


async def _probe_and_get_uuids(
    ble_device: BLEDevice,
) -> tuple[str, str, bool]:
    """Probe the adapter to confirm connectivity and discover UUIDs.

    Returns ``(uuid_write, uuid_read, atrv_supported)``.
    The UUIDs come from :class:`TransportBLE`'s dynamic GATT discovery
    (or the configured placeholders if they matched). ``atrv_supported``
    is True only if AT RV returned a parseable voltage.
    """
    loop = asyncio.get_running_loop()
    print(
        "\nProbing adapter (opens a BLE connection, queries AT RV, scans "
        "supported PIDs)..."
    )
    result = await asyncio.to_thread(
        probe_adapter,
        ble_device,
        loop,
        DEFAULT_UUID_WRITE,
        DEFAULT_UUID_READ,
        10.0,
    )
    if result.success is None:
        print("  Could not connect to adapter.", file=sys.stderr)
    elif result.success:
        print("  Connection OK (AT RV returned a parseable voltage).")
    else:
        print("  Connected, but AT RV did not return a parseable voltage.")
    print(f"  uuid_write = {result.uuid_write}")
    print(f"  uuid_read  = {result.uuid_read}")
    if result.scanned_supported:
        print(f"  Supported standard PIDs: {len(result.scanned_supported)}")
        for name in result.scanned_supported[:10]:
            print(f"    - {name}")
        if len(result.scanned_supported) > 10:
            print(f"    ... and {len(result.scanned_supported) - 10} more")
    atrv_supported = result.success is True
    return result.uuid_write, result.uuid_read, atrv_supported


async def run_setup_wizard(config_path: Path) -> dict[str, Any]:
    """Run the first-time setup wizard and save config.

    Returns the saved config dict.
    """
    print("\n=== elm327_obdii setup wizard ===")
    print(f"Config will be saved to: {config_path}")

    devices = await _scan_for_obd_devices()
    if not devices:
        print("\nNo OBD/ELM327 devices found. Exiting.", file=sys.stderr)
        sys.exit(1)
    device = _pick_device(devices)
    print(f"\nSelected: {device.name!r} [{device.address}]")

    profile = await _pick_profile()

    uuid_write, uuid_read, atrv_supported = await _probe_and_get_uuids(device)
    if not uuid_write or not uuid_read:
        print("  Could not determine adapter UUIDs. Exiting.", file=sys.stderr)
        sys.exit(1)

    poller_config = PollerConfig(
        profile=profile,
        atrv_supported=atrv_supported,
        voltage_check_enabled=True,
        voltage_on=DEFAULT_VOLTAGE_ON,
        voltage_off=DEFAULT_VOLTAGE_OFF,
        grace_seconds=DEFAULT_GRACE_SECONDS,
    )

    config = _poller_config_to_dict(
        poller_config=poller_config,
        address=device.address,
        uuid_write=uuid_write,
        uuid_read=uuid_read,
    )
    save_config(config_path, config)
    print(f"\nConfig saved to {config_path}")
    print(f"  Address:        {device.address}")
    print(f"  uuid_write:     {uuid_write}")
    print(f"  uuid_read:      {uuid_read}")
    print(f"  atrv_supported: {atrv_supported}")
    print(f"  Standard PIDs:  {len(profile.standard_pids)}")
    print(f"  Custom PIDs:    {len(profile.custom_pids)}")
    return config


# ---------------------------------------------------------------------------
# PID listing & polling
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Format a sensor value for display using the library's formatter."""
    formatted = format_sensor_value(value)
    if formatted is None:
        return "<none>"
    return str(formatted)


def print_poll_result(result: PollResult, poll_number: int) -> None:
    """Print a poll result in a readable format."""
    print(f"\n--- poll #{poll_number} ---")
    print(f"state:    {result.state.value}")
    if result.voltage is not None:
        print(f"voltage:  {result.voltage:.2f} V")
    else:
        print("voltage:  <none>")
    print(f"success:  {result.any_success}")
    if not result.data:
        print("data:     <empty>")
        return
    print("data:")
    for key in sorted(result.data.keys()):
        print(f"  {key}: {_format_value(result.data[key])}")


def _state_interval(state: PollingState, override: float | None) -> float:
    """Return the poll interval for the given state, or the override if set."""
    if override is not None:
        return override
    return DEFAULT_INTERVALS.get(state, 10.0)


def list_pids(poller_config: PollerConfig) -> None:
    """Print all PIDs in the query plan (standard + custom)."""
    profile = poller_config.profile
    print(
        f"Profile: {len(profile.standard_pids)} standard, "
        f"{len(profile.custom_pids)} custom"
    )
    if profile.standard_pids:
        print("\nStandard PIDs (Mode 01):")
        for name in profile.standard_pids:
            print(f"  - {name}")
    if profile.custom_pids:
        print("\nCustom PIDs:")
        for pid in profile.custom_pids:
            header = pid.can_header or "default"
            filt = pid.can_filter or "none"
            extra = pid.init_extra or "none"
            print(
                f"  - {pid.name}\n"
                f"      mode={pid.mode} query={pid.query} "
                f"header={header} filter={filt} extra_init={extra} "
                f"unit={pid.unit or 'none'}"
            )
    if not profile.standard_pids and not profile.custom_pids:
        print("\n(no PIDs in profile)")


async def _find_device_by_address(
    address: str, scan_timeout: float = 10.0
) -> BLEDevice | None:
    """Find a BLEDevice by MAC address (re-scans if necessary)."""
    print(f"Looking for adapter at {address}...")
    try:
        return await BleakScanner.find_device_by_address(address, timeout=scan_timeout)
    except Exception as err:  # noqa: BLE001
        print(f"BLE scan failed: {err}", file=sys.stderr)
        return None


async def run_polling_loop(
    poller_config: PollerConfig,
    address: str,
    uuid_write: str,
    uuid_read: str,
    args: argparse.Namespace,
) -> None:
    """Connect to the adapter and poll in a loop (or once, or list PIDs)."""
    if args.list_pids:
        list_pids(poller_config)
        return

    ble_device = await _find_device_by_address(address)
    if ble_device is None:
        print(f"Could not find BLE device at {address}.", file=sys.stderr)
        return

    poller = Poller(poller_config)
    loop = asyncio.get_running_loop()

    print(f"Connecting to {ble_device.name!r} [{ble_device.address}]...")
    connected = await asyncio.to_thread(
        poller.connect, ble_device, loop, uuid_write, uuid_read
    )
    if not connected:
        print("Failed to connect. Exiting.", file=sys.stderr)
        return
    print("Connected. Starting poll loop.")

    poll_number = 0
    try:
        while True:
            poll_number += 1
            result = await asyncio.to_thread(poller.poll_once)
            print_poll_result(result, poll_number)
            if args.once:
                print("\n--once set; exiting after one poll.")
                break
            interval = _state_interval(result.state, args.interval)
            print(f"(sleeping {interval:.1f}s until next poll)")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        print("\nCancelled.")
        raise
    finally:
        print("\nDisconnecting...")
        await asyncio.to_thread(poller.disconnect)
        print("Disconnected.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_args(argv)
    _setup_logging(args.debug)

    config_path: Path = args.config
    if args.reconfigure and config_path.exists():
        print(f"Deleting old config at {config_path}")
        try:
            config_path.unlink()
        except OSError as err:
            _LOGGER.warning("Could not delete config: %s", err)

    config = load_config(config_path)

    if config is None:
        if args.list_pids:
            # No config yet — show the default standard PID set so users
            # can see what would be polled without a saved profile.
            print("No config found. Showing default standard PIDs:", file=sys.stderr)
            default_cfg = PollerConfig(
                profile=ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS)),
                atrv_supported=True,
                voltage_check_enabled=True,
                voltage_on=DEFAULT_VOLTAGE_ON,
                voltage_off=DEFAULT_VOLTAGE_OFF,
                grace_seconds=DEFAULT_GRACE_SECONDS,
            )
            list_pids(default_cfg)
            return 0
        config = asyncio.run(run_setup_wizard(config_path))
        if config is None:
            print("Setup failed.", file=sys.stderr)
            return 1

    poller_config = _poller_config_from_dict(config)
    if args.no_profile:
        # Override the saved profile with a standard-only fallback so
        # the user can test the adapter without a vehicle profile.
        poller_config = PollerConfig(
            profile=ProfileConfig(standard_pids=list(RECOMMENDED_DEFAULTS)),
            atrv_supported=poller_config.atrv_supported,
            voltage_check_enabled=poller_config.voltage_check_enabled,
            voltage_on=poller_config.voltage_on,
            voltage_off=poller_config.voltage_off,
            grace_seconds=poller_config.grace_seconds,
        )

    try:
        asyncio.run(
            run_polling_loop(
                poller_config=poller_config,
                address=config["address"],
                uuid_write=config["uuid_write"],
                uuid_read=config["uuid_read"],
                args=args,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

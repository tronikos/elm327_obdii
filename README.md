# elm327_obdii

[![PyPI Version](https://img.shields.io/pypi/v/elm327_obdii.svg)](https://pypi.org/project/elm327_obdii/)

A Python library for reading vehicle diagnostic data from ELM327 Bluetooth Low Energy (BLE) OBD-II adapters.

This library is used by the [ELM327 OBD-II BLE integration in Home Assistant](https://www.home-assistant.io/integrations/elm327_obdii_ble/).

## Features

- Connect to ELM327 BLE OBD-II adapters via Bluetooth
- Read standard OBD-II Mode 01 PIDs (engine speed, vehicle speed, coolant temperature, fuel level, etc.)
- Read vehicle-specific custom PIDs from OBDb and WiCAN community profiles
- Voltage-gated polling state machine to protect the vehicle's 12V auxiliary battery
- Structured `fmt` formula representation for signal decoding (bit extraction, linear scaling, enumerations)
- CAN context management (ATSH/ATCRA header/filter switching for multi-ECU vehicles)

## Data Sources

The library fetches vehicle profile data at runtime from:

- [OBDb](https://obdb.community) — community-maintained database of 160+ vehicle models with 11,000+ signal definitions. Licensed under [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- [WiCAN](https://github.com/meatpiHQ/wican-fw) — meatpiHQ/wican-fw repository with 77 vehicle profiles. Licensed under [GPL-3.0](https://www.gnu.org/licenses/gpl-3.0.html).

The fetched profile data is subject to those licenses. The library code itself is Apache 2.0 and does not distribute any profile data.

## Installation

```bash
pip install elm327_obdii
```

## Usage

```python
import asyncio
from elm327_obdii import Poller, PollerConfig, ProfileConfig

async def main():
    config = PollerConfig(
        profile=ProfileConfig(),
        atrv_supported=True,
        voltage_check_enabled=True,
        voltage_on=13.1,
        voltage_off=13.0,
        grace_seconds=30,
    )
    poller = Poller(config)

    # Connect to the adapter (provide a BLEDevice from bleak)
    ble_device = await bleak.BleakScanner.find_device_by_address("AA:BB:CC:DD:EE:FF")
    poller.connect(ble_device, asyncio.get_event_loop(), uuid_write, uuid_read)

    # Poll once
    result = poller.poll_once()
    print(f"State: {result.state}, Voltage: {result.voltage}")
    print(f"Data: {result.data}")

asyncio.run(main())
```

## CLI Usage

The library ships a standalone CLI tool for testing it directly against a
BLE OBD-II adapter — useful for development and debugging on any BLE-capable
machine. Run it as a module:

```bash
python -m elm327_obdii
```

### First run — setup wizard

On the first run (or when `~/.elm327_obdii_config.json` doesn't exist), the
CLI launches an interactive setup wizard:

1. **Scan** for BLE devices whose name advertises "OBD" or "ELM327".
2. **Pick** an adapter from a numbered list.
3. **Pick a vehicle profile**:
   - `None` — standard Mode 01 PIDs only (`RECOMMENDED_DEFAULTS`).
   - `WiCAN` — fetches the upstream `vehicle_profiles.json` and lets you
     pick a car model.
   - `OBDb` — fetches the OBDb matrix, then walks you through
     make → model → year selection.
4. **Probe** the adapter to confirm AT RV works and to discover the BLE
   GATT UUIDs (falls back to dynamic discovery if the defaults don't match).
5. **Save** the config (address, UUIDs, voltage thresholds, profile) to
   `~/.elm327_obdii_config.json` so subsequent runs skip the wizard.

### Subsequent runs — polling loop

Once a config exists, the CLI loads it, connects to the adapter, and polls
in a loop. Each poll prints the state-machine state, battery voltage, and
the decoded value of every PID in the query plan:

```raw
--- poll #1 ---
state:    car_on
voltage:  14.23 V
success:  True
data:
  ENGINE_SPEED: 1500.0
  FUEL_LEVEL: 87.5
(sleeping 10.0s until next poll)
```

The poll interval adapts to the state machine: ~10s while the car is on,
~5 min once the voltage gate declares the car off (to protect the 12V
auxiliary battery). Press `Ctrl+C` to disconnect and exit.

### Command-line flags

| Flag | Description |
| --- | --- |
| `-d`, `--debug` | Enable `DEBUG`-level logging for the `elm327_obdii` and `obdii` loggers. |
| `-c PATH`, `--config PATH` | Path to the config file (default: `~/.elm327_obdii_config.json`). |
| `--reconfigure` | Delete the existing config and re-run the setup wizard. |
| `--no-profile` | Ignore the saved profile and use only `RECOMMENDED_DEFAULTS` (standard Mode 01 PIDs). |
| `--interval SECONDS` | Override the poll interval (default: from the state machine). |
| `--once` | Poll once and exit instead of looping. |
| `--list-pids` | List every PID in the query plan (standard + custom) and exit — no BLE connection needed. |

### Examples

```bash
# First run: launch the setup wizard
python -m elm327_obdii

# Poll once and exit (smoke test)
python -m elm327_obdii --once

# Verbose logging for debugging the state machine / query plan
python -m elm327_obdii --debug

# List the PIDs that would be polled, without connecting
python -m elm327_obdii --list-pids

# Poll every 2 seconds regardless of state
python -m elm327_obdii --interval 2

# Re-run the setup wizard (e.g. to switch adapters or vehicles)
python -m elm327_obdii --reconfigure

# Use a non-default config path
python -m elm327_obdii --config /tmp/elm327_test.json
```

### Config file format

The config file is plain JSON and may be edited by hand:

```json
{
  "address": "AA:BB:CC:DD:EE:FF",
  "uuid_read": "0000fff1-0000-1000-8000-00805f9b34fb",
  "uuid_write": "0000fff2-0000-1000-8000-00805f9b34fb",
  "atrv_supported": true,
  "voltage_check": true,
  "voltage_on": 13.1,
  "voltage_off": 13.0,
  "grace_seconds": 30,
  "profile": {
    "standard_pids": ["FUEL_LEVEL"],
    "custom_pids": [
      {
        "id": "egolf-soc-bms",
        "name": "SOC BMS",
        "mode": "22",
        "query": "028C1",
        "fmt": {"bix": 32, "len": 8, "div": 2.5},
        "can_header": "7E5",
        "can_filter": "7ED",
        "unit": "%",
        "device_class": "battery",
        "state_class": "measurement"
      }
    ]
  }
}
```

The `profile` block is the serialized form of `ProfileConfig.to_dict()` —
the same shape the library uses for Home Assistant config entries — so
profiles built via the wizard or imported from WiCAN/OBDb are stored
verbatim and don't need to be re-fetched on subsequent runs.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
# for Windows CMD:
# .venv\Scripts\activate.bat
# for Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -e ".[test]"
pytest

python -m pip install pre-commit
pre-commit install
pre-commit run --all-files
```

### Live tests

Some tests require network access to fetch live vehicle profiles from OBDb and WiCAN:

```bash
pytest --run-network
```

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.

The test fixtures in `tests/fixtures/obdb_egolf/` are sourced from the
[OBDb Volkswagen e-Golf repository](https://github.com/OBDb/Volkswagen-e-Golf)
and are licensed under [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
See `tests/fixtures/obdb_egolf/README.md` for details.

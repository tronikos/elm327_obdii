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

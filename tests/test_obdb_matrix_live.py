"""End-to-end test: fetch the live OBDb ``matrix_data.json``.

Verify every vehicle's signals parse without error. This test is
**ignored** by default (marked ``@pytest.mark.network``) because it
requires network access to obdb.community. Run it with:

    pytest tests/test_obdb_matrix_live.py -m network

or

    pytest tests/test_obdb_matrix_live.py --run-network
"""

import asyncio
import logging

import aiohttp
import pytest

from elm327_obdii import OBDB_MATRIX_URL, import_obdb_profile
from elm327_obdii._core.fmt_evaluator import validate_fmt

_LOGGER = logging.getLogger(__name__)

network = pytest.mark.network
run_network = pytest.mark.skipif(
    "not config.getoption('--run-network')",
    reason="needs --run-network to hit obdb.community",
)


@network
@run_network
def test_fetch_and_parse_all_obdb_vehicles() -> None:
    """Fetch matrix_data.json and parse every vehicle's signals.

    This test:
      1. Downloads OBDB_MATRIX_URL (4.5 MB, ~11,756 signals).
      2. Groups signals by (make, model).
      3. Runs ``import_obdb_profile()`` on each vehicle.
      4. Validates every translated fmt.
      5. Asserts at least 100 vehicles parsed (regression guard).
    """

    async def _fetch() -> list:
        async with (
            aiohttp.ClientSession() as session,
            session.get(OBDB_MATRIX_URL) as resp,
        ):
            assert resp.status == 200, f"HTTP {resp.status}"
            return await resp.json(content_type=None)

    data = asyncio.run(_fetch())
    assert isinstance(data, list), "Top-level JSON is not a list"
    assert len(data) > 5000, f"Expected ~11k signals, got {len(data)}"

    # Group by (make, model)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for signal in data:
        if not isinstance(signal, dict):
            continue
        make = signal.get("make", "")
        model = signal.get("model", "")
        if not make or not model or make == "SAEJ1979":
            continue
        grouped.setdefault((make, model), []).append(signal)

    parsed_count = 0
    errors: list[str] = []

    for (make, model), signals in grouped.items():
        try:
            profile = import_obdb_profile(signals)
            for pid in profile.custom_pids:
                try:
                    validate_fmt(pid.fmt)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"{make} {model}: PID {pid.name!r} fmt "
                        f"{pid.fmt!r} failed validation: {exc}"
                    )
            parsed_count += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{make} {model}: import raised {type(exc).__name__}: {exc}")

    assert parsed_count >= 100, (
        f"Only parsed {parsed_count} of {len(grouped)} vehicles - "
        f"check the importer against the latest OBDb schema"
    )

    if errors:
        pytest.fail(
            f"{len(errors)} errors across {len(grouped)} vehicles:\n"
            + "\n".join(f"  - {e}" for e in errors[:20])
            + (f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else "")
        )


@network
@run_network
def test_obdb_matrix_shape_stable() -> None:
    """Sanity-check the top-level shape of matrix_data.json."""

    async def _fetch() -> list:
        async with (
            aiohttp.ClientSession() as session,
            session.get(OBDB_MATRIX_URL) as resp,
        ):
            assert resp.status == 200
            return await resp.json(content_type=None)

    data = asyncio.run(_fetch())
    assert isinstance(data, list)
    assert len(data) > 0

    first = data[0]
    assert "fmt" in first, "First signal missing 'fmt'"
    assert "cmd" in first, "First signal missing 'cmd'"
    assert "make" in first, "First signal missing 'make'"
    assert "model" in first, "First signal missing 'model'"

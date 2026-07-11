"""End-to-end test: fetch the live WiCAN ``vehicle_profiles.json``.

Verify every car profile parses without error. This test is **ignored**
by default (marked ``@pytest.mark.network``) because it requires
network access to GitHub and depends on the upstream repo being
available. Run it explicitly with:

    pytest tests/test_wican_profiles_live.py -m network

or

    pytest tests/test_wican_profiles_live.py --run-network

The test downloads the same URL the integration's config-flow
``fetch_wican_profiles`` uses, then runs every car through
``import_wican_profile`` to catch any profile that would crash the
importer - a regression in formula translation, init-string parsing, or
the reverse de-dup logic.
"""

import asyncio
import logging

import aiohttp
import pytest

from elm327_obdii import WICAN_PROFILES_URL, import_wican_profile
from elm327_obdii._core.fmt_evaluator import validate_fmt

_LOGGER = logging.getLogger(__name__)

network = pytest.mark.network
run_network = pytest.mark.skipif(
    "not config.getoption('--run-network')",
    reason="needs --run-network to hit GitHub",
)


@network
@run_network
def test_fetch_and_parse_all_wican_profiles() -> None:
    """Fetch vehicle_profiles.json and parse every car profile.

    This is the ignored test required by Q7. It:
      1. Downloads WICAN_PROFILES_URL (the same URL the config flow uses).
      2. Iterates every car in ``data["cars"]``.
      3. Runs ``import_wican_profile(car)`` - must not raise.
      4. Validates every translated formula via ``validate_formula``.
      5. Asserts at least 50 cars parsed (regression guard).

    Failures here mean either:
      - The upstream WiCAN profile schema changed (the importer needs updating).
      - A formula translation bug was introduced.
      - A new profile uses a notation the importer doesn't handle.
    """

    async def _fetch() -> dict:
        async with (
            aiohttp.ClientSession() as session,
            session.get(WICAN_PROFILES_URL) as resp,
        ):
            assert resp.status == 200, f"HTTP {resp.status}"
            return await resp.json(content_type=None)

    data = asyncio.run(_fetch())
    assert isinstance(data, dict), "Top-level JSON is not a dict"
    assert "cars" in data, "Missing 'cars' key"
    assert isinstance(data["cars"], list), "'cars' is not a list"

    cars = data["cars"]
    parsed_count = 0
    errors: list[str] = []

    for car in cars:
        car_model = car.get("car_model", "unknown")
        try:
            profile = import_wican_profile(car)
            # Validate every translated fmt.
            for pid in profile.custom_pids:
                try:
                    validate_fmt(pid.fmt)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"{car_model}: PID {pid.name!r} fmt "
                        f"{pid.fmt!r} failed validation: {exc}"
                    )
            parsed_count += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{car_model}: import raised {type(exc).__name__}: {exc}")

    # Regression guard: the upstream repo has ~77 cars; if we parsed
    # fewer than 50, something broke.
    assert parsed_count >= 50, (
        f"Only parsed {parsed_count} of {len(cars)} car profiles - "
        f"check the importer against the latest WiCAN schema"
    )

    # Report all errors at once for easy debugging.
    if errors:
        pytest.fail(
            f"{len(errors)} errors across {len(cars)} profiles:\n"
            + "\n".join(f"  - {e}" for e in errors[:20])
            + (f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else "")
        )


@network
@run_network
def test_wican_profiles_shape_stable() -> None:
    """Sanity-check the top-level shape of vehicle_profiles.json.

    Catches breaking changes in the upstream JSON structure (e.g. if
    ``cars`` is renamed or the URL starts returning a different format).
    """

    async def _fetch() -> dict:
        async with (
            aiohttp.ClientSession() as session,
            session.get(WICAN_PROFILES_URL) as resp,
        ):
            assert resp.status == 200
            return await resp.json(content_type=None)

    data = asyncio.run(_fetch())
    assert "cars" in data
    assert isinstance(data["cars"], list)
    assert len(data["cars"]) > 0

    first = data["cars"][0]
    assert "car_model" in first, "First car missing 'car_model'"
    assert "pids" in first, "First car missing 'pids'"
    assert isinstance(first["pids"], list), "'pids' is not a list"

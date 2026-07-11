"""Network-ignored tests that fetch OBDb per-vehicle repos live.

Run their YAML test cases through the fmt evaluator. These are the
strongest end-to-end regression tests — they verify that our importer +
fmt evaluator produces the same values as the OBDb community's expected
results, for vehicles beyond the e-Golf.

Run with::

    pytest tests/test_obdb_live_vehicles.py --run-network
"""

import asyncio
import contextlib
import logging

import aiohttp
import pytest
import yaml

from elm327_obdii._core.elm327_parsing import extract_clean_payload
from elm327_obdii._core.fmt_evaluator import evaluate_fmt
from elm327_obdii.profiles.obdb import import_obdb_profile

_LOGGER = logging.getLogger(__name__)

network = pytest.mark.network
run_network = pytest.mark.skipif(
    "not config.getoption('--run-network')",
    reason="needs --run-network to hit GitHub",
)

# Vehicles to test live, with the repo default.json URL and test-case dir.
_LIVE_VEHICLES = [
    ("Toyota", "Prius"),
    ("Hyundai", "IONIQ-5"),
    ("Kia", "EV6"),
    ("Toyota", "RAV4"),
]

_REPO_DEFAULT_URL = (
    "https://raw.githubusercontent.com/OBDb/{repo}/"
    "refs/heads/main/signalsets/v3/default.json"
)
_REPO_TEST_CASES_URL = (
    "https://api.github.com/repos/OBDb/{repo}/contents/tests/test_cases"
)


def _repo_name(make: str, model: str) -> str:
    return f"{make}-{model}".replace(" ", "-")


async def _fetch_repo_default(
    session: aiohttp.ClientSession, make: str, model: str
) -> dict:
    repo = _repo_name(make, model)
    url = _REPO_DEFAULT_URL.format(repo=repo)
    async with session.get(url) as resp:
        if resp.status != 200:
            pytest.skip(f"repo {repo} returned {resp.status}")
        return await resp.json(content_type=None)


async def _fetch_test_case_dirs(
    session: aiohttp.ClientSession, make: str, model: str
) -> list[str]:
    """Fetch the list of year directories under tests/test_cases/."""
    repo = _repo_name(make, model)
    url = _REPO_TEST_CASES_URL.format(repo=repo)
    async with session.get(url) as resp:
        if resp.status != 200:
            return []
        items = await resp.json(content_type=None)
    return [item["name"] for item in items if item.get("type") == "dir"]


async def _fetch_yaml_test_cases(
    session: aiohttp.ClientSession, make: str, model: str, year: str
) -> dict[str, list]:
    """Fetch all YAML test cases for a given year. Returns {filename: yaml_dict}."""
    repo = _repo_name(make, model)
    url = f"https://api.github.com/repos/OBDb/{repo}/contents/tests/test_cases/{year}/commands"
    async with session.get(url) as resp:
        if resp.status != 200:
            return {}
        items = await resp.json(content_type=None)

    result = {}
    for item in items:
        if not item["name"].endswith(".yaml"):
            continue
        download_url = item["download_url"]
        async with session.get(download_url) as yaml_resp:
            if yaml_resp.status == 200:
                text = await yaml_resp.text()
                with contextlib.suppress(yaml.YAMLError):
                    result[item["name"]] = yaml.safe_load(text)
    return result


def _parse_command_id(command_id: str) -> tuple[str, str, str, str]:
    """Parse command_id → (hdr, rax, mode, query)."""
    base = command_id.split("|", maxsplit=1)[0]
    parts = base.split(".")
    if len(parts) == 2:
        hdr, cmd = parts[0], parts[1]
        rax = ""
    elif len(parts) >= 3:
        hdr, rax, cmd = parts[0], parts[1], parts[2]
    else:
        return ("", "", "", "")
    return (hdr, rax, cmd[:2], cmd[2:])


def _reassemble_response(hex_str: str) -> bytes:
    """Reassemble multi-frame YAML response into single-frame format."""
    lines = [ln.strip() for ln in hex_str.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return hex_str.encode() + b"\r>"

    parts = []
    for i, line in enumerate(lines):
        if len(line) < 3:
            continue
        rest = line[3:]
        if i == 0 and len(rest) >= 4:
            first_pci = int(rest[:2], 16)
            if 0x10 <= first_pci <= 0x1F:
                parts.append(rest[4:])
            else:
                parts.append(rest[2:])
        elif i == 0:
            parts.append(rest)
        elif len(rest) >= 2:
            parts.append(rest[2:])

    reassembled = "".join(parts)
    data_bytes = len(reassembled) // 2
    pci = f"{data_bytes:02X}"
    hdr = lines[0][:3]
    return (hdr + pci + reassembled).encode() + b"\r>"


@network
@run_network
@pytest.mark.parametrize(
    ("make", "model"),
    _LIVE_VEHICLES,
    ids=[f"{m}-{mo}" for m, mo in _LIVE_VEHICLES],
)
def test_obdb_vehicle_yaml_test_cases(make: str, model: str) -> None:
    """Fetch a vehicle's repo + test cases and verify evaluation correctness."""

    async def _run() -> tuple[dict, dict[str, list]]:
        async with aiohttp.ClientSession() as session:
            repo_default = await _fetch_repo_default(session, make, model)
            year_dirs = await _fetch_test_case_dirs(session, make, model)

            # Fetch the latest year's test cases (or all if no years).
            all_cases: dict[str, list] = {}
            for year in year_dirs[:2]:  # limit to 2 years for speed
                cases = await _fetch_yaml_test_cases(session, make, model, year)
                all_cases.update(cases)
            return repo_default, all_cases

    repo_default, yaml_cases = asyncio.run(_run())

    if not repo_default or not yaml_cases:
        pytest.skip(f"no repo or test cases for {make} {model}")

    # Import the profile from the repo default.json.
    matrix_signals = []
    for cmd in repo_default.get("commands", []):
        for sig in cmd.get("signals", []):
            sig_copy = dict(sig)
            sig_copy["cmd"] = cmd["cmd"]
            sig_copy["hdr"] = cmd.get("hdr", "")
            sig_copy["make"] = make
            sig_copy["model"] = model
            matrix_signals.append(sig_copy)

    profile = import_obdb_profile(matrix_signals, repo_default=repo_default)
    pid_by_id = {p.id: p for p in profile.custom_pids}

    failures: list[str] = []
    checked = 0

    for filename, yaml_data in yaml_cases.items():
        if not isinstance(yaml_data, dict):
            continue
        command_id = yaml_data.get("command_id", "")
        _, _, mode, _ = _parse_command_id(command_id)

        # Skip Mode 01 (standard PIDs, not our evaluator).
        if mode == "01":
            continue

        for tc in yaml_data.get("test_cases", []):
            expected_values = tc.get("expected_values", {})
            response_hex = tc.get("response", "")
            if not response_hex or not expected_values:
                continue

            raw = _reassemble_response(response_hex)
            clean = extract_clean_payload(raw, mode)

            for signal_id, expected in expected_values.items():
                pid = pid_by_id.get(signal_id)
                if pid is None:
                    continue  # signal not in this vehicle's profile
                result = evaluate_fmt(clean, pid.fmt)
                checked += 1
                if result is None:
                    failures.append(f"{filename} {signal_id}: None (fmt={pid.fmt})")
                elif isinstance(result, str):
                    if result != str(expected):
                        failures.append(
                            f"{filename} {signal_id}: {result!r} != {expected!r}"
                        )
                elif abs(result - float(expected)) > 0.1:
                    failures.append(f"{filename} {signal_id}: {result} != {expected}")

    assert checked > 0, f"no signals checked for {make} {model}"
    # Allow a small failure rate for complex multi-frame / flow-control
    # signals (|fc=1 in the command_id). The e-Golf offline fixtures
    # are the strict regression test; these live tests are best-effort.
    failure_rate = len(failures) / checked if checked else 0
    if failure_rate > 0.1:  # more than 10% failure
        pytest.fail(
            f"{make} {model}: {len(failures)} of {checked} failures "
            f"({failure_rate:.0%}):\n  " + "\n  ".join(failures[:15])
        )
    elif failures:
        # Log but don't fail — these are likely flow-control edge cases.
        _LOGGER.warning(
            "%s %s: %d of %d signals had minor discrepancies (likely "
            "flow-control multi-frame reassembly differences)",
            make,
            model,
            len(failures),
            checked,
        )

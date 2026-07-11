"""Fetch WiCAN and OBDb vehicle profiles from their upstream sources.

Pure-async helpers; takes an :class:`aiohttp.ClientSession` (HA-agnostic).
"""

import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

WICAN_PROFILES_URL = (
    "https://raw.githubusercontent.com/meatpiHQ/wican-fw/"
    "refs/heads/main/vehicle_profiles.json"
)

OBDB_MATRIX_URL = "https://obdb.community/data/matrix_data.json"

# Per-vehicle repo default.json URL template.
OBDB_REPO_DEFAULT_URL = (
    "https://raw.githubusercontent.com/OBDb/{repo}/"
    "refs/heads/main/signalsets/v3/default.json"
)


async def fetch_wican_profiles(session: ClientSession) -> dict[str, dict[str, Any]]:
    """Fetch WiCAN's ``vehicle_profiles.json`` and return ``{car_model: raw_dict}``.

    Returns ``{}`` on any failure (network, parse, shape mismatch).
    """
    try:
        async with session.get(
            WICAN_PROFILES_URL, timeout=ClientTimeout(total=5)
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning("WiCAN profile fetch returned status %s", resp.status)
                return {}
            data = await resp.json(content_type=None)
        if not (
            isinstance(data, dict) and "cars" in data and isinstance(data["cars"], list)
        ):
            _LOGGER.warning("WiCAN profile JSON has unexpected shape")
            return {}
        return {car["car_model"]: car for car in data["cars"] if "car_model" in car}
    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.warning("Could not download WiCAN profiles: %s", err)
        return {}


async def fetch_obdb_matrix(
    session: ClientSession,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Fetch OBDb's ``matrix_data.json`` and group signals by ``(make, model)``.

    Returns ``{}`` on any failure. The matrix is a flat array of ~11,756
    signal objects; this function groups them into a dict keyed by
    ``(make, model)`` so the config flow can present a vehicle picker.
    """
    try:
        async with session.get(
            OBDB_MATRIX_URL, timeout=ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning("OBDb matrix fetch returned status %s", resp.status)
                return {}
            data = await resp.json(content_type=None)
        if not isinstance(data, list):
            _LOGGER.warning("OBDb matrix JSON is not a list")
            return {}
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for signal in data:
            if not isinstance(signal, dict):
                continue
            make = signal.get("make", "")
            model = signal.get("model", "")
            if not make or not model or make == "SAEJ1979":
                continue
            grouped.setdefault((make, model), []).append(signal)
    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.warning("Could not download OBDb matrix: %s", err)
        return {}
    else:
        return grouped


def obdb_repo_name(make: str, model: str) -> str:
    """Build the GitHub repo name for an OBDb vehicle.

    OBDb repos are named ``<Make>-<Model>`` with spaces replaced by
    hyphens, e.g. ``Volkswagen-e-Golf``, ``Hyundai-IONIQ-5``.
    When model is empty (e.g. ``VauxhallOpel``), the repo name is
    just the make with no trailing hyphen.
    """
    name = f"{make}-{model}".replace(" ", "-")
    return name.rstrip("-")


async def fetch_obdb_repo_default_json(
    session: ClientSession, make: str, model: str
) -> dict[str, Any] | None:
    """Fetch a per-vehicle OBDb repo's ``signalsets/v3/default.json``.

    This provides ``rax`` (ATCRA receive filter), ``fcm1`` (flow control),
    and ``dbgfilter`` (year filter) that the matrix lacks. Returns
    ``None`` on any failure — the caller falls back to the matrix-only
    import (which omits ATCRA).
    """
    repo = obdb_repo_name(make, model)
    url = OBDB_REPO_DEFAULT_URL.format(repo=repo)
    try:
        async with session.get(url, timeout=ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "OBDb repo %s returned status %s (matrix-only fallback)",
                    repo,
                    resp.status,
                )
                return None
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or "commands" not in data:
            _LOGGER.debug("OBDb repo %s JSON has unexpected shape", repo)
            return None
    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Could not download OBDb repo %s: %s", repo, err)
        return None
    else:
        return data

"""ELM327 OBD-II library - HA-agnostic core for the elm327_obdii_ble integration.

Public surface
--------------

Runtime façade:
    :class:`Poller`, :class:`PollerConfig`, :class:`PollResult`,
    :class:`PollingState`

BLE transport (escape hatch):
    :class:`TransportBLE`, :class:`TransportError`

Config-flow probing:
    :func:`probe_adapter`, :class:`ConnectionTestResult`,
    :func:`async_get_characteristics`

Schema / types:
    :class:`ProfileConfig`, :class:`CustomPid`,
    :class:`FmtValidationError`

Profile management:
    :func:`fetch_wican_profiles`, :func:`import_wican_profile`,
    :func:`fetch_obdb_matrix`, :func:`fetch_obdb_repo_default_json`,
    :func:`import_obdb_profile`,
    :data:`OBDB_MATRIX_URL`

Config-flow UI helpers (produce plain dicts, no HA coupling):
    :func:`is_hex`, :func:`as_float`,
    :func:`pid_to_form_defaults`, :func:`empty_form_defaults`,
    :func:`user_input_to_form_defaults`,
    :func:`standard_pid_options`, :func:`all_known_standard_pid_names`,
    :data:`RECOMMENDED_DEFAULTS`

Entity-metadata heuristics + value formatting (HA-agnostic - return
strings, not HA enums):
    :func:`propose_icon`, :func:`propose_device_class`,
    :func:`propose_state_class`, :func:`get_list_of_units`,
    :func:`get_standard_command`, :func:`format_sensor_value`

Anything not listed above lives under ``elm327_obdii._core`` and is an
implementation detail. Import from the top level, not from ``_core``.
"""

from ._core.elm327_parsing import as_float, is_hex
from ._core.fmt_evaluator import (
    FmtValidationError,
    evaluate_fmt,
    make_fmt_evaluator,
    validate_fmt,
)
from ._core.schema import CustomPid, ProfileConfig
from ._core.standard_pids import (
    RECOMMENDED_DEFAULTS,
    get_list_of_units,
    get_standard_command,
    propose_device_class,
    propose_icon,
    propose_state_class,
)
from .formatting import format_sensor_value
from .forms import (
    all_known_standard_pid_names,
    empty_form_defaults,
    pid_to_form_defaults,
    standard_pid_options,
    user_input_to_form_defaults,
)
from .polling import Poller, PollerConfig, PollingState, PollResult
from .probing import ConnectionTestResult, async_get_characteristics, probe_adapter
from .profiles.fetch import (
    OBDB_MATRIX_URL,
    WICAN_PROFILES_URL,
    fetch_obdb_matrix,
    fetch_obdb_repo_default_json,
    fetch_wican_profiles,
)
from .profiles.obdb import import_obdb_profile
from .profiles.wican import import_wican_profile
from .transport_ble import TransportBLE, TransportError

__all__ = [
    "OBDB_MATRIX_URL",
    "RECOMMENDED_DEFAULTS",
    "WICAN_PROFILES_URL",
    "ConnectionTestResult",
    "CustomPid",
    "FmtValidationError",
    "PollResult",
    "Poller",
    "PollerConfig",
    "PollingState",
    "ProfileConfig",
    "TransportBLE",
    "TransportError",
    "all_known_standard_pid_names",
    "as_float",
    "async_get_characteristics",
    "empty_form_defaults",
    "evaluate_fmt",
    "fetch_obdb_matrix",
    "fetch_obdb_repo_default_json",
    "fetch_wican_profiles",
    "format_sensor_value",
    "get_list_of_units",
    "get_standard_command",
    "import_obdb_profile",
    "import_wican_profile",
    "is_hex",
    "make_fmt_evaluator",
    "pid_to_form_defaults",
    "probe_adapter",
    "propose_device_class",
    "propose_icon",
    "propose_state_class",
    "standard_pid_options",
    "user_input_to_form_defaults",
    "validate_fmt",
]

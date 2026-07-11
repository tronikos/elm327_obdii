"""Profile schema - pure dataclasses for the user's PID configuration.

No I/O, no Home Assistant imports, no obdii dependency. These types are
the JSON-serialized payload stored in
``config_entry.options[CONF_PROFILE]``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CustomPid:
    """One proprietary (non-standard-Mode-1) parameter.

    The ``id`` is independent of ``name`` so the user can rename a PID
    without orphaning its entity history. Generate once at creation
    time (e.g. ``uuid.uuid4().hex``); never change for the lifetime of
    that PID. Built-in profiles use stable string ids like
    ``"egolf-soc-bms"`` so entity unique-ids survive HA restarts.

    The ``fmt`` dict is the structured formula representation (matching
    OBDb's signal format). It contains bit-extraction fields (``bix``,
    ``len``), linear scaling (``mul``, ``div``, ``add``), signedness
    (``sign``), byte-swap for Intel byte order (``blsb``), clamping
    (``min``, ``max``), null sentinels (``nullmin``, ``nullmax``), and
    enumerations (``map``). See :mod:`elm327_obdii._core.fmt_evaluator`
    for the full specification and evaluation order.
    """

    id: str
    name: str
    mode: str  # hex mode byte as text, e.g. "01", "22"
    query: str  # hex PID/DID payload, e.g. "0C", "028C1"
    fmt: dict[str, Any]  # structured formula — see _core/fmt_evaluator.py
    can_header: str | None = None  # ATSH target, e.g. "7E5"; None = adapter default
    can_filter: str | None = (
        None  # ATCRA expected reply id, e.g. "7ED"; None = no filter
    )
    init_extra: str | None = None  # escape hatch: extra raw AT cmds this PID needs
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    model_years: list[int] | None = None  # OBDb year filter; None = all years
    expected_bytes: int = 0  # for ELM327 early-return optimization (0 = disabled)
    source: str = "manual"  # provenance: "manual" | "import:wican:<car_model>" | "import:obdb:<make:model>" | "builtin"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for storage in HA config entries."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomPid:
        """Deserialize from a stored dict, ignoring unknown keys for forward-compat."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ProfileConfig:
    """The whole tracked PID set, isolated from any upstream schema.

    Stored as ``config_entry.options[CONF_PROFILE]``. Standard Mode 01
    PIDs are referenced by canonical obdii command name; custom PIDs
    carry their own mode/query/formula.
    """

    standard_pids: list[str] = field(default_factory=list)
    custom_pids: list[CustomPid] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for storage in HA config entries."""
        return {
            "standard_pids": list(self.standard_pids),
            "custom_pids": [p.to_dict() for p in self.custom_pids],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileConfig:
        """Deserialize from a stored dict."""
        return cls(
            standard_pids=list(data.get("standard_pids", [])),
            custom_pids=[CustomPid.from_dict(p) for p in data.get("custom_pids", [])],
        )

"""CAN-context-aware query plan builder.

Groups all queryable items (standard Mode 01 commands AND custom PIDs)
by their CAN context (header, filter, extra init). The poller walks
the resulting plan in order, switching ATSH/ATCRA only when the
context changes between groups - including transitioning back to the
default (header=None) context.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
from typing import Any, Protocol

from .can_context import CanContext
from .elm327_parsing import extract_clean_payload
from .schema import CustomPid

_LOGGER = logging.getLogger(__name__)


class QueryItem(Protocol):
    """A single queryable item, regardless of whether it's standard or custom.

    The poller doesn't care about the difference at schedule time;
    it only needs to know which context the item belongs to, what dict
    key to write the value to, and how to execute the query.
    """

    @property
    def context(self) -> CanContext:
        """Return the CAN context this item belongs to."""

    @property
    def key(self) -> str:
        """Return the dict key the poller stores the value under."""

    def execute(self, connection: Any) -> Any:
        """Execute the query and return the computed value, or None."""


@dataclass
class StandardQueryItem:
    """A standard Mode 01 PID query - uses py-obdii's Command + resolver.

    ``execute()`` returns the resolver's typed value verbatim - obdii
    resolvers return float (most PIDs), int (bitfields), list (O2
    sensors, supported-PID bitmaps), list-of-tuples (fuel system
    status), str (DTCs), or None. The poller stores whatever comes
    back; the sensor platform formats it for display.
    """

    command_name: str  # canonical obdii name, e.g. "ENGINE_SPEED"
    command: Any  # obdii.Command - built by the caller
    context: CanContext = field(default_factory=CanContext)

    @property
    def key(self) -> str:
        """Return the canonical obdii command name."""
        return self.command_name

    def execute(self, connection: Any) -> Any:
        """Query the standard PID and return the resolver's typed value."""
        resp = connection.query(self.command)
        if resp is None:
            _LOGGER.debug("Standard query %s: no response", self.command_name)
            return None
        raw = getattr(resp, "raw", None)
        _LOGGER.debug(
            "Standard query %s: raw=%s, value=%s",
            self.command_name,
            raw.decode(errors="ignore") if raw else None,
            resp.value,
        )
        if _is_buffer_full(resp):
            return None
        return resp.value


@dataclass
class CustomQueryItem:
    """A custom PID query - uses the structured ``fmt`` evaluator.

    Uses :func:`extract_clean_payload(resp.raw, mode)
    <elm327_obdii._core.elm327_parsing.extract_clean_payload>` to strip
    the CAN header, PCI byte, mode echo, and PID echo, leaving only the
    data bytes that ``fmt.bix`` offsets are measured against.
    """

    pid: CustomPid
    command: Any  # obdii.Command - built from pid.mode + pid.query
    evaluator: Callable[[list[int]], float | str | None]
    context: CanContext

    @property
    def key(self) -> str:
        """Return the custom PID's unique id (stable across renames)."""
        return self.pid.id

    def execute(self, connection: Any) -> float | str | None:
        """Query the custom PID, build the clean payload, evaluate the fmt."""
        resp = connection.query(self.command)
        if resp is None:
            _LOGGER.debug("Custom query %s: no response", self.pid.id)
            return None
        raw = getattr(resp, "raw", None)
        if not raw:
            _LOGGER.debug("Custom query %s: empty raw response", self.pid.id)
            return None
        if b"BUFFER FULL" in raw:
            _LOGGER.debug("Custom query %s: BUFFER FULL", self.pid.id)
            return None
        clean = extract_clean_payload(raw, self.pid.mode)
        _LOGGER.debug(
            "Custom query %s: raw=%s, clean=%s",
            self.pid.id,
            raw.decode(errors="ignore"),
            [f"{b:02X}" for b in clean] if clean else None,
        )
        if not clean:
            return None
        return self.evaluator(clean)


def build_query_plan(
    items: Iterable[QueryItem],
) -> list[tuple[CanContext, list[QueryItem]]]:
    """Group items by CAN context, ordered with the default context first.

    The default context (:class:`CanContext()` - all fields None) is
    always the first group, because:
      1. It's the cheapest (no ATSH/ATCRA setup needed).
      2. Standard Mode 01 PIDs always live here.
      3. Starting here matches the adapter's power-on state, so the
         poller can skip an unnecessary "transition to default" step
         at the top of each poll cycle.
    Other groups are sorted by (header, filter, extra_init) for
    deterministic ordering across runs.

    Within each group, items preserve their input order - callers that
    care about intra-group ordering (e.g. "fast-changing PIDs first")
    should pre-sort their input.
    """
    groups: dict[CanContext, list[QueryItem]] = defaultdict(list)
    for item in items:
        groups[item.context].append(item)

    default = CanContext()
    ordered: list[tuple[CanContext, list[QueryItem]]] = []
    if default in groups:
        ordered.append((default, groups.pop(default)))
    ordered.extend(
        (ctx, groups[ctx])
        for ctx in sorted(
            groups,
            key=lambda c: (c.header or "", c.filter or "", c.extra_init or ""),
        )
    )
    return ordered


def _is_buffer_full(resp: Any) -> bool:
    """True if the ELM327's internal buffer overflowed on this response.

    Common with fast multi-PID polling. The caller treats this as
    "no data this cycle" rather than an error.
    """
    raw = getattr(resp, "raw", None)
    return bool(raw and b"BUFFER FULL" in raw)

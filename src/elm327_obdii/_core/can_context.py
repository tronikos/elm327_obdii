"""CAN addressing context.

A :class:`CanContext` is a unique ELM327 addressing state - the
combination of CAN header (ATSH), receive filter (ATCRA), and any
extra init commands a custom PID needs. The query scheduler groups
items by context so ATSH/ATCRA are only re-issued when the context
actually changes between groups.
"""

from dataclasses import dataclass

from .schema import CustomPid


@dataclass(frozen=True)
class CanContext:
    """A unique ELM327 addressing state.

    ``header=None`` is an explicit, real value meaning "adapter default
    addressing" (no ATSH issued). It is NOT the absence of a value -
    the scheduler treats it as a context the poller must actively
    transition back to, so no stale header from a custom PID survives
    into the next cycle's standard-PID pass.

    Frozen so it can be used as a dict key for grouping.
    """

    header: str | None = None
    filter: str | None = None
    extra_init: str | None = None


def context_for_custom_pid(pid: CustomPid) -> CanContext:
    """Derive the CAN context for a custom PID from its schema fields."""
    return CanContext(
        header=pid.can_header,
        filter=pid.can_filter,
        extra_init=pid.init_extra,
    )

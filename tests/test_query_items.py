"""Tests for :mod:`elm327_obdii._core.query_items`.

Also covers :mod:`elm327_obdii._core.can_context`: CAN-context grouping,
the default-first ordering, and the QueryItem execute paths for standard
and custom PIDs (using the new ``fmt`` dict evaluator).
"""

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from elm327_obdii._core.can_context import CanContext, context_for_custom_pid
from elm327_obdii._core.fmt_evaluator import make_fmt_evaluator
from elm327_obdii._core.query_items import (
    CustomQueryItem,
    StandardQueryItem,
    build_query_plan,
)
from elm327_obdii._core.schema import CustomPid

_FMT = {"bix": 0, "len": 8}


def _make_pid(
    pid_id: str = "test", name: str = "Test", header: str | None = None
) -> CustomPid:
    """Build a minimal CustomPid with a valid fmt."""
    return CustomPid(
        id=pid_id,
        name=name,
        mode="22",
        query="FFFF",
        fmt=dict(_FMT),
        can_header=header,
    )


class TestCanContext:
    """CanContext equality and default-vs-custom distinction."""

    def test_default_context_is_all_none(self) -> None:
        """A default CanContext has all fields set to None."""
        ctx = CanContext()
        assert ctx.header is None
        assert ctx.filter is None
        assert ctx.extra_init is None

    def test_equal_contexts_are_equal(self) -> None:
        """Two CanContexts with the same fields compare equal."""
        a = CanContext(header="7E5", filter="7ED")
        b = CanContext(header="7E5", filter="7ED")
        assert a == b

    def test_different_contexts_not_equal(self) -> None:
        """CanContexts with differing headers compare unequal."""
        a = CanContext(header="7E5")
        b = CanContext(header="7E6")
        assert a != b

    def test_frozen(self) -> None:
        """CanContext is frozen: assigning a field raises FrozenInstanceError."""
        ctx = CanContext()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            ctx.header = "7E5"  # type: ignore[misc]

    def test_context_for_custom_pid(self) -> None:
        """context_for_custom_pid maps the PID's header/filter/init_extra."""
        pid = CustomPid(
            id="test",
            name="Test",
            mode="22",
            query="FFFF",
            fmt=dict(_FMT),
            can_header="7E5",
            can_filter="7ED",
            init_extra="ATST64",
        )
        ctx = context_for_custom_pid(pid)
        assert ctx.header == "7E5"
        assert ctx.filter == "7ED"
        assert ctx.extra_init == "ATST64"

    def test_context_for_custom_pid_defaults(self) -> None:
        """A PID without header/filter/init_extra yields the default context."""
        pid = _make_pid()
        ctx = context_for_custom_pid(pid)
        assert ctx == CanContext()


class TestBuildQueryPlan:
    """The query-plan builder groups items by context, default first."""

    def test_default_context_first(self) -> None:
        """The default-context group is emitted before custom-context groups."""
        default_item = StandardQueryItem(
            command_name="ENGINE_SPEED", command=MagicMock()
        )
        custom_item = CustomQueryItem(
            pid=_make_pid(header="7E5"),
            command=MagicMock(),
            evaluator=make_fmt_evaluator(_FMT),
            context=CanContext(header="7E5"),
        )
        plan = build_query_plan([custom_item, default_item])
        assert plan[0][0] == CanContext()
        assert plan[1][0] == CanContext(header="7E5")

    def test_items_grouped_by_context(self) -> None:
        """Items sharing a context are grouped into a single plan entry."""
        ctx_a = CanContext(header="7E5")
        ctx_b = CanContext(header="7E6")
        items = [
            CustomQueryItem(
                pid=_make_pid("1", "A"),
                command=MagicMock(),
                evaluator=make_fmt_evaluator(_FMT),
                context=ctx_a,
            ),
            CustomQueryItem(
                pid=_make_pid("2", "B"),
                command=MagicMock(),
                evaluator=make_fmt_evaluator(_FMT),
                context=ctx_a,
            ),
            CustomQueryItem(
                pid=_make_pid("3", "C"),
                command=MagicMock(),
                evaluator=make_fmt_evaluator(_FMT),
                context=ctx_b,
            ),
        ]
        plan = build_query_plan(items)
        assert len(plan) == 2
        group_sizes = sorted(len(items) for _, items in plan)
        assert group_sizes == [1, 2]

    def test_empty_plan(self) -> None:
        """An empty items list produces an empty plan."""
        assert build_query_plan([]) == []

    def test_only_default_context(self) -> None:
        """Standard-only items collapse into a single default-context group."""
        items = [
            StandardQueryItem(command_name="ENGINE_SPEED", command=MagicMock()),
            StandardQueryItem(command_name="VEHICLE_SPEED", command=MagicMock()),
        ]
        plan = build_query_plan(items)
        assert len(plan) == 1
        assert plan[0][0] == CanContext()
        assert len(plan[0][1]) == 2

    def test_custom_contexts_sorted_deterministically(self) -> None:
        """Custom contexts are emitted in ascending header order."""
        items = [
            CustomQueryItem(
                pid=_make_pid(h, h),
                command=MagicMock(),
                evaluator=make_fmt_evaluator(_FMT),
                context=CanContext(header=h),
            )
            for h in ("7E7", "7E5", "7E6")
        ]
        plan = build_query_plan(items)
        headers = [ctx.header for ctx, _ in plan]
        assert headers == ["7E5", "7E6", "7E7"]


class TestStandardQueryItemExecute:
    """StandardQueryItem.execute delegates to obdii's connection.query."""

    def test_returns_value_on_success(self) -> None:
        """A successful query returns the response's value."""
        cmd = MagicMock()
        cmd.name = "ENGINE_SPEED"
        conn = MagicMock()
        resp = MagicMock()
        resp.value = 1500.0
        resp.raw = b"7E8 04 41 0C 1A F8\r>"
        conn.query.return_value = resp
        item = StandardQueryItem(command_name="ENGINE_SPEED", command=cmd)
        assert item.execute(conn) == 1500.0

    def test_returns_none_on_no_response(self) -> None:
        """A None response from the connection yields None."""
        cmd = MagicMock()
        conn = MagicMock()
        conn.query.return_value = None
        item = StandardQueryItem(command_name="ENGINE_SPEED", command=cmd)
        assert item.execute(conn) is None

    def test_returns_none_on_buffer_full(self) -> None:
        """A 'BUFFER FULL' raw response is treated as no data."""
        cmd = MagicMock()
        conn = MagicMock()
        resp = MagicMock()
        resp.value = 1500.0
        resp.raw = b"BUFFER FULL\r>"
        conn.query.return_value = resp
        item = StandardQueryItem(command_name="ENGINE_SPEED", command=cmd)
        assert item.execute(conn) is None

    def test_key_is_command_name(self) -> None:
        """The item's key is the command_name."""
        item = StandardQueryItem(command_name="ENGINE_SPEED", command=MagicMock())
        assert item.key == "ENGINE_SPEED"


class TestCustomQueryItemExecute:
    """CustomQueryItem.execute extracts the clean payload and evaluates fmt."""

    def test_returns_evaluated_value(self) -> None:
        """A Mode 22 response is parsed and the fmt is evaluated."""
        cmd = MagicMock()
        conn = MagicMock()
        # Response: 7ED 05 62 02 8C 1F A0 → clean payload [1F, A0]
        # fmt bix=0, len=8 → 0x1F = 31
        resp = MagicMock()
        resp.raw = b"7ED 05 62 02 8C 1F A0\r>"
        conn.query.return_value = resp
        pid = CustomPid(
            id="t",
            name="SOC",
            mode="22",
            query="028C",
            fmt={"bix": 0, "len": 8},
        )
        item = CustomQueryItem(
            pid=pid,
            command=cmd,
            evaluator=make_fmt_evaluator({"bix": 0, "len": 8}),
            context=CanContext(header="7ED"),
        )
        result = item.execute(conn)
        assert result == 31.0

    def test_returns_none_on_no_response(self) -> None:
        """A None response from the connection yields None."""
        conn = MagicMock()
        conn.query.return_value = None
        pid = _make_pid()
        item = CustomQueryItem(
            pid=pid,
            command=MagicMock(),
            evaluator=make_fmt_evaluator(_FMT),
            context=CanContext(),
        )
        assert item.execute(conn) is None

    def test_returns_none_on_empty_raw(self) -> None:
        """An empty raw response yields None."""
        conn = MagicMock()
        resp = MagicMock()
        resp.raw = b""
        conn.query.return_value = resp
        pid = _make_pid()
        item = CustomQueryItem(
            pid=pid,
            command=MagicMock(),
            evaluator=make_fmt_evaluator(_FMT),
            context=CanContext(),
        )
        assert item.execute(conn) is None

    def test_returns_none_on_buffer_full(self) -> None:
        """A 'BUFFER FULL' raw response is treated as no data."""
        conn = MagicMock()
        resp = MagicMock()
        resp.raw = b"BUFFER FULL\r>"
        conn.query.return_value = resp
        pid = _make_pid()
        item = CustomQueryItem(
            pid=pid,
            command=MagicMock(),
            evaluator=make_fmt_evaluator(_FMT),
            context=CanContext(),
        )
        assert item.execute(conn) is None

    def test_key_is_pid_id(self) -> None:
        """The item's key is the PID's unique id, not its display name."""
        pid = _make_pid("t", "SOC BMS")
        item = CustomQueryItem(
            pid=pid,
            command=MagicMock(),
            evaluator=make_fmt_evaluator(_FMT),
            context=CanContext(),
        )
        assert item.key == "t"

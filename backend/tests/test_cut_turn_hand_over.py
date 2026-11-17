"""A cut chat turn hands over its automatic resume before the drain cancels it.

xtm-one #7132 (web-miss-6): a resumed hop runs as a background turn; the
shutdown drain cancelled it and its next resume was queued only at the end
of its unwinding, so a slow unwinding or the pod's kill lost it (6 of 16 hops
ended without ``resumed_as``). A first cut never named its own trace, so a
resume taken before that trace was flushed linked nothing
(``resumed_from.trace_id`` null).
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import shutdown_finalizers
from app.core.shutdown_finalizers import (
    BACKGROUND_TURN_CANCEL_MESSAGE,
    before_cancel,
    drain_shutdown_finalizers,
    track_background_turn,
)
from app.services.langfuse_tracing import _take_root_trace_link, arm_root_trace_link, next_root_trace


@pytest.fixture(autouse=True)
def _empty_registries(monkeypatch):
    monkeypatch.setattr(shutdown_finalizers, "_request_tasks", set())
    monkeypatch.setattr(shutdown_finalizers, "_finalizers", set())
    monkeypatch.setattr(shutdown_finalizers, "_background_turns", set())
    monkeypatch.setattr(shutdown_finalizers, "_before_cancel", {})


async def test_a_background_turn_hands_over_before_it_is_cancelled():
    order: list[str] = []
    started = asyncio.Event()

    async def _hand_over() -> None:
        order.append("hand-over")

    async def _turn() -> None:
        with before_cancel(_hand_over):
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError as exc:
                order.append(f"cancelled:{exc.args[0] if exc.args else ''}")
                raise

    task = asyncio.create_task(_turn())
    track_background_turn(task)
    await started.wait()

    await drain_shutdown_finalizers(timeout=1.0, background_grace_until=None)

    assert order == ["hand-over", f"cancelled:{BACKGROUND_TURN_CANCEL_MESSAGE}"]
    assert task.cancelled()


async def test_a_slow_hand_over_never_holds_the_cancel_past_its_bound(monkeypatch):
    monkeypatch.setattr(shutdown_finalizers, "BEFORE_CANCEL_SECONDS", 0.05)
    started = asyncio.Event()

    async def _hangs() -> None:
        await asyncio.sleep(3600)

    async def _turn() -> None:
        with before_cancel(_hangs):
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_turn())
    track_background_turn(task)
    await started.wait()

    await asyncio.wait_for(drain_shutdown_finalizers(timeout=1.0, background_grace_until=None), 2.0)

    assert task.cancelled()


async def test_a_failing_hand_over_still_cancels_the_turn(caplog):
    started = asyncio.Event()

    async def _fails() -> None:
        raise RuntimeError("redis down")

    async def _turn() -> None:
        with before_cancel(_fails):
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_turn())
    track_background_turn(task)
    await started.wait()

    await drain_shutdown_finalizers(timeout=1.0, background_grace_until=None)

    assert task.cancelled()
    assert "hand-over failed" in caplog.text


async def test_a_turn_that_ended_leaves_no_hand_over_behind():
    called: list[str] = []

    async def _hand_over() -> None:
        called.append("x")

    async def _turn() -> None:
        with before_cancel(_hand_over):
            await asyncio.sleep(0)

    task = asyncio.create_task(_turn())
    track_background_turn(task)
    await task

    await drain_shutdown_finalizers(timeout=1.0, background_grace_until=None)

    assert called == []
    assert shutdown_finalizers._before_cancel == {}


async def test_the_first_cut_knows_its_own_trace_id():
    link = arm_root_trace_link()

    taken = _take_root_trace_link()

    # The turn's first trace takes the armed link (and adds no metadata); the
    # handler reads the id it records when the turn is cut.
    assert taken is link and taken.metadata == {}
    assert _take_root_trace_link() is None


async def test_a_resumed_hop_s_link_is_not_replaced_by_an_armed_one():
    with next_root_trace({"resumed_from": {"resume": 2}}) as link:
        assert _take_root_trace_link() is link
        assert link.metadata == {"resumed_from": {"resume": 2}}

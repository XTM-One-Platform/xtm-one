"""WebSocket token coalescing for the chat endpoint.

LLM providers emit very small deltas; sending one WS frame per delta floods
slow links with hundreds of tiny frames per second. ``_StreamCoalescer``
buffers token chunks and flushes at most once per coalescing window, while
callers drain the buffer explicitly before ``status``/``done``/``error``
frames so the on-wire ordering matches the order events were produced by
the agent loop.

These tests pin the timing/ordering contract the chat WS endpoint relies on:
coalescing within the window, flush-before-status ordering, tail draining on
``aclose()`` with no lingering task, and reuse across turns.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.v1.chat import _StreamCoalescer

# Short window so tests run fast while still exercising real timer behaviour.
_WINDOW = 0.02

# Event-loop turns a ``delay=0`` coalescer needs at most to reach a state:
# the tests driving an in-flight send count loop turns, never wall time, so a
# loaded CI worker cannot time them out.
_MAX_TURNS = 100


async def _until(predicate) -> None:
    """Yield to the event loop until ``predicate()`` holds, bounded in turns."""
    for _ in range(_MAX_TURNS):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"not reached within {_MAX_TURNS} event-loop turns")


class _FrameRecorder:
    """Fake ``_safe_send_json`` that records every frame in order."""

    def __init__(self, *, closed: bool = False):
        self.frames: list[dict] = []
        self.closed = closed

    async def __call__(self, data: dict) -> bool:
        if self.closed:
            return False
        self.frames.append(data)
        return True


@pytest.mark.asyncio
async def test_many_small_chunks_coalesce_into_one_frame():
    """N tiny deltas inside one window -> exactly 1 merged ``stream`` frame."""
    send = _FrameRecorder()
    c = _StreamCoalescer(send, delay=_WINDOW)

    for i in range(20):
        c.add(f"t{i} ")
    await asyncio.sleep(_WINDOW * 4)

    assert len(send.frames) == 1
    assert send.frames[0]["type"] == "stream"
    assert send.frames[0]["content"] == "".join(f"t{i} " for i in range(20))


@pytest.mark.asyncio
async def test_flush_drains_tail_before_status_frame():
    """Callers flush before status events -> stream frame precedes status."""
    send = _FrameRecorder()
    c = _StreamCoalescer(send, delay=_WINDOW)

    c.add("hello ")
    c.add("world")
    # Mimic _on_event: drain pending token text, then emit the status frame.
    await c.flush()
    await send({"type": "status", "status": "tool_start"})

    assert [f["type"] for f in send.frames] == ["stream", "status"]
    assert send.frames[0]["content"] == "hello world"

    # The armed delayed flush must not re-send the already-drained text.
    await asyncio.sleep(_WINDOW * 4)
    assert [f["type"] for f in send.frames] == ["stream", "status"]


@pytest.mark.asyncio
async def test_flush_with_empty_buffer_sends_nothing():
    send = _FrameRecorder()
    c = _StreamCoalescer(send, delay=_WINDOW)

    await c.flush()

    assert send.frames == []


@pytest.mark.asyncio
async def test_aclose_cancels_armed_flush_and_drains_tail():
    """aclose() (the per-message finally) drains the tail exactly once and
    leaves no armed task that could wake up after the turn completes."""
    send = _FrameRecorder()
    c = _StreamCoalescer(send, delay=_WINDOW)

    c.add("tail")
    task = c._task
    assert task is not None and not task.done()

    await c.aclose()

    assert len(send.frames) == 1
    assert send.frames[0] == {"type": "stream", "content": "tail"}
    assert c._task is None
    assert task.cancelled() or task.done()

    # Sleeping past the original window must not produce a late duplicate.
    await asyncio.sleep(_WINDOW * 4)
    assert len(send.frames) == 1


@pytest.mark.asyncio
async def test_coalescer_is_reusable_across_turns():
    """After aclose() (end of one message), the next turn re-arms cleanly."""
    send = _FrameRecorder()
    c = _StreamCoalescer(send, delay=_WINDOW)

    c.add("turn-1")
    await c.aclose()
    c.add("turn-2")
    await asyncio.sleep(_WINDOW * 4)

    assert [f["content"] for f in send.frames] == ["turn-1", "turn-2"]


class _BlockingSend:
    """Fake send that blocks inside the call until released, so tests can
    inject chunks while a flush's send is in-flight."""

    def __init__(self):
        self.frames: list[dict] = []
        self.in_send = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self, data: dict) -> bool:
        self.in_send.set()
        await self.gate.wait()
        self.frames.append(data)
        return True


@pytest.mark.asyncio
async def test_chunk_added_during_in_flight_send_is_drained():
    """A chunk that arrives while a flush's send is awaiting must still go
    out within a coalescing window - ``add()`` sees a non-done task and does
    not arm a new one, so the delayed flush itself re-checks the buffer
    after sending instead of leaving the tail stuck until the next event."""
    send = _BlockingSend()
    c = _StreamCoalescer(send, delay=0)

    c.add("first ")
    # Wait until the delayed flush is blocked inside the send call.
    await _until(send.in_send.is_set)
    # This chunk lands while the send is in-flight: the armed task is not
    # done yet, so add() must not (and does not) create a second task.
    c.add("tail")
    task = c._task
    send.gate.set()

    # No further add()/flush()/aclose() calls: the tail must drain on its own,
    # and the delayed-flush task exits once the buffer is observed empty.
    assert task is not None
    await _until(task.done)
    assert [f["content"] for f in send.frames] == ["first ", "tail"]
    assert c._runs == []


@pytest.mark.asyncio
async def test_reasoning_deltas_coalesce_into_one_thinking_text_frame():
    """Reasoning streams many tiny deltas a second; each was its own
    awaited send. Merged, the client appends the same text."""
    send = _FrameRecorder()
    c = _StreamCoalescer(send, delay=_WINDOW)

    for i in range(30):
        c.add_thinking(f"r{i} ")
    await asyncio.sleep(_WINDOW * 4)

    assert send.frames == [
        {"type": "status", "status": "thinking_text", "content": "".join(f"r{i} " for i in range(30))}
    ]


@pytest.mark.asyncio
async def test_reasoning_and_answer_runs_keep_their_order():
    send = _FrameRecorder()
    c = _StreamCoalescer(send, delay=_WINDOW)

    c.add_thinking("plan ")
    c.add_thinking("it. ")
    c.add("Here ")
    c.add("goes.")
    c.add_thinking("more reasoning")
    await c.flush()
    await send({"type": "status", "status": "tool_start"})

    assert send.frames == [
        {"type": "status", "status": "thinking_text", "content": "plan it. "},
        {"type": "stream", "content": "Here goes."},
        {"type": "status", "status": "thinking_text", "content": "more reasoning"},
        {"type": "status", "status": "tool_start"},
    ]


@pytest.mark.asyncio
async def test_a_status_never_overtakes_a_run_of_an_in_flight_flush():
    """One flush sends one frame per run: a caller flushing while it is in
    flight waits for it, so the status it sends next lands after every run."""
    send = _BlockingSend()
    c = _StreamCoalescer(send, delay=0)

    c.add_thinking("reasoning ")
    c.add("answer")
    await _until(send.in_send.is_set)  # delayed flush blocked on run 1

    async def _status_after_flush():
        await c.flush()
        await send({"type": "status", "status": "tool_start"})

    status_task = asyncio.create_task(_status_after_flush())
    await _until(c._lock.locked)
    for _ in range(3):  # let the status task reach the lock the flush holds
        await asyncio.sleep(0)
    assert not status_task.done()
    send.gate.set()
    await _until(status_task.done)
    status_task.result()

    assert [f.get("status") or f["type"] for f in send.frames] == ["thinking_text", "stream", "tool_start"]


@pytest.mark.asyncio
async def test_a_cancelled_flush_keeps_the_runs_it_did_not_send():
    send = _BlockingSend()
    c = _StreamCoalescer(send, delay=0)

    c.add_thinking("reasoning ")
    c.add("answer")
    await _until(send.in_send.is_set)
    c._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await c._task
    send.gate.set()
    await c.flush()

    assert send.frames == [{"type": "stream", "content": "answer"}]


@pytest.mark.asyncio
async def test_flush_on_closed_socket_drops_silently():
    """A closed socket (send returns False) must not raise nor retain the
    buffered text - matching ``_safe_send_json`` drop-on-closed semantics."""
    send = _FrameRecorder(closed=True)
    c = _StreamCoalescer(send, delay=_WINDOW)

    c.add("lost")
    await c.aclose()

    assert send.frames == []
    # Buffer was cleared on flush - a reopened send path must not resurrect it.
    send.closed = False
    await c.flush()
    assert send.frames == []

"""Every relayed chat turn ENDS on every stream it is shown on.

Every stream of a conversation is fed the turns it did not start, and
a client following a relayed turn waits for its terminal frame with no poll
behind it. Pinned here, on ``services/chat_turn_stream.py`` and the real send
connection (``run_chat_connection``):

* a resync (hub reconnect, listener overflow) onto a turn that already ended
  delivers the turn's own terminal, kept by the tombstone;
* a terminal a Redis failure or the quiet period after it kept off the
  channel is delivered once Redis answers;
* a turn whose producer died is closed by the relay's liveness probe with an
  interrupted terminal (``reason: "lost"``) once its lock is gone;
* a relayed turn followed by another turn is closed before the next turn's
  first frame;
* a live terminal whose sequence number the resync snapshot already counted
  is delivered.

And the ownership half: every frame of a turn names it (``turn_id``, a digest
of the lock token, never the token), on the stream that runs it and on every
stream it is relayed to; a send folded into a running turn is told which one,
then relayed that turn to its end: at once from the tombstone when it ended
before the relay attached, as lost once Redis cannot vouch for it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api.v1 import chat as chat_module
from app.core.utils import chat_processing_key
from app.services import chat_turn_guard, chat_turn_stream
from app.services.chat_turn_stream import (
    RESYNC,
    TERMINAL_REASON_INTERRUPTED,
    TERMINAL_REASON_LOST,
    TURN_INTERRUPTED_MESSAGE,
    TURN_LOST_MESSAGE,
    TurnPublisher,
    interrupted_frame,
    journal_keys,
    lost_turn_frame,
    public_turn_id,
    relay_turn_frames,
    turn_channel,
)

# The fan-out suite's harness, reused (same precedent as
# test_segment_delivery.py): the fixtures must be imported under their own
# names for pytest to resolve them; the F811 noqas on the test signatures
# cover the fixture-as-parameter shadowing.
from tests.test_chat_turn_fanout import (  # noqa: F401
    CONV,
    _attached_viewer,
    _FakeStreamRedis,
    _patch_general_chat,
    _start,
    _wait_until,
    fake_rl,
    fanout_redis,
    ws_env,
)
from tests.test_chat_web_steering import _send, _wait_for_frame

# ── Helpers ───────────────────────────────────────────────────────────────


class _ScriptedHub:
    """A hub whose single listener queue the test fills by hand."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self.queue = queue

    async def attach(self, _conversation_id: str) -> asyncio.Queue:
        return self.queue

    async def detach(self, _conversation_id: str, _queue: asyncio.Queue) -> None:
        return None


def _scripted_queue(monkeypatch: pytest.MonkeyPatch, maxsize: int = 0) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    hub = _ScriptedHub(queue)
    monkeypatch.setattr(chat_turn_stream, "_hub", lambda: hub)
    return queue


def _recorder() -> tuple[list[dict], object]:
    sent: list[dict] = []

    async def _send(frame: dict) -> bool:
        sent.append(frame)
        return True

    return sent, _send


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _published_payloads(redis: _FakeStreamRedis) -> list[str]:
    return [payload for channel, payload in redis.published if channel == turn_channel(CONV)]


@pytest.fixture(autouse=True)
async def _no_recovery_leaks():
    """A terminal recovery outlives its publisher by design; one a test left
    waiting on a quiet period must not outlive the test."""
    yield
    loop = asyncio.get_running_loop()
    for task in list(chat_turn_stream._background_tasks):
        if task.get_loop() is loop:
            task.cancel()


# ── Wire contract ─────────────────────────────────────────────────────────


def test_turn_id_is_a_stable_digest_of_the_lock_token_never_the_token():
    token = "0123456789abcdef0123456789abcdef"
    turn_id = public_turn_id(token)
    assert turn_id == public_turn_id(token)
    assert turn_id != public_turn_id(token + "x")
    assert token not in turn_id and len(turn_id) == 32


def test_synthesized_terminals_say_why_they_were_synthesized():
    assert interrupted_frame() == {
        "type": "error",
        "content": TURN_INTERRUPTED_MESSAGE,
        "reason": TERMINAL_REASON_INTERRUPTED,
    }
    assert lost_turn_frame() == {
        "type": "error",
        "content": TURN_LOST_MESSAGE,
        "reason": TERMINAL_REASON_LOST,
    }
    assert chat_turn_stream.is_terminal_frame(lost_turn_frame())


async def test_every_published_frame_of_a_turn_names_it(fanout_redis):  # noqa: F811
    pub = TurnPublisher(CONV, "tok", origin="owner")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["x"]})
    pub.offer({"type": "done", "content": "final", "message_id": "m-1"})
    await pub.aclose()
    frames = fanout_redis.frames(turn_channel(CONV))
    assert [f["type"] for f in frames] == ["thinking", "status", "done"]
    assert {f["turn_id"] for f in frames} == {public_turn_id("tok")}
    # The journal keeps the id too, so a resumed stream's steps carry it.
    # (The tombstone keeps the terminal the same way, below.)
    state_key = journal_keys(CONV, "tok")[0]
    stored = json.loads(fanout_redis.hashes[state_key]["terminal"])
    assert stored == {
        "type": "done",
        "content": "final",
        "message_id": "m-1",
        "turn_id": public_turn_id("tok"),
    }


async def test_a_resumed_stream_is_told_which_turn_it_was_resumed_into(fanout_redis):  # noqa: F811
    token = await chat_turn_guard.acquire_turn(CONV)
    owner = TurnPublisher(CONV, token, origin="owner")
    owner.start()
    await owner.on_event({"status": "tool_start", "tools": ["search"]})
    owner.offer({"type": "stream", "content": "partial"})
    await _wait_until(lambda: any(f["type"] == "stream" for f in fanout_redis.frames()))
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in sent))
        assert sent[0]["resumed"] is True
        assert {f["turn_id"] for f in sent} == {public_turn_id(token)}
        owner.offer({"type": "stream", "content": " more"})
        await _wait_until(lambda: sent[-1].get("content") == " more")
        assert sent[-1] == {
            "type": "stream",
            "content": " more",
            "turn_id": public_turn_id(token),
            "relayed": True,
        }
        # A user message is not a turn frame: no turn id.
        chat_turn_stream.publish_live(
            CONV, {"type": "user_message", "message": {"id": "m"}}, origin="owner"
        )
        await _wait_until(lambda: sent[-1]["type"] == "user_message")
        assert "turn_id" not in sent[-1]
    finally:
        await _stop(relay)
        await owner.aclose()


async def test_a_frame_of_a_producer_that_predates_turn_ids_is_given_one(
    fanout_redis, monkeypatch  # noqa: F811
):
    """A replica of the previous build (rolling deploy) publishes frames with
    no ``turn_id``: the relay derives it from the transport stamp."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    try:
        queue.put_nowait(json.dumps([{"type": "thinking", "origin": "old", "turn": "legacy", "seq": 1}]))
        await _wait_until(lambda: sent)
        assert sent[0] == {"type": "thinking", "turn_id": public_turn_id("legacy"), "relayed": True}
    finally:
        await _stop(relay)


# ── (1) A resync onto a turn that already ended ───────────────────────────


async def _show_turn(redis: _FakeStreamRedis, queue, sent, token: str) -> TurnPublisher:
    """Start a turn and hand its published frames to the viewer's queue."""
    pub = TurnPublisher(CONV, token, origin="owner")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["search"]})
    await _wait_until(lambda: any(f.get("status") == "tool_start" for f in redis.frames()))
    for payload in _published_payloads(redis):
        queue.put_nowait(payload)
    await _wait_until(lambda: any(f.get("status") == "tool_start" for f in sent))
    return pub


@pytest.mark.parametrize("why", ["hub reconnect", "listener overflow"])
async def test_a_resync_onto_a_turn_that_ended_delivers_its_own_terminal(
    fanout_redis, monkeypatch, why  # noqa: F811
):
    """The viewer is shown turn T; T's ``done`` is published while the hub is
    reconnecting (or dropped with an overflowing listener's backlog). The
    resync that follows reads no turn in flight, and delivers T's own
    terminal, kept by the tombstone: nothing else would release the viewer."""
    queue = _scripted_queue(monkeypatch, maxsize=3 if why == "listener overflow" else 0)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    try:
        pub = await _show_turn(fanout_redis, queue, sent, token)
        pub.offer({"type": "done", "content": "final answer", "message_id": "m-1"})
        await pub.aclose()
        assert await chat_turn_guard.release_turn(CONV, token)
        if why == "listener overflow":
            # The listener fell behind: its backlog - T's ``done`` included -
            # is dropped and replaced by a resync.
            chat_turn_stream._offer_to_listener(queue, _published_payloads(fanout_redis)[-1])
            for n in range(3):
                late = {"type": "stream", "content": "x", "origin": "owner", "turn": token, "seq": 100 + n}
                chat_turn_stream._offer_to_listener(queue, json.dumps([late]))
            assert queue.qsize() == 1 and queue.get_nowait() is RESYNC
            queue.put_nowait(RESYNC)
        else:
            queue.put_nowait(RESYNC)
        await _wait_until(lambda: any(f["type"] == "done" for f in sent))
        assert sent[-1] == {
            "type": "done",
            "content": "final answer",
            "message_id": "m-1",
            "turn_id": public_turn_id(token),
            "relayed": True,
        }
        # Delivered once: a copy arriving later (a recovered publish) is dropped.
        queue.put_nowait(_published_payloads(fanout_redis)[-1])
        await asyncio.sleep(0.05)
        assert [f["type"] for f in sent].count("done") == 1
    finally:
        await _stop(relay)


async def test_a_resync_onto_a_turn_closed_without_a_kept_terminal_ends_it_at_once(
    fanout_redis, monkeypatch  # noqa: F811
):
    """A tombstone with no ``terminal`` - written by a replica of the
    previous build during a rolling deploy, or for an end too large to keep -
    means its producer closed the turn and nothing more of it will come: the
    turn is ended at once, not after the grace kept for a pending recovery."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    try:
        await _show_turn(fanout_redis, queue, sent, token)
        state_key = journal_keys(CONV, token)[0]
        fanout_redis.hashes[state_key] = {"ended": "1"}  # the previous build's tombstone
        assert await chat_turn_guard.release_turn(CONV, token)
        queue.put_nowait(RESYNC)
        await _wait_until(lambda: sent[-1]["type"] == "error")
        assert sent[-1] == {**lost_turn_frame(), "turn_id": public_turn_id(token), "relayed": True}
    finally:
        await _stop(relay)


async def test_a_resync_onto_a_newer_turn_ends_the_shown_one_before_resuming_into_it(
    fanout_redis, monkeypatch  # noqa: F811
):
    """T1 is on screen, its ``done`` is lost with a hub reconnect, and T2 is
    already running when the resync reads the snapshot. T1 ends first - with
    the terminal its tombstone kept - and only then is the stream resumed
    into T2, so a client never holds two turns open."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    first = await chat_turn_guard.acquire_turn(CONV)
    try:
        pub = await _show_turn(fanout_redis, queue, sent, first)
        pub.offer({"type": "done", "content": "first answer", "message_id": "m-1"})
        await pub.aclose()  # published, but lost with the reconnect
        assert await chat_turn_guard.release_turn(CONV, first)
        second = await chat_turn_guard.acquire_turn(CONV)
        running = TurnPublisher(CONV, second, origin="b")
        running.start()
        await running.on_event({"status": "tool_start", "tools": ["x"]})
        await _wait_until(lambda: fanout_redis.hashes.get(journal_keys(CONV, second)[0]))
        queue.put_nowait(RESYNC)
        await _wait_until(lambda: sent[-1].get("status") == "tool_start" and sent[-1].get("turn_id") == public_turn_id(second))
        closing = next(i for i, f in enumerate(sent) if f["type"] == "done")
        resumed = next(i for i, f in enumerate(sent) if f.get("resumed"))
        assert closing < resumed
        assert sent[closing]["content"] == "first answer"
        assert sent[closing]["turn_id"] == public_turn_id(first)
        assert sent[resumed]["turn_id"] == public_turn_id(second)
        await running.aclose()
    finally:
        await _stop(relay)


async def test_a_terminal_the_resync_snapshot_already_counted_is_still_delivered(
    fanout_redis, monkeypatch  # noqa: F811
):
    """The batch that publishes a terminal also journals its sequence number,
    so a resync that reads the snapshot right after it (a hub reconnect whose
    confirmation raced the batch) replays the turn up to and including that
    number. A replay never carries a terminal: the live one is still
    forwarded, never dropped as covered."""
    monkeypatch.setattr(chat_turn_stream, "LIVENESS_CHECK_SECONDS", 60.0)
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="owner")
    pub.start()
    try:
        await pub.on_event({"status": "tool_start", "tools": ["search"]})
        pub.offer({"type": "done", "content": "final answer", "message_id": "m-1"})
        await _wait_until(lambda: any(f["type"] == "done" for f in fanout_redis.frames()))
        done_seq = next(f["seq"] for f in fanout_redis.frames() if f["type"] == "done")
        state_key = journal_keys(CONV, token)[0]
        assert int(fanout_redis.hashes[state_key]["seq"]) >= done_seq
        queue.put_nowait(RESYNC)
        for payload in _published_payloads(fanout_redis):
            queue.put_nowait(payload)
        await _wait_until(lambda: any(f["type"] == "done" for f in sent), timeout=1.0)
        assert sent[-1] == {
            "type": "done",
            "content": "final answer",
            "message_id": "m-1",
            "turn_id": public_turn_id(token),
            "relayed": True,
        }
    finally:
        await _stop(relay)
        await pub.aclose()


def test_the_tombstone_keeps_a_terminal_within_a_bound():
    done = {"type": "done", "content": "answer", "message_id": "m", "turn": "t", "seq": 3}
    assert json.loads(chat_turn_stream._stored_terminal(done)) == {
        "type": "done",
        "content": "answer",
        "message_id": "m",
    }
    bound = chat_turn_stream.MAX_STORED_TERMINAL_CHARS
    heavy = {**done, "tool_call_trace": ["x" * bound], "reasoning": "r"}
    assert json.loads(chat_turn_stream._stored_terminal(heavy)) == {
        "type": "done",
        "content": "answer",
        "message_id": "m",
    }, "the reasoning-panel fields go first; the refetch brings them back"
    assert chat_turn_stream._stored_terminal({**done, "content": "x" * bound}) is None
    # A kept terminal reads back only as a terminal.
    assert chat_turn_stream._decode_terminal(json.dumps(done)) == done
    assert chat_turn_stream._decode_terminal(json.dumps({"type": "stream"})) is None
    assert chat_turn_stream._decode_terminal("not json") is None


async def test_a_hub_reconnect_delivers_the_terminal_of_a_turn_that_ended_during_the_gap(
    fanout_redis,  # noqa: F811
):
    """The same, end to end through the real hub: its subscription drops, the
    turn ends while nobody is subscribed, and the reconnect's resync closes
    the turn on the viewer."""
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="owner")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["search"]})
    await _wait_until(lambda: fanout_redis.published)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    try:
        await _wait_until(lambda: any(f.get("status") == "tool_start" for f in sent))
        channel = turn_channel(CONV)
        (subscriber,) = fanout_redis.subscribers[channel]
        subscriber.fail_next_read = True
        await _wait_until(lambda: not fanout_redis.subscribers.get(channel), timeout=3.0)
        pub.offer({"type": "done", "content": "answered meanwhile", "message_id": "m-9"})
        await pub.aclose()
        assert await chat_turn_guard.release_turn(CONV, token)
        await _wait_until(lambda: any(f["type"] == "done" for f in sent), timeout=5.0)
        done = next(f for f in sent if f["type"] == "done")
        assert done["content"] == "answered meanwhile" and done["message_id"] == "m-9"
        assert done["relayed"] is True and done["turn_id"] == public_turn_id(token)
    finally:
        await _stop(relay)


# ── (2) A Redis failure or quiet period on the publishing side ────────────


async def test_a_terminal_dropped_by_a_quiet_period_is_delivered_once_redis_answers(
    fanout_redis, monkeypatch  # noqa: F811
):
    """A publish failure trips a process-wide quiet period during which every
    frame is dropped - the terminal of every turn ending in those 30 s
    included, and nothing ever sent it again. The publisher now delivers its
    terminal (and writes its tombstone) once Redis answers."""
    monkeypatch.setattr(chat_turn_stream, "FAILURE_COOLDOWN_SECONDS", 0.05)
    monkeypatch.setattr(chat_turn_stream, "TERMINAL_RECOVERY_RETRY_SECONDS", 0.01)
    pub = TurnPublisher(CONV, "tok", origin="owner")
    pub.start()
    await _wait_until(lambda: fanout_redis.published)  # the opening ack
    fanout_redis.fail_pipelines = True
    pub.offer({"type": "done", "content": "final", "message_id": "m-1"})
    await pub.aclose()
    assert not any(f["type"] == "done" for f in fanout_redis.frames())
    fanout_redis.fail_pipelines = False
    await _wait_until(
        lambda: any(f["type"] == "done" for f in fanout_redis.frames()), timeout=3.0
    )
    (done,) = [f for f in fanout_redis.frames() if f["type"] == "done"]
    assert done["turn"] == "tok" and done["content"] == "final"
    assert done["turn_id"] == public_turn_id("tok")
    state_key = journal_keys(CONV, "tok")[0]
    await _wait_until(lambda: fanout_redis.hashes.get(state_key, {}).get("ended") == "1")
    assert json.loads(fanout_redis.hashes[state_key]["terminal"])["content"] == "final"


async def test_a_terminal_that_was_published_is_not_published_again_by_the_recovery(
    fanout_redis, monkeypatch  # noqa: F811
):
    """Only what failed is retried: the terminal went out, the tombstone
    write failed - the recovery writes the tombstone and publishes nothing."""
    monkeypatch.setattr(chat_turn_stream, "FAILURE_COOLDOWN_SECONDS", 0.05)
    monkeypatch.setattr(chat_turn_stream, "TERMINAL_RECOVERY_RETRY_SECONDS", 0.01)
    pub = TurnPublisher(CONV, "tok", origin="owner")
    pub.start()
    pub.offer({"type": "done", "content": "final"})
    await _wait_until(lambda: any(f["type"] == "done" for f in fanout_redis.frames()))
    fanout_redis.fail_pipelines = True
    await pub.aclose()
    state_key = journal_keys(CONV, "tok")[0]
    assert "ended" not in fanout_redis.hashes.get(state_key, {})
    fanout_redis.fail_pipelines = False
    await _wait_until(lambda: fanout_redis.hashes.get(state_key, {}).get("ended") == "1", timeout=3.0)
    assert [f["type"] for f in fanout_redis.frames()].count("done") == 1


async def test_a_relayed_turn_whose_terminal_was_lost_is_closed_after_the_grace(
    fanout_redis, monkeypatch  # noqa: F811
):
    """The turn is over (its lock released) but neither its terminal nor its
    tombstone ever reached Redis, and no recovery could deliver them: once the
    grace for that recovery has passed, the relay closes the turn itself."""
    monkeypatch.setattr(chat_turn_stream, "LIVENESS_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(chat_turn_stream, "TERMINAL_GRACE_SECONDS", 0.2)
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    try:
        pub = await _show_turn(fanout_redis, queue, sent, token)
        # The end never reaches Redis, and the lock goes (released, or lapsed).
        fanout_redis.fail_pipelines = True
        pub.offer({"type": "done", "content": "final"})
        await pub.aclose()
        fanout_redis.fail_pipelines = False
        pub._recovery.cancel()  # and its recovery never gets through either
        fanout_redis.kv.pop(chat_processing_key(CONV))
        shown = len(sent)
        await asyncio.sleep(0.1)
        assert len(sent) == shown, "within the grace the producer may still deliver it"
        await _wait_until(lambda: len(sent) > shown, timeout=3.0)
        assert sent[-1] == {
            **lost_turn_frame(),
            "turn_id": public_turn_id(token),
            "relayed": True,
        }
    finally:
        await _stop(relay)


# ── (3) A producer that died ──────────────────────────────────────────────


async def test_a_relayed_turn_whose_producer_died_is_closed_with_an_interrupted_terminal(
    fanout_redis, monkeypatch  # noqa: F811
):
    """The process running the turn dies: no terminal, no tombstone, and its
    lock lapses (``TURN_MARKER_TTL_SECONDS``). Nothing ever ended the turn on
    the viewer. The relay's liveness probe now sees the turn gone and closes
    it with an interrupted terminal."""
    monkeypatch.setattr(chat_turn_stream, "LIVENESS_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(chat_turn_stream, "TERMINAL_GRACE_SECONDS", 0.1)
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="owner")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["search"]})
    await _wait_until(lambda: fanout_redis.published)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    try:
        await _wait_until(lambda: any(f.get("status") == "tool_start" for f in sent))
        # Alive: a probe finding its lock held changes nothing.
        await asyncio.sleep(0.15)
        assert sent[-1].get("status") == "tool_start"
        # The process dies with its drainer; the lock lapses.
        pub._task.cancel()
        fanout_redis.kv.pop(chat_processing_key(CONV))
        await _wait_until(lambda: sent[-1]["type"] == "error", timeout=3.0)
        assert sent[-1] == {
            "type": "error",
            "content": TURN_LOST_MESSAGE,
            "reason": TERMINAL_REASON_LOST,
            "turn_id": public_turn_id(token),
            "relayed": True,
        }
        # Closed once.
        await asyncio.sleep(0.2)
        assert [f["type"] for f in sent].count("error") == 1
    finally:
        await _stop(relay)


async def test_a_turn_superseded_by_another_is_closed_before_the_next_turns_frames(
    fanout_redis, monkeypatch  # noqa: F811
):
    """Shown turn T1 never delivered its end; T2 starts. T1 is closed - with
    its own terminal when its tombstone kept one - before T2's first frame,
    so no client ever holds two turns open."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    first = await chat_turn_guard.acquire_turn(CONV)
    try:
        pub = await _show_turn(fanout_redis, queue, sent, first)
        pub.offer({"type": "done", "content": "first answer"})
        await pub.aclose()  # published, but never reaches this listener
        assert await chat_turn_guard.release_turn(CONV, first)
        second = await chat_turn_guard.acquire_turn(CONV)
        queue.put_nowait(
            json.dumps([{"type": "thinking", "origin": "b", "turn": second, "seq": 1}])
        )
        await _wait_until(lambda: sent[-1].get("turn_id") == public_turn_id(second))
        closing, opening = sent[-2], sent[-1]
        assert closing["type"] == "done" and closing["content"] == "first answer"
        assert closing["turn_id"] == public_turn_id(first)
        assert opening == {"type": "thinking", "turn_id": public_turn_id(second), "relayed": True}
    finally:
        await _stop(relay)


async def test_a_late_frame_of_a_turn_already_closed_reopens_nothing(
    fanout_redis, monkeypatch  # noqa: F811
):
    """T1 was closed on this stream (its producer closed it without a kept
    terminal) and T2 is on screen when a late frame of T1 arrives - a
    producer that outlived its lock. Forwarded, it would reopen T1 and close
    the live T2 as lost; it is dropped, like every later frame of a turn the
    stream already saw end."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    first = await chat_turn_guard.acquire_turn(CONV)
    try:
        await _show_turn(fanout_redis, queue, sent, first)
        fanout_redis.hashes[journal_keys(CONV, first)[0]] = {"ended": "1"}
        assert await chat_turn_guard.release_turn(CONV, first)
        queue.put_nowait(RESYNC)
        await _wait_until(lambda: sent[-1]["type"] == "error")
        second = await chat_turn_guard.acquire_turn(CONV)
        queue.put_nowait(
            json.dumps([{"type": "thinking", "origin": "b", "turn": second, "seq": 1}])
        )
        await _wait_until(lambda: sent[-1].get("turn_id") == public_turn_id(second))
        shown = len(sent)
        late = {"type": "stream", "content": "late", "origin": "owner", "turn": first, "seq": 99}
        live = {"type": "stream", "content": "live", "origin": "b", "turn": second, "seq": 2}
        queue.put_nowait(json.dumps([late]))
        queue.put_nowait(json.dumps([live]))
        await _wait_until(lambda: sent[-1].get("content") == "live")
        assert sent[shown:] == [
            {"type": "stream", "content": "live", "turn_id": public_turn_id(second), "relayed": True}
        ]
    finally:
        await _stop(relay)


async def test_a_stop_on_the_owner_stream_reaches_viewers_with_its_stored_end(
    fanout_redis, monkeypatch  # noqa: F811
):
    """Stop pressed on the stream running the turn sends two ``cancelled``
    frames (P2.5): the acknowledgement at once, then the stored end carrying
    ``message_id`` - the one a client reads the stored thread back on. A
    viewer gets both, and a later copy of that end
    closes nothing twice."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    try:
        stored_end = {
            "type": "cancelled",
            "message_id": "m1",
            "conversation_id": CONV,
            "origin": "owner",
            "turn": token,
            "seq": 3,
        }
        for frame in (
            {"type": "thinking", "origin": "owner", "turn": token, "seq": 1},
            {"type": "cancelled", "provisional": True, "origin": "owner", "turn": token, "seq": 2},
            stored_end,
        ):
            queue.put_nowait(json.dumps([frame]))
        await _wait_until(lambda: len(sent) == 3)
        turn_id = public_turn_id(token)
        assert sent[1:] == [
            {"type": "cancelled", "provisional": True, "turn_id": turn_id, "relayed": True},
            {
                "type": "cancelled",
                "message_id": "m1",
                "conversation_id": CONV,
                "turn_id": turn_id,
                "relayed": True,
            },
        ]
        queue.put_nowait(json.dumps([stored_end]))
        await asyncio.sleep(0.2)
        assert len(sent) == 3
    finally:
        await _stop(relay)


async def test_a_resync_before_the_journal_drop_does_not_reopen_a_turn_that_ended(
    fanout_redis, monkeypatch  # noqa: F811
):
    """A turn's journal is dropped just after its terminal is published, so a
    resync in between reads a snapshot that still names the turn. The stream
    already delivered that turn's end: it is not resumed into it again."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="owner")
    pub.start()
    try:
        await pub.on_event({"status": "tool_start", "tools": ["search"]})
        pub.offer({"type": "done", "content": "final answer", "message_id": "m-1"})
        await _wait_until(lambda: any(f["type"] == "done" for f in fanout_redis.frames()))
        for payload in _published_payloads(fanout_redis):
            queue.put_nowait(payload)
        await _wait_until(lambda: any(f["type"] == "done" for f in sent))
        delivered = len(sent)
        assert await chat_turn_stream.read_snapshot(CONV) is not None  # still journaled
        queue.put_nowait(RESYNC)
        await _wait_until(queue.empty)
        await asyncio.sleep(0.05)
        assert sent[delivered:] == []
    finally:
        await _stop(relay)
        await pub.aclose()


# ── (4) An end its producer follows with the stored one ───────────────────


async def test_a_producer_error_its_stored_end_follows_still_lets_that_end_through(
    fanout_redis, monkeypatch  # noqa: F811
):
    """A web chat turn whose agent loop raised sends ``error`` at once, then
    stores the reply and ends with that stored ``done`` (``message_id``), the
    frame a viewer commits the message on - as the Stop acknowledgement is
    followed by its stored ``cancelled``. Read as the turn's end, the
    ``error`` made the relay drop the stored ``done`` on every viewer, where
    it must be delivered. The viewer is released by the ``error``, the turn's
    later steps reopen nothing, its stored end lands, and copies of either
    end are dropped."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    try:
        error = {
            "type": "error",
            "content": "I encountered an error: boom",
            "provisional": True,
            "origin": "owner",
            "turn": "t",
            "seq": 3,
        }
        stored_end = {
            "type": "done",
            "content": "I encountered an error: boom",
            "message_id": "m-1",
            "conversation_id": CONV,
            "origin": "owner",
            "turn": "t",
            "seq": 5,
        }
        for frame in (
            {"type": "thinking", "origin": "owner", "turn": "t", "seq": 1},
            {"type": "status", "status": "tool_start", "tools": ["x"], "origin": "owner", "turn": "t", "seq": 2},
            error,
            {"type": "status", "status": "persisting", "origin": "owner", "turn": "t", "seq": 4},
            stored_end,
            error,
            stored_end,
        ):
            queue.put_nowait(json.dumps([frame]))
        await _wait_until(lambda: any(f.get("message_id") == "m-1" for f in sent))
        await _wait_until(queue.empty)
        await asyncio.sleep(0.05)
        turn_id = public_turn_id("t")
        assert [(f["type"], f.get("status")) for f in sent] == [
            ("thinking", None),
            ("status", "tool_start"),
            ("error", None),
            ("done", None),
        ]
        assert sent[-1] == {
            "type": "done",
            "content": "I encountered an error: boom",
            "message_id": "m-1",
            "conversation_id": CONV,
            "turn_id": turn_id,
            "relayed": True,
        }
    finally:
        await _stop(relay)


async def test_a_resync_never_resumes_a_turn_a_provisional_end_released(
    fanout_redis, monkeypatch  # noqa: F811
):
    """Between a Stop acknowledgement and the stored end the turn is still
    journaled (it is persisting): a resync then must not resume the viewer
    into the turn it was just released from."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="owner")
    pub.start()
    try:
        await pub.on_event({"status": "tool_start", "tools": ["search"]})
        pub.offer({"type": "cancelled", "provisional": True})
        await _wait_until(lambda: any(f["type"] == "cancelled" for f in fanout_redis.frames()))
        for payload in _published_payloads(fanout_redis):
            queue.put_nowait(payload)
        await _wait_until(lambda: any(f["type"] == "cancelled" for f in sent))
        assert await chat_turn_stream.read_snapshot(CONV) is not None  # still persisting
        released = len(sent)
        queue.put_nowait(RESYNC)
        await _wait_until(queue.empty)
        await asyncio.sleep(0.05)
        assert sent[released:] == []
    finally:
        await _stop(relay)
        await pub.aclose()


async def test_a_turn_that_cannot_store_after_a_provisional_end_ends_interrupted(
    fanout_redis, monkeypatch  # noqa: F811
):
    """The stored end a provisional end announces never comes when storing the
    reply fails: closing the publisher then offers the ``interrupted`` end,
    which lands on the viewer the provisional end released and is the end the
    tombstone keeps."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="owner")
    pub.start()
    try:
        await pub.on_event({"status": "tool_start", "tools": ["search"]})
        pub.offer({"type": "error", "content": "I encountered an error: boom", "provisional": True})
        assert not pub.terminal_offered
        await pub.aclose(terminal=interrupted_frame())
        for payload in _published_payloads(fanout_redis):
            queue.put_nowait(payload)
        await _wait_until(lambda: any(f.get("reason") for f in sent))
        turn_id = public_turn_id(token)
        ends = [f for f in sent if chat_turn_stream.is_terminal_frame(f)]
        assert [chat_turn_stream.is_provisional_end(f) for f in ends] == [True, False]
        assert ends[-1] == {**interrupted_frame(), "turn_id": turn_id, "relayed": True}
        await chat_turn_guard.release_turn(CONV, token)
        end = await chat_turn_stream.probe_turn_end(CONV, token)
        assert end is not None and end.terminal == {**interrupted_frame(), "turn_id": turn_id}
    finally:
        await _stop(relay)


async def test_a_turn_whose_loop_failed_reaches_viewers_with_its_stored_end(
    monkeypatch, ws_env  # noqa: F811
):
    """End to end: the owner's agent loop raises, so the sender's stream gets
    ``error``, the ``persisting`` step and the stored ``done`` - and every
    viewer gets the ``error`` and that stored ``done``, as before #4395."""

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        raise RuntimeError("model unreachable")

    _patch_general_chat(monkeypatch, _run)
    viewer, seen = await _attached_viewer(ws_env)
    try:
        conn = await _send(ws_env, {"content": "build the report"}, timeline=[])
        own_done = next(f for f in conn.sent if f["type"] == "done")
        assert any(f["type"] == "error" for f in conn.sent)
        await _wait_until(lambda: any(f["type"] == "done" for f in seen), timeout=4.0)
        relayed = [f for f in seen if f["type"] in ("error", "done")]
        assert [f["type"] for f in relayed] == ["error", "done"]
        assert relayed[1]["message_id"] == own_done["message_id"]
        assert relayed[1]["content"] == own_done["content"]
        assert relayed[1]["relayed"] is True and relayed[1]["turn_id"] == own_done["turn_id"]
    finally:
        await _stop(viewer)


# ── (5) Another turn's frame never closes a turn still running ────────────


def _break_the_next_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next snapshot read fails (a Redis blip): the resync then follows
    nothing, while the turn on screen is still shown."""
    real = chat_turn_stream.read_snapshot
    calls = {"n": 0}

    async def _once_broken(conversation_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("redis blip")
        return await real(conversation_id)

    monkeypatch.setattr(chat_turn_stream, "read_snapshot", _once_broken)


async def test_a_late_terminal_of_another_turn_never_closes_the_turn_still_running(
    fanout_redis, monkeypatch  # noqa: F811
):
    """U is on screen and still holds the lock. A resync whose snapshot read
    failed leaves the relay following nothing, and a terminal of an older
    turn arrives late (its recovery after a quiet period). It is dropped, and
    U goes on to its end: closed as ``lost``, every later frame of U would be
    dropped as a turn already ended."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = await _show_turn(fanout_redis, queue, sent, token)
    try:
        _break_the_next_resync(monkeypatch)
        queue.put_nowait(RESYNC)
        late = {"type": "done", "content": "older answer", "message_id": "m-0", "origin": "old", "turn": "older", "seq": 9}
        queue.put_nowait(json.dumps([late]))
        await _wait_until(queue.empty)
        await asyncio.sleep(0.05)
        shown = len(sent)
        assert not any(f["type"] in ("done", "error") for f in sent)
        published = len(_published_payloads(fanout_redis))
        pub.offer({"type": "stream", "content": "still here"})
        pub.offer({"type": "done", "content": "the answer", "message_id": "m-1"})
        await _wait_until(lambda: any(f["type"] == "done" for f in fanout_redis.frames()))
        for payload in _published_payloads(fanout_redis)[published:]:
            queue.put_nowait(payload)
        await _wait_until(lambda: any(f.get("message_id") == "m-1" for f in sent))
        assert [(f["type"], f.get("content")) for f in sent[shown:]] == [
            ("stream", "still here"),
            ("done", "the answer"),
        ]
        assert {f["turn_id"] for f in sent[shown:]} == {public_turn_id(token)}
    finally:
        await _stop(relay)
        await pub.aclose()


async def test_a_frame_of_a_producer_that_lost_its_lock_never_closes_the_shown_turn(
    fanout_redis, monkeypatch  # noqa: F811
):
    """A producer that outlived its lock (its marker lapsed while it ran) is
    not the conversation's turn any more: while the turn on screen holds the
    lock, the zombie's frames are dropped instead of closing it."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = await _show_turn(fanout_redis, queue, sent, token)
    try:
        shown = len(sent)
        zombie = {"type": "stream", "content": "zombie", "origin": "old", "turn": "zombie", "seq": 40}
        queue.put_nowait(json.dumps([zombie]))
        await _wait_until(queue.empty)
        await asyncio.sleep(0.05)
        assert sent[shown:] == []
        live = {"type": "stream", "content": "live", "origin": "owner", "turn": token, "seq": 50}
        queue.put_nowait(json.dumps([live]))
        await _wait_until(lambda: sent[-1].get("content") == "live")
        assert sent[shown:] == [
            {"type": "stream", "content": "live", "turn_id": public_turn_id(token), "relayed": True}
        ]
    finally:
        await _stop(relay)
        await pub.aclose()


async def test_an_unreadable_probe_never_closes_the_shown_turn(
    fanout_redis, monkeypatch  # noqa: F811
):
    """Another turn's frame costs a probe of the turn on screen. When Redis
    cannot answer it, that turn may still be the conversation's: it stays on
    screen and the other frame is dropped. Closed as ``lost``, every later
    frame of it would be dropped as a turn already ended."""
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = await _show_turn(fanout_redis, queue, sent, token)
    try:

        async def _unreadable(_conversation_id: str, _token: str) -> None:
            return None

        monkeypatch.setattr(chat_turn_stream, "probe_turn_end", _unreadable)
        shown = len(sent)
        other = {"type": "stream", "content": "other", "origin": "b", "turn": "t2", "seq": 1}
        live = {"type": "stream", "content": "live", "origin": "owner", "turn": token, "seq": 50}
        queue.put_nowait(json.dumps([other]))
        queue.put_nowait(json.dumps([live]))
        await _wait_until(lambda: any(f.get("content") == "live" for f in sent[shown:]))
        assert sent[shown:] == [
            {"type": "stream", "content": "live", "turn_id": public_turn_id(token), "relayed": True}
        ]
    finally:
        await _stop(relay)
        await pub.aclose()


def test_a_relay_remembers_a_bounded_number_of_ended_turns():
    """A long-lived stream sees many turns end: the relay keeps the last
    ``ENDED_TURNS_KEPT`` - enough to drop a late copy of a recent end - and
    never one entry per turn it ever saw."""
    relay = chat_turn_stream._TurnRelay(
        CONV,
        origin="viewer",
        send=_recorder()[1],
        is_closed=lambda: False,
        attached=None,
    )
    kept = chat_turn_stream.ENDED_TURNS_KEPT
    turns = [f"t{i}" for i in range(kept + 5)]
    for turn in turns:
        relay._note_end(turn)
    assert list(relay._ended) == turns[-kept:]
    assert relay._drops_late_frame(turns[-1], {"type": "stream"})
    assert not relay._drops_late_frame(turns[0], {"type": "stream"})


async def test_the_lone_terminal_of_the_turn_holding_the_lock_is_delivered(
    fanout_redis, monkeypatch  # noqa: F811
):
    """U's end and every opening frame of the next turn T were lost (Redis
    failures), and T's terminal arrives while T still holds the lock - it is
    published before the lock is released. That is the conversation's
    current turn, not a stale one: U is ended first (the terminal its
    tombstone kept) and T's answer is delivered."""
    monkeypatch.setattr(chat_turn_stream, "LIVENESS_CHECK_SECONDS", 60.0)
    queue = _scripted_queue(monkeypatch)
    sent, send = _recorder()
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=send))
    first = await chat_turn_guard.acquire_turn(CONV)
    try:
        pub = await _show_turn(fanout_redis, queue, sent, first)
        pub.offer({"type": "done", "content": "first answer", "message_id": "m-1"})
        await pub.aclose()  # published, but never reaches this listener
        assert await chat_turn_guard.release_turn(CONV, first)
        second = await chat_turn_guard.acquire_turn(CONV)
        lone = {"type": "done", "content": "second answer", "message_id": "m-2", "origin": "b", "turn": second, "seq": 6}
        queue.put_nowait(json.dumps([lone]))
        await _wait_until(lambda: any(f.get("message_id") == "m-2" for f in sent), timeout=1.0)
        closing, answer = sent[-2], sent[-1]
        assert closing["message_id"] == "m-1" and closing["turn_id"] == public_turn_id(first)
        assert answer == {
            "type": "done",
            "content": "second answer",
            "message_id": "m-2",
            "turn_id": public_turn_id(second),
            "relayed": True,
        }
    finally:
        await _stop(relay)


# ── The send connection ───────────────────────────────────────────────────


async def test_own_turn_frames_carry_the_turn_id_its_viewers_see(monkeypatch, ws_env):  # noqa: F811
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        await kwargs["on_token"]("Hello")
        await gate.wait()
        return ("Hello", None)

    _patch_general_chat(monkeypatch, _run)
    viewer, seen = await _attached_viewer(ws_env)
    conn, owner = _start(ws_env)
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in seen), timeout=4.0)
        gate.set()
        await owner
        await _wait_until(lambda: any(f["type"] == "done" for f in seen), timeout=4.0)
        own = conn.sent
        # Every frame of the turn names it, the acknowledgement of the send
        # included, written before the lock was taken.
        assert own[0]["type"] == "thinking"
        turn_ids = {f.get("turn_id") for f in own}
        assert len(turn_ids) == 1 and None not in turn_ids
        (turn_id,) = turn_ids
        relayed = [f for f in seen if f["type"] != "user_message"]
        assert relayed and {f["turn_id"] for f in relayed} == {turn_id}
        assert all(f["relayed"] is True for f in relayed)
        assert not any(f.get("relayed") for f in own)
    finally:
        gate.set()
        await _stop(viewer)


async def test_folded_send_is_told_which_turn_answers_it(monkeypatch, ws_env):  # noqa: F811
    """A send folded into the turn another send runs is answered by that turn
    - whose frames reach its stream relayed. ``folded_into_active_turn`` names
    it, so a client that ignores relayed frames (the Office add-in, voice
    mode) still knows which relayed ``done`` is its answer."""
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        await gate.wait()
        return ("answer", None)

    _patch_general_chat(monkeypatch, _run)
    first, owner = _start(ws_env, "first")
    second = None
    try:
        await _wait_until(lambda: any(f.get("status") == "tool_start" for f in first.sent), timeout=4.0)
        running = next(f["turn_id"] for f in first.sent if f.get("status") == "tool_start")
        second, folding = _start(ws_env, "and also this")
        folded = await _wait_for_frame(second, "folded_into_active_turn")
        assert folded is not None and folded["folded"] is True
        assert folded["turn_id"] == running
    finally:
        gate.set()
        await owner
        if second is not None:
            await folding


async def test_a_folded_send_hears_its_answering_turn_named_before_that_turns_frames(
    monkeypatch, ws_env  # noqa: F811
):
    """The send is folded into the running turn, and that turn ends right
    after - before the fold frame is written. The relay and the send handler
    write independently: the turn's relayed ``done`` reached the client first,
    while the client (the Office add-in, voice mode) did not know yet that it
    was its answer, and dropped it. The fold frame read the lock again, too,
    and by then the turn had released it, so it named no turn at all. The
    fold frame now comes first and names the turn that accepted the message
    - read by the fold itself - and that turn's frames follow."""
    conv_id = str(ws_env.conv.id)
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        await gate.wait()
        return ("the running turn's answer", None)

    _patch_general_chat(monkeypatch, _run)
    # The turn is over before the fold relay attaches: its end comes from the probe.
    monkeypatch.setattr(chat_module, "FOLD_RELAY_PROBE_SECONDS", 0.05)
    first, owner = _start(ws_env, "first")
    real_acquire_or_fold = chat_turn_guard.acquire_or_fold

    async def _fold_then_the_turn_ends(*args, **kwargs):
        result = await real_acquire_or_fold(*args, **kwargs)
        if result == (None, True):
            gate.set()
            await _wait_until(
                lambda: any(f["type"] == "done" for f in ws_env.redis.frames(turn_channel(conv_id))),
                timeout=4.0,
            )
            await _wait_until(lambda: not ws_env.redis.kv.get(chat_processing_key(conv_id)), timeout=4.0)
            await asyncio.sleep(0.1)  # time enough for the relay to forward it
        return result

    folding = None
    try:
        await _wait_until(lambda: any(f.get("status") == "tool_start" for f in first.sent), timeout=4.0)
        running = next(f["turn_id"] for f in first.sent if f.get("status") == "tool_start")
        monkeypatch.setattr(chat_turn_guard, "acquire_or_fold", _fold_then_the_turn_ends)
        second, folding = _start(ws_env, "and also this")
        await asyncio.wait_for(folding, 6.0)
        types = [f["type"] for f in second.sent]
        fold_at = types.index("folded_into_active_turn")
        done_at = types.index("done")
        assert fold_at < done_at, "the fold frame comes before the answering turn's frames"
        assert second.sent[fold_at]["turn_id"] == running
        assert second.sent[done_at]["turn_id"] == running
        assert second.sent[done_at]["content"] == "the running turn's answer"
    finally:
        gate.set()
        await owner
        if folding is not None and not folding.done():
            await _stop(folding)


async def test_a_folded_send_hears_the_end_of_a_turn_over_before_its_relay_attached_at_once(
    monkeypatch, ws_env  # noqa: F811
):
    """The turn the send folded into ends before the relay attaches, so its
    ``done`` never reaches the relay live. It is read from the tombstone right
    after the attachment, not two probe intervals (about 10 s) later."""
    conv_id = str(ws_env.conv.id)
    running = await chat_turn_guard.acquire_turn(conv_id)
    pub = TurnPublisher(conv_id, running, origin="other-tab")
    pub.start()
    real_acquire_or_fold = chat_turn_guard.acquire_or_fold

    async def _fold_then_the_turn_ends(*args, **kwargs):
        result = await real_acquire_or_fold(*args, **kwargs)
        pub.offer({"type": "done", "content": "the running turn's answer", "message_id": "m-1"})
        await pub.aclose()
        assert await chat_turn_guard.release_turn(conv_id, running)
        return result

    monkeypatch.setattr(chat_turn_guard, "acquire_or_fold", _fold_then_the_turn_ends)
    conn, folding = _start(ws_env, "and also this")
    try:
        await asyncio.wait_for(folding, 2.0)
        assert "folded_into_active_turn" in [f["type"] for f in conn.sent]
        assert conn.sent[-1] == {
            "type": "done",
            "content": "the running turn's answer",
            "message_id": "m-1",
            "turn_id": public_turn_id(running),
        }
    finally:
        if not folding.done():
            await _stop(folding)


async def test_a_folded_send_redis_cannot_vouch_for_is_closed_as_lost_after_the_marker_ttl(
    monkeypatch, ws_env  # noqa: F811
):
    """The relay of a folded send asks Redis whether the turn still runs. A
    turn it confirms keeps the stream open however long it runs; once Redis
    stays unreadable for the turn marker's lifetime the stream ends with the
    lost-turn terminal, rather than staying open forever."""
    conv_id = str(ws_env.conv.id)
    running = await chat_turn_guard.acquire_turn(conv_id)
    redis_answers = {"value": True}
    probes = {"n": 0}

    async def _probe(_conversation_id: str, _token: str):
        probes["n"] += 1
        return chat_turn_stream.TurnEnd(over=False) if redis_answers["value"] else None

    monkeypatch.setattr(chat_turn_stream, "probe_turn_end", _probe)
    monkeypatch.setattr(chat_module, "FOLD_RELAY_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(chat_module, "FOLD_RELAY_UNCONFIRMED_SECONDS", 0.2)
    conn, folding = _start(ws_env, "and also this")
    try:
        await _wait_until(lambda: probes["n"] >= 20, timeout=2.0)
        assert not folding.done(), "a turn Redis says runs keeps its folded send open"
        redis_answers["value"] = False
        await asyncio.wait_for(folding, 2.0)
        assert conn.sent[-1] == {**lost_turn_frame(), "turn_id": public_turn_id(running)}
    finally:
        if not folding.done():
            await _stop(folding)
        await chat_turn_guard.release_turn(conv_id, running)


async def test_rest_fold_response_names_the_turn_that_picks_the_message_up(fanout_redis):  # noqa: F811
    """The REST ``202`` says what the web chat's ``folded_into_active_turn``
    says: the turn that accepted the fold - named from the fold's own read,
    whoever holds the lock by the time the response is written - and, on the
    requeue fallback (``queued_for_next_turn``), the running turn whose
    bridge picks the persisted message up live."""
    user_message = chat_module.MessageResponse(
        id="m-1",
        role="user",
        content="and also this",
        channel="web",
        sequence=2,
        created_at="2026-09-27T21:00:00+00:00",
    )

    async def _body(folded: bool, accepted: str | None = None) -> dict:
        response = await chat_module._rest_fold_response(
            CONV, folded=folded, user_message=user_message, accepted_token=accepted
        )
        assert response.status_code == 202
        return json.loads(response.body)

    accepting = await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.release_turn(CONV, accepting)
    successor = await chat_turn_guard.acquire_turn(CONV)
    try:
        assert await _body(True, accepting) == {
            "status": "folded_into_active_turn",
            "conversation_id": CONV,
            "user_message": json.loads(user_message.model_dump_json()),
            "turn_id": public_turn_id(accepting),
        }
        # Taken by an exiting turn's release-drain: that turn is gone.
        assert "turn_id" not in await _body(True, None)
        queued = await _body(False)
        assert queued["status"] == "queued_for_next_turn"
        assert queued["turn_id"] == public_turn_id(successor)
    finally:
        assert await chat_turn_guard.release_turn(CONV, successor)
    assert "turn_id" not in await _body(False)

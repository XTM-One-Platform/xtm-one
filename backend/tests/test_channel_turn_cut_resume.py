"""A channel turn cut by a platform update is resumed, or answered, never silent.

A rolling deploy or a restart cancels the turns still running when its grace
runs out. A Slack or Teams turn on a conversation the web also reaches puts its
message back at the head of its queue for the next process and tells the
sender; one it cannot resume is told to send its message again; one already
answered is left alone (``channel_continuation.hand_on_cut_turn``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.channels.base import OutboundMessage, tell_cut_turn
from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE
from app.services import channel_continuation, chat_turn_guard
from app.services.channel_continuation import (
    CUT_HANDLED_META_KEY,
    RESUMED_META_KEY,
)
from app.services.cut_turn_resume import MAX_AUTO_RESUMES
from tests import test_channel_continuation as _continuation

CONV = _continuation.CONV
QUEUE = _continuation.QUEUE
_dm = _continuation._dm
# The fixtures of the queue's own tests: an in-memory Redis, the drains asked for.
fake_redis = _continuation.fake_redis
drains = _continuation.drains


@pytest.fixture
def told(monkeypatch):
    """The notices posted on the thread of a cut turn, by catalog key."""
    posted: list[str] = []

    async def _post(inbound, key):
        posted.append(key)
        return True

    monkeypatch.setattr(channel_continuation, "_post_turn_notice", _post)
    return posted


@pytest.fixture
def stored(monkeypatch):
    """What the database says of the cut message: ``None`` never stored, ``True`` answered."""
    state = {"answer": None}

    async def _stored_turn(conversation_id, items):
        return state["answer"]

    async def _no_files(file_ids):
        return None

    monkeypatch.setattr(channel_continuation, "_stored_turn", _stored_turn)
    monkeypatch.setattr(channel_continuation, "_mark_files_waiting", _no_files)
    return state


def _router_turn(replies: list[str], hang: bool = False):
    """A router handler holding the conversation's turn, as ``handle_message`` does."""
    from app.channels.router import _holds_channel_turn

    @_holds_channel_turn
    async def _handler(inbound, conversation_id=None, *, turn):
        if not turn.held:
            assert await turn.acquire(CONV)
        if hang:  # in the middle of a tool call
            await asyncio.sleep(3600)
        replies.append(inbound.channel_message_id)
        return OutboundMessage(text="the answer")

    return _handler


async def _cut(handler, inbound) -> None:
    """Run *handler* until it holds the turn, then cancel it as the shutdown drain does."""
    task = asyncio.create_task(handler(inbound))
    while not await chat_turn_guard.is_turn_active(CONV):
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel(msg=BACKGROUND_TURN_CANCEL_MESSAGE)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_cut_turn_is_resumed_by_the_next_process_with_one_reply_and_no_notice(
    fake_redis, drains, told, stored, monkeypatch
):
    replies: list[str] = []
    inbound = _dm("summarise the incident", message_id="msg-1")

    await _cut(_router_turn(replies, hang=True), inbound)

    # Put back at the head of its queue, for the next process's sweep: never
    # restarted on the process that is stopping.
    assert drains == []
    assert not await chat_turn_guard.is_turn_active(CONV)
    (item,) = [json.loads(raw) for raw in fake_redis.lists[QUEUE]]
    assert item["channel_message_id"] == "msg-1"
    assert item["metadata"][RESUMED_META_KEY] is True
    assert CONV in fake_redis.sets[chat_turn_guard.CHANNEL_QUEUE_INDEX_KEY]
    # The resume is silent: nothing is posted on the thread (#10772).
    assert told == []
    assert inbound.metadata[CUT_HANDLED_META_KEY] is True

    # The next process: its sweep drains the queue, the router takes the turn
    # over from the drain (the redelivery claim is not asked again) and
    # answers the same message once.
    handler = _router_turn(replies)
    seen: list[bool] = []

    async def _run(queued_inbound):
        seen.append(bool(queued_inbound.metadata.get(RESUMED_META_KEY)))
        await handler(queued_inbound)

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _run)
    assert await channel_continuation.sweep_channel_queues() == 1
    assert await channel_continuation.drain_channel_queue(CONV)
    assert replies == ["msg-1"], "exactly one reply, for the same message id"
    assert seen == [True]
    assert not fake_redis.lists.get(QUEUE)
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_a_turn_cut_again_while_resuming_is_put_back_up_to_the_cap_then_told(
    fake_redis, drains, told, stored, monkeypatch
):
    replies: list[str] = []
    inbound = _dm("long work", message_id="msg-2")
    await _cut(_router_turn(replies, hang=True), inbound)
    assert told == []

    # Every resume is cut too (several rollouts in a row): put back up to the
    # web turn's cap, then told, since a cut the turn itself causes must not loop.
    async def _run(queued_inbound):
        await _cut(_router_turn(replies, hang=True), queued_inbound)

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _run)
    for _ in range(MAX_AUTO_RESUMES):
        assert await channel_continuation.drain_channel_queue(CONV)
    assert told == ["channel_turn_cut"]
    assert not fake_redis.lists.get(QUEUE)
    assert not fake_redis.lists.get(chat_turn_guard.channel_queue_claim_key(CONV))
    assert replies == []
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_a_turn_whose_reply_was_stored_is_never_answered_twice(fake_redis, drains, told, stored):
    stored["answer"] = True
    inbound = _dm("done already", message_id="msg-3")
    await _cut(_router_turn([], hang=True), inbound)
    assert not fake_redis.lists.get(QUEUE)
    assert told == [], "already answered: nothing more is posted"


async def test_a_message_stored_without_its_reply_is_told_to_be_sent_again(fake_redis, drains, told, stored):
    stored["answer"] = False
    await _cut(_router_turn([], hang=True), _dm("half stored", message_id="msg-4"))
    assert not fake_redis.lists.get(QUEUE)
    assert told == ["channel_turn_cut"]


async def test_a_message_whose_files_were_not_stored_is_told_to_be_sent_again(fake_redis, drains, told, stored):
    from app.channels.base import InboundFileAttachment

    inbound = _dm(
        "with a file",
        message_id="msg-5",
        files=[InboundFileAttachment(url="https://files.example.test/a", filename="a.txt", content_type="text/plain")],
    )
    await _cut(_router_turn([], hang=True), inbound)
    assert not fake_redis.lists.get(QUEUE)
    assert told == ["channel_turn_cut"]


async def test_any_other_cancellation_hands_nothing_on(fake_redis, drains, told, stored):
    from app.channels.router import _holds_channel_turn

    @_holds_channel_turn
    async def _handler(inbound, conversation_id=None, *, turn):
        assert await turn.acquire(CONV)
        await asyncio.sleep(3600)

    task = asyncio.create_task(_handler(_dm("stop", message_id="msg-6")))
    while not await chat_turn_guard.is_turn_active(CONV):
        await asyncio.sleep(0)
    task.cancel()  # a Stop, a client gone: not a shutdown
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fake_redis.lists.get(QUEUE)
    assert told == []


async def test_the_adapter_tells_a_cut_turn_the_router_did_not_hand_on():
    posted: list[str] = []

    async def _post(text):
        posted.append(text)

    inbound = _dm("a group thread", message_id="msg-7")
    await tell_cut_turn(inbound, _post)
    assert len(posted) == 1 and "platform update" in posted[0]

    inbound.metadata[CUT_HANDLED_META_KEY] = True
    await tell_cut_turn(inbound, _post)
    assert len(posted) == 1, "the router already said it"


async def test_the_every_few_seconds_sweep_drains_a_cut_channel_turn(
    fake_redis: Any, drains: list[str], told: list[str], stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#11924: the new pod answers a cut Teams or Slack turn within one sweep interval, not at the minute tick."""
    from app.core import worker_shutdown
    from app.services import cut_turn_resume

    monkeypatch.setattr(worker_shutdown, "_request", None)
    cut: Any = _cut
    await cut(_router_turn_any([], hang=True), _dm_any("summarise the incident", message_id="msg-8"))
    assert drains == []

    assert await cut_turn_resume.sweep_channel_turns_once() == 1
    assert drains == [CONV]


async def test_a_stopping_process_drains_no_channel_queue(
    fake_redis: Any, drains: list[str], told: list[str], stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn started on a pod being stopped is cut again by its drain: the next pod takes it."""
    from app.core import worker_shutdown
    from app.services import cut_turn_resume

    monkeypatch.setattr(worker_shutdown, "_request", None)
    cut: Any = _cut
    await cut(_router_turn_any([], hang=True), _dm_any("long work", message_id="msg-9"))
    monkeypatch.setattr(
        worker_shutdown,
        "_request",
        worker_shutdown.ShutdownRequest(reason=worker_shutdown.STOP, detail="test", requested_at=0.0),
    )
    assert await channel_continuation.sweep_channel_queues() == 0
    assert await cut_turn_resume.sweep_channel_turns_once() == 0
    assert drains == []
    assert fake_redis.lists[QUEUE], "left queued for the next process"


async def test_the_channel_sweep_never_raises_into_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import cut_turn_resume

    async def _boom() -> int:
        raise RuntimeError("redis down")

    monkeypatch.setattr(channel_continuation, "sweep_channel_queues", _boom)
    assert await cut_turn_resume.sweep_channel_turns_once() == 0


_router_turn_any: Any = _router_turn
_dm_any: Any = _dm

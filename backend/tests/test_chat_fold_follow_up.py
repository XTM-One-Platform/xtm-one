"""A message folded into a turn after its last look is answered.

A turn stops reading its steering inbox at the fold-in loop's last leftover
check, yet holds the conversation's lock until its reply is stored. A fold
landing in between - a steer or a send on another replica, a wake - sees the
lock and tells its sender the turn took it. Pins, on the real turn guard (a
fake Redis), the web chat's send connection and the REST handler:

* such a fold is handed to a follow-up turn once the lock is released, never
  only logged; a Stop asks none;
* the bridge, stopped at the turn's end, queues the batch it holds instead of
  losing it to a cancellation;
* what the turn's queue holds after the loop comes back as the inbox entries
  it was bridged from, for that follow-up.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from app.api.v1 import chat as chat_module
from app.services import chat_turn_guard, parent_wake
from app.services.chat_turn_guard import SteeringPayload, processing_key
from app.tools.agent_loop import SteeringMessage
from tests.test_chat_turn_fanout import (  # noqa: F401
    _patch_general_chat,
    fake_rl,
    fanout_redis,
    ws_env,
)
from tests.test_chat_web_steering import _FakeConnection, _FakeSession, _run_connection

LATE = "and compare it with last quarter"


@pytest.fixture
def follow_ups(monkeypatch, ws_env):  # noqa: F811
    """Every follow-up a turn's end asks for, and whether the lock was free by then."""
    calls: list[SimpleNamespace] = []

    def _schedule(*, conversation_id, user_id, folds):
        calls.append(
            SimpleNamespace(
                conversation_id=conversation_id,
                user_id=user_id,
                texts=[fold.text for fold in folds],
                lock_free=processing_key(conversation_id) not in ws_env.redis.kv,
            )
        )
        return True

    monkeypatch.setattr(parent_wake, "schedule_fold_follow_up", _schedule, raising=False)
    return calls


def _fold_while_the_reply_is_stored(monkeypatch) -> list[bool]:
    """Another replica folds a message as the turn stores its reply: past the
    last leftover check, the lock still held. Returns what the fold answered."""
    folded: list[bool] = []
    original = chat_module.store_message

    async def _store(db, conv_id, role, content, **kw):
        if role == "assistant" and not folded:
            folded.append(await chat_turn_guard.fold_into_inflight_turn(str(conv_id), LATE))
        return await original(db, conv_id, role, content, **kw)

    monkeypatch.setattr(chat_module, "store_message", _store)
    return folded


def _answers(*texts: str):
    replies = iter(texts)

    async def _run(_db, _uid, _history, _content, **_kwargs):
        return (next(replies), None)

    return _run


async def _send(env, content: str = "summarise the risks", **kw) -> _FakeConnection:
    conn = _FakeConnection(str(env.conv.id), {"content": content}, env.timeline, **kw)
    # No wall-clock budget: the scripted turn ends by itself, and a budget
    # expires on a loaded xdist worker wherever the turn is (#1915).
    await _run_connection(conn, env.user_id)
    return conn


async def _rest_send(env, content: str = "summarise the risks"):
    return await chat_module.send_message(
        conversation_id=str(env.conv.id),
        body=chat_module.SendMessageRequest(content=content),
        user=SimpleNamespace(id=env.user_id),
        db=_FakeSession(env.conv, env.timeline),
    )


async def test_a_fold_taken_after_the_last_leftover_check_is_followed_up(
    monkeypatch, ws_env, follow_ups  # noqa: F811
):
    _patch_general_chat(monkeypatch, _answers("final answer"))
    folded = _fold_while_the_reply_is_stored(monkeypatch)

    conn = await _send(ws_env)

    conv_id = str(ws_env.conv.id)
    assert folded == [True], "the guard told the sender the turn took it"
    assert [(c.conversation_id, c.user_id, c.texts, c.lock_free) for c in follow_ups] == [
        (conv_id, str(ws_env.user_id), [LATE], True)
    ]
    assert [f["content"] for f in conn.sent if f.get("type") == "done"] == ["final answer"]
    assert await chat_turn_guard.drain_steering_inbox(conv_id) == []


async def test_a_rest_turn_follows_up_a_fold_taken_as_it_stores_its_reply(
    monkeypatch, ws_env, follow_ups  # noqa: F811
):
    _patch_general_chat(monkeypatch, _answers("final answer"))
    folded = _fold_while_the_reply_is_stored(monkeypatch)

    await _rest_send(ws_env)

    assert folded == [True]
    assert [(c.texts, c.lock_free) for c in follow_ups] == [([LATE], True)]


async def test_a_stopped_turn_follows_nothing_up(monkeypatch, ws_env, follow_ups):  # noqa: F811
    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["cancel_event"].wait()
        return ("cancelled mid-run", None)

    _patch_general_chat(monkeypatch, _run)
    folded = _fold_while_the_reply_is_stored(monkeypatch)

    conn = await _send(ws_env, then=[{"type": "cancel"}])

    assert any(f.get("type") == "cancelled" for f in conn.sent)
    assert folded == [True]
    assert follow_ups == []


def _stop_while_the_reply_is_stored(monkeypatch, stop) -> list[bool]:
    """A fold lands, then the user presses Stop, as the turn stores its reply:
    past the turn's last pass. Returns what the fold answered."""
    folded: list[bool] = []
    original = chat_module.store_message

    async def _store(db, conv_id, role, content, **kw):
        if role == "assistant" and not folded:
            folded.append(await chat_turn_guard.fold_into_inflight_turn(str(conv_id), LATE))
            await stop(str(conv_id))
        return await original(db, conv_id, role, content, **kw)

    monkeypatch.setattr(chat_module, "store_message", _store)
    # The cascade's sub-agent cancellation reads the database.
    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", lambda *_a, **_kw: None)
    return folded


async def test_a_stop_during_the_persistence_follows_nothing_up(
    monkeypatch, ws_env, follow_ups  # noqa: F811
):
    """The Stop route reaches the turn on its replica while no pass reads its
    frames: the turn guard's record of the Stop still asks no follow-up. The
    steer stays in the thread for the next turn's history."""
    from app.api.v1 import chat_web_stream

    async def _stop(conversation_id: str) -> None:
        answer = await chat_web_stream.cancel_web_chat_turn(
            chat_web_stream.WebChatCancelRequest(conversation_id=conversation_id),
            SimpleNamespace(headers={}),
            db=SimpleNamespace(execute=_FakeSession(ws_env.conv, []).execute),
            user=SimpleNamespace(id=ws_env.user_id),
        )
        assert answer["turn"] == "local"

    _patch_general_chat(monkeypatch, _answers("final answer"))
    folded = _stop_while_the_reply_is_stored(monkeypatch, _stop)

    conn = await _send(ws_env)

    assert folded == [True]
    assert [f["content"] for f in conn.sent if f.get("type") == "done"] == ["final answer"]
    assert follow_ups == []


async def test_a_rest_turn_stopped_during_its_persistence_follows_nothing_up(
    monkeypatch, ws_env, follow_ups  # noqa: F811
):
    async def _stop(conversation_id: str) -> None:
        async def _request_cancel() -> bool:
            return await chat_turn_guard.request_turn_cancel(conversation_id)

        assert await chat_module.stop_conversation_work(conversation_id, _request_cancel)

    _patch_general_chat(monkeypatch, _answers("final answer"))
    folded = _stop_while_the_reply_is_stored(monkeypatch, _stop)

    await _rest_send(ws_env)

    assert folded == [True]
    assert follow_ups == []


_ISSUED_AT = 1_790_000_000
_REAL_ACCOUNT_REJECTION = chat_module.resolve_account_rejection


def _change_the_account_while_the_reply_is_stored(monkeypatch, change) -> list[bool]:
    """A fold lands, then *change* is made to the account, as the turn stores
    its reply: the account check runs against that ``users`` row. Returns
    what the fold answered."""
    from tests.test_deactivated_account_auth import _Account, _ChatSession

    account = _Account(is_active=True)

    async def _account_check(_db, user_id, issued_at, **kwargs):
        return await _REAL_ACCOUNT_REJECTION(_ChatSession(account), user_id, issued_at, **kwargs)

    monkeypatch.setattr(chat_module, "resolve_account_rejection", _account_check)
    folded: list[bool] = []
    original = chat_module.store_message

    async def _store(db, conv_id, role, content, **kw):
        if role == "assistant" and not folded:
            folded.append(await chat_turn_guard.fold_into_inflight_turn(str(conv_id), LATE))
            change(account)
        return await original(db, conv_id, role, content, **kw)

    monkeypatch.setattr(chat_module, "store_message", _store)
    return folded


def _deactivate(account) -> None:
    account.is_active = False


def _change_password(account) -> None:
    from datetime import UTC, datetime

    account.password_changed_at = datetime.fromtimestamp(_ISSUED_AT + 60, UTC)


def _unchanged(_account) -> None:
    return None


@pytest.mark.parametrize(
    ("change", "session", "followed"),
    [
        (_deactivate, True, False),
        (_change_password, True, False),
        (_deactivate, False, False),
        (_change_password, False, True),
        (_unchanged, True, True),
    ],
    ids=[
        "session-deactivated",
        "session-password-changed",
        "api-key-deactivated",
        "api-key-password-changed",
        "session-unchanged",
    ],
)
async def test_a_rest_turn_follows_up_only_for_an_account_still_allowed_to_chat(
    monkeypatch, ws_env, follow_ups, change, session, followed  # noqa: F811
):
    """A follow-up is new work on the account's behalf: the REST turn checks
    the account before asking one, like the streamed turn. A password change
    ends a session, not an API key."""
    _patch_general_chat(monkeypatch, _answers("final answer"))
    folded = _change_the_account_while_the_reply_is_stored(monkeypatch, change)
    user = SimpleNamespace(id=ws_env.user_id)
    if session:
        user.web_session = SimpleNamespace(claims={"iat": _ISSUED_AT})

    await chat_module.send_message(
        conversation_id=str(ws_env.conv.id),
        body=chat_module.SendMessageRequest(content="summarise the risks"),
        user=user,
        db=_FakeSession(ws_env.conv, ws_env.timeline),
    )

    assert folded == [True]
    assert [c.texts for c in follow_ups] == ([[LATE]] if followed else [])


async def test_a_stop_recorded_before_the_turn_took_the_lock_is_not_its_own(
    monkeypatch, ws_env, follow_ups  # noqa: F811
):
    from datetime import UTC, datetime, timedelta

    await chat_turn_guard.record_stop(str(ws_env.conv.id), datetime.now(UTC) - timedelta(seconds=5))
    _patch_general_chat(monkeypatch, _answers("final answer"))
    folded = _fold_while_the_reply_is_stored(monkeypatch)

    await _send(ws_env)

    assert folded == [True]
    assert [c.texts for c in follow_ups] == [[LATE]]


async def test_a_fold_the_bridge_queued_after_the_loop_is_followed_up(
    monkeypatch, ws_env, follow_ups  # noqa: F811
):
    """A fold the bridge reads while the turn finishes - after the loop
    returned, before its end stops the bridge - lands on a queue no loop
    reads any more: the turn's end hands it to the follow-up too."""
    bridged = asyncio.Event()
    original_put = parent_wake.PartnerAnswerQueue.put_fold

    def _put_fold(self, item, payload):
        original_put(self, item, payload)
        bridged.set()

    monkeypatch.setattr(parent_wake.PartnerAnswerQueue, "put_fold", _put_fold)
    original_admit = chat_module._admit_leftover_steers

    async def _admit(queue, leftover, steered):
        admitted = await original_admit(queue, leftover, steered)
        if not admitted and not bridged.is_set():
            # The leftover check has emptied the queue: the bridge, still
            # running, queues this fold where no loop reads it.
            assert await chat_turn_guard.fold_into_inflight_turn(str(ws_env.conv.id), LATE)
            await bridged.wait()
        return admitted

    monkeypatch.setattr(chat_module, "_admit_leftover_steers", _admit)
    _patch_general_chat(monkeypatch, _answers("final answer"))

    await _send(ws_env)

    assert [(c.texts, c.lock_free) for c in follow_ups] == [([LATE], True)]


# ── The guard's pieces ────────────────────────────────────────────────────


async def test_a_stopped_bridge_queues_the_batch_it_holds():
    """Stopped at the turn's end, the bridge finishes the batch it took off
    the inbox - cancelled, that batch would be in neither the inbox nor the
    queue."""
    stop = asyncio.Event()
    took = asyncio.Event()
    queued: list[str] = []

    async def _bridge():
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 0.01)
            if stop.is_set():
                return
            took.set()
            await asyncio.sleep(0.05)  # resolving the batch's attachments
            queued.append("batch")

    task = asyncio.create_task(_bridge())
    await took.wait()
    await chat_turn_guard.stop_bridge(task, stop)

    assert queued == ["batch"]
    assert task.done() and not task.cancelled()


async def test_a_bridge_that_does_not_stop_in_time_is_cancelled(monkeypatch):
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_STOP_SECONDS", 0.05)
    task = asyncio.create_task(asyncio.Event().wait())

    await chat_turn_guard.stop_bridge(task, asyncio.Event())

    assert task.cancelled()


async def test_what_the_loop_never_took_comes_back_as_its_inbox_entries():
    queue = parent_wake.PartnerAnswerQueue("conv")
    taken = SteeringPayload(text="taken")
    late = SteeringPayload(text="late", file_ids=["f-1"])
    late_item = SteeringMessage(text="late", files=[])
    queue.put_fold("taken", taken)
    # The loop takes the first at an iteration boundary.
    assert await queue.admit([queue.get_nowait()]) == ["taken"]
    queue.put_fold(late_item, late)
    queue.put_nowait("put without an entry")

    unanswered = queue.take_unanswered()

    assert unanswered[0] is late
    assert [p.text for p in unanswered] == ["late", "put without an entry"]
    assert queue.take_unanswered() == []


def test_a_fold_handed_back_for_its_mentions_only_is_not_followed_up(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(parent_wake, "_spawn_wake", lambda coro, *, label: (spawned.append(label), coro.close()))
    handed_back = SteeringPayload(text="answered", mentioned_agent_ids=["a"], mentions_only=True)

    assert not parent_wake.schedule_fold_follow_up(conversation_id="c", user_id="u", folds=[handed_back])
    assert parent_wake.schedule_fold_follow_up(
        conversation_id="c", user_id="u", folds=[handed_back, SteeringPayload(text=LATE)]
    )
    assert spawned == ["c"]

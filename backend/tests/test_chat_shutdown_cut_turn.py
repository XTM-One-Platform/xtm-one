"""A chat turn cut by the graceful shutdown timeout keeps a partial reply.

On the web chat's real send connection and the REST handler, with the
fan-out fakes: a turn whose request uvicorn's bounded drain cancels in the
middle of a tool call stores an
assistant message that names what it did (the completed calls, the calls still
running, the last completed action) with its tool trace and the
``interrupted`` marker, before the turn's lock goes; the checkpoint's latest
snapshot is flushed for the next turn. Any other cancellation stores nothing,
as before.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app.services.agent_chat as agent_chat_module
from app.api.v1 import chat as chat_module
from app.core import worker_shutdown
from app.core.shutdown_finalizers import drain_shutdown_finalizers
from app.services import chat_turn_guard, cut_turn_resume, turn_checkpoint
from app.services.interrupted_turn import (
    INTERRUPTED_CAUSE_META_KEY,
    INTERRUPTED_META_KEY,
    cut_cause_text,
)
from tests.test_chat_turn_fanout import (  # noqa: F401
    _patch_general_chat,
    fake_rl,
    fanout_redis,
    ws_env,
)
from tests.test_chat_web_steering import _FakeConnection, _FakeSession, _run_connection
from tests.test_mention_turn import B, mention_env  # noqa: F401

UVICORN_DRAIN_CANCEL_MSG = "Task cancelled, timeout graceful shutdown exceeded"


@pytest.fixture(autouse=True)
def _no_recorded_stop(monkeypatch):
    """No test of this module asked the worker to stop, unless it says so."""
    monkeypatch.setattr(worker_shutdown, "_request", None)


@pytest.fixture
def replies(monkeypatch, ws_env):  # noqa: F811
    """Every assistant message stored, its metadata, and whether its turn still held the lock."""
    stored: list[SimpleNamespace] = []
    original = chat_module.store_message

    async def _store(db, conv_id, role, content, **kw):
        if role == "assistant":
            stored.append(
                SimpleNamespace(
                    content=content,
                    meta=json.loads(kw.get("metadata_json") or "{}"),
                    lock_held=await chat_turn_guard.is_turn_active(str(conv_id)),
                )
            )
        return await original(db, conv_id, role, content, **kw)

    monkeypatch.setattr(chat_module, "store_message", _store)
    return stored


@pytest.fixture
def checkpoint_closes(monkeypatch) -> list[bool]:
    """The ``flush`` of every checkpoint close."""
    flushes: list[bool] = []
    original = turn_checkpoint.TurnCheckpoint.aclose

    async def _aclose(self, *, flush: bool) -> None:
        flushes.append(flush)
        await original(self, flush=flush)

    monkeypatch.setattr(turn_checkpoint.TurnCheckpoint, "aclose", _aclose)
    return flushes


async def _work(on_event, started: asyncio.Event, *, prefix: str = "") -> None:
    """Two calls started, one returned, then a hang inside the other."""
    await on_event({"status": "thinking", "iteration": 1})
    await on_event({"status": "tool_start", "tools": [f"{prefix}search_web", f"{prefix}read_page"], "iteration": 1})
    await on_event(
        {
            "status": "tool_done",
            "tool": f"{prefix}search_web",
            "success": True,
            "tool_input": '{"q": "quarterly risks"}',
            "tool_output": "3 hits",
        }
    )
    started.set()
    await asyncio.Event().wait()


def _working_general_chat(started: asyncio.Event):
    async def _run(_db, _uid, _history, _content, **kwargs):
        await _work(kwargs["on_event"], started)
        raise AssertionError("unreachable")

    return _run


async def _cut(task: asyncio.Task, started: asyncio.Event, *, msg: str | None = UVICORN_DRAIN_CANCEL_MSG) -> None:
    await asyncio.wait_for(started.wait(), 5)
    if msg is None:
        task.cancel()
    else:
        task.cancel(msg=msg)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await drain_shutdown_finalizers(timeout=5) == 0


def _assert_partial_reply(reply: SimpleNamespace, *, prefix: str = "", cause: str | None = None) -> None:
    assert reply.content.endswith(
        f"This turn was interrupted before it could finish: {cut_cause_text(cause)}.\n\n"
        "Completed tool calls (1):\n"
        f"- `{prefix}search_web`\n\n"
        "Still running when it stopped (1):\n"
        f"- `{prefix}read_page`\n\n"
        f"Last completed action: `{prefix}search_web`."
    )
    assert reply.meta[INTERRUPTED_META_KEY] is True
    assert reply.meta[INTERRUPTED_CAUSE_META_KEY] == cause
    assert reply.meta["tool_names"] == [f"{prefix}search_web", f"{prefix}read_page"]
    assert reply.meta["tool_call_count"] == 2
    assert reply.meta["iterations"] == 1
    assert reply.meta["tool_call_trace"] == [
        {"name": f"{prefix}search_web", "input": '{"q": "quarterly risks"}', "output": "3 hits", "success": True}
    ]
    # Readable before the lock goes, so a viewer told the turn was interrupted
    # reads it back.
    assert reply.lock_held is True


def _connection_turn(env) -> asyncio.Task:
    """The same send on a bare send connection, run as the send route runs it."""
    conn = _FakeConnection(str(env.conv.id), {"content": "research the quarterly risks"}, env.timeline)
    return asyncio.create_task(_run_connection(conn, env.user_id))


def _rest_send(env, content: str, **fields):
    return chat_module.send_message(
        conversation_id=str(env.conv.id),
        body=chat_module.SendMessageRequest(content=content, **fields),
        user=SimpleNamespace(id=env.user_id),
        db=_FakeSession(env.conv, env.timeline),
    )


def _sse_turn(env, monkeypatch) -> asyncio.Task:
    """The web chat's SSE send of the same message, in its request task."""
    from app.services.chat_web_transport import DetachedTurnStreamResponse, HeadlessTurnConnection

    async def _session_holds(session):
        return session

    monkeypatch.setattr(chat_module, "recheck_session", _session_holds)
    conversation_id = str(env.conv.id)
    connection = HeadlessTurnConnection(conversation_id, {"content": "research the quarterly risks"})
    auth = chat_module.ChatConnectionAuth(user_id=env.user_id, session=SimpleNamespace())

    async def _run(conn):
        await chat_module.run_chat_connection(conn, conversation_id, auth)

    async def _client_stays():
        await asyncio.Event().wait()

    async def _written(_message):
        return None

    return asyncio.create_task(DetachedTurnStreamResponse(connection, _run)({"type": "http"}, _client_stays, _written))


# ---------------------------------------------------------------------------
# Web chat send
# ---------------------------------------------------------------------------


async def test_an_sse_turn_cut_by_the_drain_stores_what_it_did(monkeypatch, ws_env, replies, checkpoint_closes):  # noqa: F811
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))

    await _cut(_sse_turn(ws_env, monkeypatch), started)

    [reply] = replies
    _assert_partial_reply(reply)
    assert checkpoint_closes == [True]
    conv_id = str(ws_env.conv.id)
    assert not await chat_turn_guard.is_turn_active(conv_id)
    # The interrupted-turn notice still offers to resume the work.
    assert await chat_turn_guard.interrupted_turn(conv_id) is not None


@pytest.mark.parametrize(
    "reason", [worker_shutdown.STOP, worker_shutdown.RECYCLE], ids=["stop", "recycle"]
)
async def test_an_sse_turn_cut_says_what_stopped_its_worker(monkeypatch, ws_env, replies, reason):  # noqa: F811
    """A deploy and a recycle are told apart, never "a server restart"."""
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))
    task = _sse_turn(ws_env, monkeypatch)
    await asyncio.wait_for(started.wait(), 5)
    worker_shutdown.note_shutdown(reason, "SIGTERM")

    await _cut(task, started)

    [reply] = replies
    _assert_partial_reply(reply, cause=reason)


@pytest.fixture
def queued(monkeypatch) -> list[dict]:
    """The cut turns handed to the automatic resume queue, and its answer."""
    calls: list[dict] = []

    async def _enqueue(conversation_id, **kw):
        calls.append({"conversation_id": str(conversation_id), **kw})
        return cut_turn_resume.resumable_cause(kw.get("cause"))

    monkeypatch.setattr(chat_module, "enqueue_cut_turn", _enqueue)
    return calls


@pytest.mark.parametrize(
    "reason", [worker_shutdown.STOP, worker_shutdown.RECYCLE], ids=["stop", "recycle"]
)
async def test_an_sse_turn_cut_by_a_platform_stop_is_queued_for_its_resume(monkeypatch, ws_env, replies, queued, reason):  # noqa: F811
    """No click: the next process resumes the same user message."""
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))
    task = _sse_turn(ws_env, monkeypatch)
    await asyncio.wait_for(started.wait(), 5)
    worker_shutdown.note_shutdown(reason, "SIGTERM")

    await _cut(task, started)

    [call] = queued
    assert call["conversation_id"] == str(ws_env.conv.id)
    assert call["cause"] == reason
    assert call["entry"] == "web"
    assert call["user_message_id"] is not None
    [reply] = replies
    assert reply.meta[cut_turn_resume.AUTO_RESUME_META_KEY] is True


async def test_an_sse_turn_cut_by_the_drain_with_no_recorded_stop_still_resumes(monkeypatch, ws_env, replies, queued):  # noqa: F811
    """#7032: only a stopping server's drain raises this cancellation, so it is a
    platform stop even when the stop hook recorded no cause; the reply still
    says the cause is unknown, and carries the lock token the sweep frees."""
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))

    await _cut(_sse_turn(ws_env, monkeypatch), started)

    [call] = queued
    assert call["cause"] == worker_shutdown.STOP
    assert call["lock_token"]
    [reply] = replies
    assert reply.meta[cut_turn_resume.AUTO_RESUME_META_KEY] is True
    assert reply.meta[INTERRUPTED_CAUSE_META_KEY] is None


async def test_an_sse_turn_cancelled_otherwise_stores_nothing(monkeypatch, ws_env, replies, checkpoint_closes):  # noqa: F811
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))

    await _cut(_sse_turn(ws_env, monkeypatch), started, msg=None)

    assert replies == []
    assert checkpoint_closes == []
    assert not await chat_turn_guard.is_turn_active(str(ws_env.conv.id))


async def test_a_turn_reports_only_what_followed_its_last_stored_message(monkeypatch, ws_env, replies):  # noqa: F811
    """A mid-turn segment is stored with the calls before it: the cut reply
    names the work after it."""
    started = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "tool_start", "tools": ["list_assets"], "iteration": 1})
        await on_event({"status": "tool_done", "tool": "list_assets", "success": True, "tool_output": "12"})
        await on_event({"status": "segment_complete", "content": "Found 12 assets, checking each."})
        await _work(on_event, started)

    _patch_general_chat(monkeypatch, _run)

    await _cut(_connection_turn(ws_env), started)

    segment, reply = replies
    assert segment.content == "Found 12 assets, checking each."
    assert [e["name"] for e in segment.meta["tool_call_trace"]] == ["list_assets"]
    _assert_partial_reply(reply)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_a_rest_turn_cut_by_the_drain_stores_what_it_did(monkeypatch, ws_env, replies, checkpoint_closes):  # noqa: F811
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))

    await _cut(asyncio.create_task(_rest_send(ws_env, "research the quarterly risks")), started)

    [reply] = replies
    _assert_partial_reply(reply)
    assert checkpoint_closes == [True]
    assert not await chat_turn_guard.is_turn_active(str(ws_env.conv.id))


async def test_a_rest_turn_cut_by_a_platform_stop_is_queued_for_its_resume(monkeypatch, ws_env, replies, queued):  # noqa: F811
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))
    task = asyncio.create_task(_rest_send(ws_env, "research the quarterly risks"))
    await asyncio.wait_for(started.wait(), 5)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    await _cut(task, started)

    [call] = queued
    assert call["cause"] == worker_shutdown.STOP
    assert call["entry"] == "rest"
    assert call["user_id"] == ws_env.user_id
    assert call["lock_token"]
    [reply] = replies
    assert reply.meta[cut_turn_resume.AUTO_RESUME_META_KEY] is True


async def test_a_rest_turn_cancelled_otherwise_stores_nothing(monkeypatch, ws_env, replies, checkpoint_closes, queued):  # noqa: F811
    started = asyncio.Event()
    _patch_general_chat(monkeypatch, _working_general_chat(started))

    await _cut(asyncio.create_task(_rest_send(ws_env, "research the quarterly risks")), started, msg=None)

    assert queued == []
    assert replies == []
    assert checkpoint_closes == []
    assert not await chat_turn_guard.is_turn_active(str(ws_env.conv.id))


async def test_a_rest_turn_cut_while_a_mentioned_agent_answers_keeps_the_first_answer(monkeypatch, mention_env):  # noqa: F811
    """The conversation agent had answered, the agent its message mentions was
    still working: the stored message keeps that answer, then names the work."""
    started = asyncio.Event()

    async def _general(_db, _uid, _history, _content, **_kwargs):
        return ("assistant says", None)

    async def _beta(_db, _uid, _agent, _history, _content, **kwargs):
        await _work(kwargs["on_event"], started, prefix="beta_")
        raise AssertionError("unreachable")

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta)

    await _cut(asyncio.create_task(_rest_send(mention_env.env, "@beta your view?", mentioned_agent_ids=[B])), started)

    [(_role, content, kw)] = [entry for entry in mention_env.stored if entry[0] == "assistant"]
    meta = json.loads(kw["metadata_json"])
    assert content.startswith("assistant says\n\nThis turn was interrupted before it could finish")
    _assert_partial_reply(SimpleNamespace(content=content, meta=meta, lock_held=True), prefix="beta_")

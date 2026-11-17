"""Stop and "Continue now" for a turn on another replica, against a real Redis (T039).

The web chat's Stop and "Continue now" reach a turn this process does not
run through Redis requests addressed to the turn's lock token; the running
turn's steering bridge consumes them on its poll. These run the real turn
(``chat.run_chat_connection`` behind the SSE send, with the steering suite's
fakes around it) and its real bridge, at the real poll period, against the
Redis at ``REDIS_URL``, and skip without one. Every key carries the test's
fresh conversation id and is deleted afterwards.

* a Stop and a "Continue now" sent while the turn is not on this replica
  reach it within 2 s;
* a Stop with ``awaiting_send`` is kept for that client's send and ends its
  turn as it starts; without it nothing is kept, and a kept one is dropped;
* a Redis that cannot carry the request answers 503, and a Stop it recorded
  before failing is withdrawn.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
import redis.asyncio as redis_asyncio
from fastapi import HTTPException

import app.core.redis as redis_module
from app.api.v1 import chat as chat_module
from app.api.v1 import chat_web_stream
from app.services import chat_turn_guard, chat_web_transport
from tests import redis_unreachable
from tests.test_chat_web_sse_transport import (  # noqa: F401 - fixtures
    _REAL_STOP_CONVERSATION_WORK,
    _Asgi,
    _assistant_answers,
    _cancel,
    _cancel_seen_at_start,
    _request,
    _stream,
    web_env,
)
from tests.test_chat_web_steering import (  # noqa: F401 - fixtures
    _FakeSession,
    _patch_general_chat,
    fake_rl,
    ws_env,
)

pytestmark = pytest.mark.integration

# The bridge's real poll, read before ``web_env`` shortens it.
_BRIDGE_POLL_SECONDS = chat_turn_guard.BRIDGE_POLL_SECONDS
_REACH_SECONDS = 2.0


@pytest.fixture
async def real_redis(monkeypatch, web_env):  # noqa: F811
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")

    async def _client():
        return client

    monkeypatch.setattr(redis_module, "redis_client", _client)
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", _BRIDGE_POLL_SECONDS)
    try:
        yield client
    finally:
        keys = [key async for key in client.scan_iter(match=f"*{web_env.conv.id}*", count=500)]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def _start_turn(env, monkeypatch, run) -> tuple[_Asgi, asyncio.Task]:
    _patch_general_chat(monkeypatch, run)
    asgi = _Asgi()
    _connection, response = _stream(env, {"content": "long job"})
    return asgi, asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))


def _off_this_replica(monkeypatch) -> None:
    monkeypatch.setattr(chat_web_transport, "live_connection", lambda _cid: None)
    monkeypatch.setattr(chat_web_stream, "live_connection", lambda _cid: None)


async def _skip_wait(env) -> dict:
    return await chat_web_stream.skip_web_chat_wait(
        chat_web_stream.WebChatTurnRequest(conversation_id=str(env.conv.id)),
        db=_FakeSession(env.conv, []),
        user=SimpleNamespace(id=env.user_id),
    )


async def test_a_stop_for_a_turn_on_another_replica_reaches_it_within_two_seconds(
    real_redis,
    web_env,  # noqa: F811
    monkeypatch,
):
    conversation_id = str(web_env.conv.id)
    loop = asyncio.get_running_loop()
    turn_running = asyncio.Event()
    reached_at: list[float] = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        turn_running.set()
        await asyncio.wait_for(kwargs["cancel_event"].wait(), 5)
        reached_at.append(loop.time())
        return ("The request was cancelled.", None)

    asgi, task = _start_turn(web_env, monkeypatch, _run)
    await asyncio.wait_for(turn_running.wait(), 3)
    holder = await real_redis.get(chat_turn_guard.processing_key(conversation_id))
    assert holder, "the turn holds no lock in Redis"
    _off_this_replica(monkeypatch)

    asked_at = loop.time()
    answer = await _cancel(web_env)
    await asyncio.wait_for(task, 5)

    assert answer == {"status": "cancelled", "turn": "remote"}
    assert reached_at and reached_at[0] - asked_at < _REACH_SECONDS
    assert asgi.frames[-1]["type"] == "cancelled"
    assert not await real_redis.exists(chat_turn_guard.cancel_request_key(conversation_id))
    assert not await real_redis.exists(chat_turn_guard.processing_key(conversation_id))


async def test_continue_now_for_a_turn_on_another_replica_reaches_it_within_two_seconds(
    real_redis,
    web_env,  # noqa: F811
    monkeypatch,
):
    conversation_id = str(web_env.conv.id)
    loop = asyncio.get_running_loop()
    turn_running = asyncio.Event()
    reached_at: list[float] = []
    cancelled: list[bool] = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        turn_running.set()
        await asyncio.wait_for(kwargs["wait_skip_event"].wait(), 5)
        reached_at.append(loop.time())
        cancelled.append(kwargs["cancel_event"].is_set())
        return ("carried on", None)

    asgi, task = _start_turn(web_env, monkeypatch, _run)
    await asyncio.wait_for(turn_running.wait(), 3)
    assert await real_redis.get(chat_turn_guard.processing_key(conversation_id))
    _off_this_replica(monkeypatch)

    asked_at = loop.time()
    answer = await _skip_wait(web_env)
    await asyncio.wait_for(task, 5)

    assert answer == {"status": "wait_skipped", "turn": "remote"}
    assert reached_at and reached_at[0] - asked_at < _REACH_SECONDS
    assert cancelled == [False]
    assert _assistant_answers(web_env) == ["carried on"]
    assert not any(f["type"] == "cancelled" for f in asgi.frames)
    assert not await real_redis.exists(chat_turn_guard.wait_skip_request_key(conversation_id))


async def test_a_stop_kept_for_a_send_on_its_way_ends_that_turn_as_it_starts(
    real_redis,
    web_env,  # noqa: F811
    monkeypatch,
):
    seen = _cancel_seen_at_start(monkeypatch)
    kept = chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-1")

    assert await _cancel(web_env, "tab-1", awaiting_send=True) == {"status": "cancelled", "turn": "none"}
    assert 0 < await real_redis.ttl(kept) <= chat_turn_guard.PENDING_CANCEL_TTL_SECONDS

    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "long job"}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert seen == [True]
    assert asgi.frames[-1]["type"] == "cancelled"
    assert not await real_redis.exists(kept)


async def test_a_stop_with_no_send_on_its_way_keeps_nothing_and_drops_a_kept_one(
    real_redis,
    web_env,  # noqa: F811
    monkeypatch,
):
    seen = _cancel_seen_at_start(monkeypatch)
    kept = chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-1")

    assert (await _cancel(web_env, "tab-1"))["turn"] == "none"
    assert not await real_redis.exists(kept)

    await _cancel(web_env, "tab-1", awaiting_send=True)
    assert await real_redis.exists(kept)
    assert (await _cancel(web_env, "tab-1"))["turn"] == "none"
    assert not await real_redis.exists(kept)

    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "next question"}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert seen == [False]
    assert asgi.frames[-1]["type"] == "done"


async def test_stop_and_continue_now_answer_503_when_redis_cannot_carry_them(web_env, monkeypatch):  # noqa: F811
    client, _pool = redis_unreachable.unreachable_redis()

    async def _refusing():
        return client

    monkeypatch.setattr(redis_module, "redis_client", _refusing)
    try:
        with pytest.raises(HTTPException) as stop:
            await _cancel(web_env)
        with pytest.raises(HTTPException) as skip:
            await _skip_wait(web_env)
    finally:
        await client.aclose()

    assert stop.value.status_code == 503
    assert skip.value.status_code == 503


async def test_a_stop_recorded_before_its_request_failed_is_withdrawn(
    real_redis,
    web_env,  # noqa: F811
    monkeypatch,
):
    """A 503 means nothing reached: the Stop's record, written first, refuses no partner answer."""
    monkeypatch.setattr(chat_module, "_stop_conversation_work", _REAL_STOP_CONVERSATION_WORK)

    async def _request_fails(*_a, **_kw):
        raise ConnectionError("redis went away")

    monkeypatch.setattr(chat_turn_guard, "request_turn_cancel", _request_fails)
    with pytest.raises(HTTPException) as stop:
        await _cancel(web_env)

    assert stop.value.status_code == 503
    assert not await real_redis.exists(chat_turn_guard.stop_record_key(str(web_env.conv.id)))

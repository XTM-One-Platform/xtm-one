"""A platform chat steer sent as the turn ends is answered or refused.

The embedded chat steers a running turn through
``POST /platform/chat/messages/steer``: the route queues the text on a Redis
list the turn's bridge polls every second. A steer queued after the bridge's
last poll is taken when the turn closes the bridge and answered in a fold-in
segment; one sent after that close gets a 409, and its stored row is rolled
back. Redis is a fake that runs the route's and the bridge's scripts as
Redis would, one at a time.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api.v1.platform as platform_api


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.lists.pop(key, None)

    async def lpop(self, key):
        items = self.lists.get(key)
        return items.pop(0) if items else None

    async def eval(self, script, numkeys, *keys_and_args):
        keys, args = keys_and_args[:numkeys], keys_and_args[numkeys:]
        if script == platform_api._STEER_ENQUEUE_LUA:
            if keys[0] not in self.values:
                return 0
            self.lists.setdefault(keys[1], []).append(args[0])
            return 1
        if script == platform_api._STEER_CLOSE_LUA:
            self.values.pop(keys[0], None)
            return self.lists.pop(keys[1], [])
        raise AssertionError(f"unexpected script {script!r}")


class _StreamDB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        if self.added and getattr(self.added[-1], "id", None) is None:
            self.added[-1].id = uuid.uuid4()

    async def commit(self):
        return None


class _SteerDB:
    def __init__(self, conversation):
        self.conversation = conversation
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.conversation)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _events(chunks: list[str]) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in "".join(chunks).split("\n")
        if line.startswith("data: ")
    ]


@pytest.fixture
def wired(monkeypatch):
    redis = _Redis()
    stored: list[tuple[str, str]] = []

    async def _redis_client():
        return redis

    async def _agent(_db, _slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _store(_db, _conversation_id, role, content, channel, metadata_json=None):
        stored.append((role, content))
        return SimpleNamespace(id=uuid.uuid4())

    async def _history(_db, _conversation_id):
        return []

    async def _none(*_args, **_kwargs):
        return None

    async def _collect(_db, _ctx, _initial, *, conversation_id, reply_text=None):
        return [], []

    async def _link(_db, _rows, *, conversation_id, message_id):
        return []

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)
    monkeypatch.setattr("app.core.rate_limit.enforce_rate_limit", _none)
    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _agent)
    monkeypatch.setattr("app.channels.router.store_message", _store)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _none)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _link)
    # The bridge polls once when it starts and never again in these tests.
    monkeypatch.setattr(platform_api, "_STEER_POLL_INTERVAL_S", 3600)
    return SimpleNamespace(redis=redis, stored=stored, monkeypatch=monkeypatch)


async def _steer(user, conversation_id, text):
    db = _SteerDB(SimpleNamespace(id=conversation_id))
    try:
        return await platform_api.steer_platform_chat_message(
            platform_api.PlatformChatSteerRequest(conversation_id=str(conversation_id), content=text),
            db=db,  # type: ignore[arg-type]
            user=user,  # type: ignore[arg-type]
        ), db
    except HTTPException as exc:
        return exc, db


async def _stream(wired, user, run_agent_chat) -> list[dict]:
    wired.monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_agent_chat)
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/platform/chat/messages", "headers": [], "query_string": b""}
    )
    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content="find campaigns", agent_slug="agent", stream=True),
        request=request,
        db=_StreamDB(),  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )
    return _events([chunk async for chunk in response.body_iterator])


async def _after_first_poll(redis: _Redis, conversation_id) -> None:
    while platform_api._steer_active_key(conversation_id) not in redis.values:
        await asyncio.sleep(0)
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_steer_queued_after_the_last_poll_is_answered_in_a_fold_in_segment(wired):
    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    contents: list[str] = []
    steered: list = []

    async def _run(_db, _user_id, _agent, _history, content, conversation_id, **kwargs):
        contents.append(content)
        if len(contents) == 1:
            await _after_first_poll(wired.redis, conversation_id)
            steered.append(await _steer(user, conversation_id, "also check APT29"))
        return (f"answer {len(contents)}", None)

    events = await _stream(wired, user, _run)

    (response, db), = steered
    assert response == {"status": "steered", "conversation_id": response["conversation_id"]}
    assert (db.commits, db.rollbacks) == (1, 0)
    assert contents == ["find campaigns", "also check APT29"]
    assert [e["content"] for e in events if e["type"] == "done"] == ["answer 1", "answer 2"]
    assert ("user", "also check APT29") in wired.stored


@pytest.mark.asyncio
async def test_a_steer_after_the_turn_closed_its_bridge_is_refused_and_not_stored(wired):
    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    conversation: list = []

    async def _run(_db, _user_id, _agent, _history, content, conversation_id, **kwargs):
        conversation.append(conversation_id)
        return ("answer", None)

    events = await _stream(wired, user, _run)
    refused, db = await _steer(user, conversation[0], "too late")

    assert [e["type"] for e in events].count("done") == 1
    assert isinstance(refused, HTTPException) and refused.status_code == 409
    assert (db.commits, db.rollbacks) == (0, 1)
    assert not wired.redis.lists and not wired.redis.values


@pytest.mark.asyncio
async def test_a_steer_during_a_fold_in_segment_still_lands(wired):
    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    contents: list[str] = []
    steered: list = []

    async def _run(_db, _user_id, _agent, _history, content, conversation_id, **kwargs):
        contents.append(content)
        if len(contents) <= 2:
            await _after_first_poll(wired.redis, conversation_id)
            steered.append(await _steer(user, conversation_id, f"steer {len(contents)}"))
        return (f"answer {len(contents)}", None)

    await _stream(wired, user, _run)

    assert [response for response, _db in steered] == [
        {"status": "steered", "conversation_id": steered[0][0]["conversation_id"]}
    ] * 2
    assert contents == ["find campaigns", "steer 1", "steer 2"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_scripts_on_a_real_redis():
    import os

    import redis.asyncio as redis_asyncio

    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")
    conversation_id = uuid.uuid4()
    keys = (platform_api._steer_active_key(conversation_id), platform_api._steer_list_key(conversation_id))

    async def _enqueue(text):
        return await client.eval(platform_api._STEER_ENQUEUE_LUA, 2, *keys, text, "60")

    try:
        assert await _enqueue("no run") == 0
        assert not await client.exists(keys[1])
        await client.set(keys[0], "1", ex=30)
        assert await _enqueue("first") == 1
        assert await _enqueue("second") == 1
        assert 0 < await client.ttl(keys[1]) <= 60
        assert await client.eval(platform_api._STEER_CLOSE_LUA, 2, *keys) == ["first", "second"]
        assert not await client.exists(*keys)
        assert await _enqueue("after the close") == 0
        assert await client.eval(platform_api._STEER_CLOSE_LUA, 2, *keys) == []
    finally:
        await client.delete(*keys)
        await client.aclose()

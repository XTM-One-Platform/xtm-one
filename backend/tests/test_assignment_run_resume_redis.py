"""The assignment run checkpoint's compare-and-set against a real Redis.

``RunCheckpoint._write`` compares the stored ``(epoch, attempt)`` place and
writes in one Lua script: run here as Redis runs it, a stale attempt's write
and hand-back flush after a newer one leave the newer checkpoint in place.

Needs ``REDIS_URL`` to reach a Redis it may write to and skips otherwise.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
import redis.asyncio as redis_asyncio

from app.llm.base import ChatMessage
from app.services import assignment_run_resume, background_checkpoint
from app.tools.base import ToolCall

pytestmark = pytest.mark.integration


def _round(call_id: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="web_fetch", arguments={"url": f"https://example.org/{call_id}"})],
        ),
        ChatMessage(role="tool", content=f"result {call_id}", tool_call_id=call_id, name="web_fetch"),
    ]


@pytest.fixture
async def real_redis(monkeypatch):
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

    monkeypatch.setattr(background_checkpoint, "_redis", _client)
    run_id = str(uuid.uuid4())
    try:
        yield client, run_id
    finally:
        await client.delete(background_checkpoint.checkpoint_key(assignment_run_resume.checkpoint_id(run_id)))
        await client.aclose()


async def test_a_stale_attempt_never_overwrites_a_newer_checkpoint_in_redis(real_redis):
    client, run_id = real_redis
    key = background_checkpoint.checkpoint_key(assignment_run_resume.checkpoint_id(run_id))

    stale = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=2, place=(0, 2))
    stale.offer(_round("a"))
    await stale._task
    assert json.loads(await client.get(key))["place"] == [0, 2]

    newer = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=3, place=(0, 3))
    newer.offer([*_round("a"), *_round("b")])
    await newer._task

    stale.offer([*_round("a"), *_round("x")])
    await stale.aclose(flush=True)

    stored = json.loads(await client.get(key))
    assert stored["place"] == [0, 3] and stored["turn_id"] == "attempt-3"
    assert 0 < await client.ttl(key) <= background_checkpoint.CHECKPOINT_TTL_SECONDS

    later_epoch = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=1, place=(1, 1))
    later_epoch.offer(_round("c"))
    await later_epoch._task
    assert json.loads(await client.get(key))["place"] == [1, 1], "a later epoch writes over an earlier one"


async def test_the_tombstone_of_an_ending_fences_a_stale_writer_in_redis(real_redis):
    client, run_id = real_redis
    key = background_checkpoint.checkpoint_key(assignment_run_resume.checkpoint_id(run_id))
    replaced = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=2, place=(0, 2))
    replaced.offer(_round("a"))
    await replaced._task

    await assignment_run_resume.discard(run_id, place=(0, 3))
    replaced.offer([*_round("a"), *_round("x")])
    await replaced.aclose(flush=True)
    same_place = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=3, place=(0, 3))
    same_place.offer(_round("y"))
    await same_place._task

    stored = json.loads(await client.get(key))
    assert stored["ended"] is True and stored["place"] == [0, 3] and "entries" not in stored


async def test_a_checkpoint_with_no_readable_place_is_overwritten(real_redis):
    client, run_id = real_redis
    key = background_checkpoint.checkpoint_key(assignment_run_resume.checkpoint_id(run_id))
    await client.set(key, "not json", ex=60)

    checkpoint = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=1, place=(0, 1))
    checkpoint.offer(_round("a"))
    await checkpoint._task

    assert json.loads(await client.get(key))["place"] == [0, 1]

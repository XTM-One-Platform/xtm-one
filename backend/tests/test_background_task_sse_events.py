"""Tests for real-time background-task SSE lifecycle events.

The chat BackgroundTasksBanner (and the Background Tasks screen / Flows live
view) rely on the SSE activity stream to invalidate their caches. Without an
event at task spawn and completion the banner only refreshed on its 30s idle
poll — an agent could spawn and finish background tasks with zero live
indication in the UI.

``publish_task_event`` is the single publisher: it must emit a
``background_task_update`` payload to the owner's personal activity channel
and must NEVER raise (a Redis hiccup can't be allowed to fail the task).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.sub_agent import publish_task_event


@pytest.mark.asyncio
async def test_publish_task_event_publishes_to_personal_channel():
    user_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())

    redis = AsyncMock()
    with patch("app.core.redis.redis_client", new=AsyncMock(return_value=redis)):
        await publish_task_event(
            user_id,
            "spawned",
            task_id,
            conversation_id=conversation_id,
            status="pending",
        )

    redis.publish.assert_awaited_once()
    channel, raw_payload = redis.publish.await_args.args
    assert channel == f"activity:{user_id}"
    payload = json.loads(raw_payload)
    assert payload == {
        "type": "background_task_update",
        "event": "spawned",
        "task_id": task_id,
        "conversation_id": conversation_id,
        "status": "pending",
    }


@pytest.mark.asyncio
async def test_publish_task_event_optional_fields_default_to_none():
    redis = AsyncMock()
    with patch("app.core.redis.redis_client", new=AsyncMock(return_value=redis)):
        await publish_task_event(str(uuid.uuid4()), "completed", "task-1")

    payload = json.loads(redis.publish.await_args.args[1])
    assert payload["event"] == "completed"
    assert payload["conversation_id"] is None
    assert payload["status"] is None


@pytest.mark.asyncio
async def test_publish_task_event_swallows_redis_errors():
    """A Redis outage must never propagate into the task lifecycle."""
    redis = AsyncMock()
    redis.publish.side_effect = ConnectionError("redis down")
    with patch("app.core.redis.redis_client", new=AsyncMock(return_value=redis)):
        await publish_task_event(str(uuid.uuid4()), "failed", "task-1")


@pytest.mark.asyncio
async def test_publish_task_event_skips_empty_user_id():
    """No owner channel to publish to — must be a silent no-op."""
    redis = AsyncMock()
    with patch("app.core.redis.redis_client", new=AsyncMock(return_value=redis)):
        await publish_task_event("", "spawned", "task-1")

    redis.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_task_event_is_time_bounded():
    """A hung Redis publish must not stall the caller.

    The spawn-side publish sits on the ``spawn_background_task`` tool hot
    path — a slow/unreachable Redis is bounded by ``_PUBLISH_EVENT_TIMEOUT``
    and swallowed like any other publish failure.
    """

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(30)

    redis = AsyncMock()
    redis.publish = _hang
    with (
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=redis)),
        patch("app.services.sub_agent._PUBLISH_EVENT_TIMEOUT", 0.05),
    ):
        started = time.monotonic()
        await publish_task_event(str(uuid.uuid4()), "spawned", "task-1")
        elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"publish was not time-bounded (took {elapsed:.2f}s)"

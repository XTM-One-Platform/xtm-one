"""A non-streaming platform chat turn cut by a platform stop answers retryably.

A ``stream: false`` call (a calling platform's playbook step) waits for one
JSON answer. When a rolling deploy or a worker recycle cut its turn, uvicorn's
graceful drain cancelled the route, nothing caught the cancellation, and the
caller saw a dropped connection or nothing until its own timeout. The route
now answers 503 ``platform_restarting``, ``retryable: true``, with a
``Retry-After``, and logs one WARNING naming the conversation and the cause.
A cancellation that is not a platform stop (the client left) propagates as
before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import platform as platform_api
from app.core import worker_shutdown
from app.core.utils import GRACEFUL_SHUTDOWN_CANCEL_HINT
from app.core.worker_shutdown import WIND_DOWN_CANCEL_MESSAGE, TurnWoundDown

_DRAIN_MESSAGE = f"Task cancelled, {GRACEFUL_SHUTDOWN_CANCEL_HINT}"


@pytest.fixture(autouse=True)
def _no_recorded_stop(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "_request", None)


def _patch_route(monkeypatch, run_agent_chat) -> list:
    stored: list = []

    async def _resolve(_db, _slug):
        return SimpleNamespace(id=uuid.uuid4(), name="Alpha")

    async def _store_message(_db, _conv_id, role, content, channel, metadata_json=None):
        stored.append((role, content))
        return SimpleNamespace(id=uuid.uuid4(), metadata_json=metadata_json, role=role)

    async def _history(_db, _conv_id):
        return []

    async def _quota(_db, _user_id):
        return None

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _resolve)
    monkeypatch.setattr("app.channels.router.store_message", _store_message)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_agent_chat)
    return stored


async def _send():
    conversations: list = []

    class _DB:
        def add(self, obj):
            conversations.append(obj)

        async def flush(self):
            for obj in conversations:
                if getattr(obj, "id", None) is None:
                    obj.id = uuid.uuid4()

        async def commit(self):
            return None

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/platform/chat/messages", "headers": [], "query_string": b""}
    )
    return await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content="transform this bundle", agent_slug="alpha", stream=False),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="u@example.com"),  # type: ignore[arg-type]
    )


def _assert_retryable_cut(response, stored) -> dict:
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(platform_api.PLATFORM_CHAT_RESTART_RETRY_AFTER_SECONDS)
    body = json.loads(response.body)
    assert body["error_type"] == "platform_restarting"
    assert body["retryable"] is True
    assert "content" not in body
    # No answer stored: the caller sends the same request again.
    assert [role for role, _ in stored] == ["user"]
    return body


@pytest.mark.asyncio
async def test_a_graceful_drain_cut_answers_a_retryable_503(monkeypatch, caplog):
    async def _run_agent_chat(*_args, **_kwargs):
        raise asyncio.CancelledError(_DRAIN_MESSAGE)

    stored = _patch_route(monkeypatch, _run_agent_chat)
    monkeypatch.setattr(worker_shutdown, "_request", None)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    with caplog.at_level(logging.WARNING, logger="app.api.v1.platform"):
        response = await _send()

    body = _assert_retryable_cut(response, stored)
    assert "deployment" in body["detail"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(body["conversation_id"] in m and "cause stop" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_a_turn_wound_down_at_an_iteration_boundary_answers_a_retryable_503(monkeypatch, caplog):
    async def _run_agent_chat(*_args, **_kwargs):
        raise TurnWoundDown(WIND_DOWN_CANCEL_MESSAGE)

    stored = _patch_route(monkeypatch, _run_agent_chat)
    worker_shutdown.note_shutdown(worker_shutdown.RECYCLE, "1000 requests")

    with caplog.at_level(logging.WARNING, logger="app.api.v1.platform"):
        response = await _send()

    body = _assert_retryable_cut(response, stored)
    assert "recycled" in body["detail"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(body["conversation_id"] in m and "cause recycle" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_a_real_drain_cancellation_of_the_request_task_is_answered(monkeypatch):
    started = asyncio.Event()

    async def _run_agent_chat(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(60)
        return "never", None

    stored = _patch_route(monkeypatch, _run_agent_chat)
    task = asyncio.ensure_future(_send())
    await started.wait()
    task.cancel(_DRAIN_MESSAGE)
    response = await task

    _assert_retryable_cut(response, stored)
    # The delivered cancellation was absorbed: nothing stays armed on the task.
    assert task.cancelling() == 0


@pytest.mark.asyncio
async def test_a_client_disconnect_still_propagates(monkeypatch):
    started = asyncio.Event()

    async def _run_agent_chat(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(60)
        return "never", None

    stored = _patch_route(monkeypatch, _run_agent_chat)
    task = asyncio.ensure_future(_send())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert [role for role, _ in stored] == ["user"]


@pytest.mark.asyncio
async def test_a_turn_that_answered_is_unchanged(monkeypatch):
    async def _run_agent_chat(*_args, **_kwargs):
        return "the answer", None

    async def _no_attachments(*_args, **_kwargs):
        return [], []

    stored = _patch_route(monkeypatch, _run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _no_attachments)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    response = await _send()

    assert isinstance(response, dict)
    assert response["content"] == "the answer"
    assert ("assistant", "the answer") in stored


def test_the_classifier_maps_a_drain_cancellation_to_platform_restarting():
    failure = platform_api.classify_platform_turn_failure(asyncio.CancelledError(_DRAIN_MESSAGE))

    assert (failure.status_code, failure.error_type, failure.retryable) == (503, "platform_restarting", True)
    assert failure.retry_after == platform_api.PLATFORM_CHAT_RESTART_RETRY_AFTER_SECONDS
    assert failure.frame()["error_type"] == "platform_restarting"


def test_the_classifier_leaves_a_bare_cancellation_alone():
    failure = platform_api.classify_platform_turn_failure(asyncio.CancelledError())

    assert failure.error_type != "platform_restarting"

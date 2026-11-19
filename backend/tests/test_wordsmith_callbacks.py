"""Wordsmith question callbacks routed to the integration receiver (#160, slice 3 remainder)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.integrations.providers import wordsmith
from app.integrations.providers.wordsmith import build_tools
from app.services import wordsmith_webhooks as ws

INTEGRATION = "11111111-2222-3333-4444-555555555555"
SECRET = "whsec_callback_test_secret"
OAUTH_TOKEN = "ws-oauth-access-token"


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttl[key] = ex or 0

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


@pytest.fixture
def redis() -> Iterator[FakeRedis]:
    fake = FakeRedis()
    with patch("app.core.redis.redis_client", AsyncMock(return_value=fake)):
        yield fake


def _signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    t = int(time.time())
    v1 = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"wordsmith-signature": f"t={t},v1={v1}"}


# -- Events ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "event_type"),
    [("completed", "operation.succeeded"), ("filtered", "operation.succeeded"), ("error", "operation.failed")],
)
def test_a_final_callback_stands_for_its_operation_event(status, event_type):
    record = {"question_id": "q1", "operation_id": "op1", "agent_id": "default"}
    event = ws.callback_event(record, {"id": "q1", "status": status, "session_id": "s1"})
    assert event is not None
    assert event["type"] == event_type
    assert event["resource"] == {"type": "operation", "id": "op1"}
    assert event["data"]["question_id"] == "q1" and event["data"]["status"] == status
    assert event["follow_up_tool"] == "get_wordsmith_agents operation=get_question"


def test_a_callback_that_is_not_final_runs_nothing():
    assert ws.callback_event({"question_id": "q1"}, {"id": "q1", "status": "in_progress"}) is None


def test_a_question_without_operation_is_keyed_on_the_question():
    event = ws.callback_event({"question_id": "q1"}, {"id": "q1", "status": "completed"})
    assert event is not None and event["resource"] == {"type": "question", "id": "q1"}


def test_the_callback_and_the_endpoint_event_of_one_operation_share_one_seen_member():
    callback = ws.event_payload(ws.callback_event({"operation_id": "op1"}, {"id": "q1", "status": "completed"}))
    endpoint = ws.event_payload(
        {"id": "evt_9", "type": "operation.succeeded", "resource": {"type": "operation", "id": "op1"}}
    )
    assert callback is not None and endpoint is not None
    assert callback["message_id"] == endpoint["message_id"] == "operation.succeeded:op1"
    assert callback["follow_up_tool"] == "get_wordsmith_agents operation=get_question"


def test_other_events_keep_their_event_id_as_seen_member():
    payload = ws.event_payload({"id": "evt_1", "type": "task.created", "resource": {"type": "task", "id": "t1"}})
    assert payload is not None and payload["message_id"] == "evt_1"


def test_the_callback_url_names_the_routing_token_on_a_public_base_only():
    url = ws.callback_receiver_url("https://xtm.example.com", INTEGRATION, "tok")
    assert url is not None
    assert url.startswith("https://xtm.example.com/") and INTEGRATION in url
    assert url.endswith(f"?{ws.CALLBACK_PARAM}=tok")
    assert ws.callback_receiver_url("http://localhost:8000", INTEGRATION, "tok") is None


# -- Routing record ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_routing_record_keeps_the_secret_encrypted_past_the_retry_window(redis):
    await ws.remember_callback(INTEGRATION, "tok", secret=SECRET, question_id="q1", operation_id="op1", agent_id="a")
    key = ws.callback_key(INTEGRATION, "tok")
    assert SECRET not in redis.store[key]
    assert redis.ttl[key] > 24 * 3600
    record = await ws._callback_record(INTEGRATION, "tok")
    assert ws.callback_secret(record) == SECRET
    assert record["operation_id"] == "op1"
    assert await ws._callback_record(INTEGRATION, "other") == {}


# -- Receiver ----------------------------------------------------------------


@asynccontextmanager
async def _session() -> AsyncIterator[MagicMock]:
    yield MagicMock()


@pytest.fixture
def receiver(redis) -> Iterator[AsyncMock]:
    assignment = SimpleNamespace(id="wf1", name="On answer", trigger_config={"event": "operation.succeeded"})
    dispatch = AsyncMock(return_value=[{"run_id": "r1"}])
    with (
        patch("app.core.database.async_session", _session),
        patch(
            "app.services.docusign_connect.provider_assignments",
            AsyncMock(return_value=[(assignment, SimpleNamespace(name="Agent"))]),
        ),
        patch.object(ws, "_dispatch_event", dispatch),
    ):
        yield dispatch


@pytest.mark.asyncio
async def test_a_signed_callback_runs_the_operation_assignments(receiver):
    await ws.remember_callback(INTEGRATION, "tok", secret=SECRET, question_id="q1", operation_id="op1", agent_id="a")
    payload = {"id": "q1", "status": "completed", "session_id": "s1", "answer": "..."}
    body = json.dumps(payload).encode()
    result = await ws.dispatch_delivery(INTEGRATION, _signed(body), body, payload, callback_token="tok")
    assert result["count"] == 1
    event = receiver.await_args.args[3]
    assert event["type"] == "operation.succeeded" and event["resource"]["id"] == "op1"
    assert receiver.await_args.kwargs["delivery"] == "wordsmith_callback"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["tok", "unknown"])
async def test_a_callback_not_signed_by_its_question_secret_is_refused(receiver, token):
    await ws.remember_callback(INTEGRATION, "tok", secret=SECRET, question_id="q1", operation_id="", agent_id="a")
    payload = {"id": "q1", "status": "completed"}
    body = json.dumps(payload).encode()
    with pytest.raises(HTTPException) as caught:
        await ws.dispatch_delivery(INTEGRATION, _signed(body, "whsec_other"), body, payload, callback_token=token)
    assert caught.value.status_code == 401
    receiver.assert_not_awaited()


# -- Tool --------------------------------------------------------------------


def _resp(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("POST", "https://x"))


@asynccontextmanager
async def _client(*_a: Any, **_k: Any) -> AsyncIterator[MagicMock]:
    yield MagicMock()


@pytest.fixture
def http() -> Iterator[AsyncMock]:
    mock = AsyncMock()
    with (
        patch.object(wordsmith, "safe_request", mock),
        patch.object(wordsmith, "egress_client", _client),
        patch("app.core.config.settings.base_url", "https://xtm.example.com"),
    ):
        yield mock


def _ask(token: str, integration_id: str = INTEGRATION):
    executor = MagicMock()
    build_tools(token, executor, integration_id=integration_id)
    handlers = {call.args[0]: call.args[1] for call in executor.register.call_args_list}
    return handlers["send_wordsmith_agent_question"]


@pytest.mark.asyncio
async def test_a_routed_question_sends_the_receiver_url_and_keeps_its_secret_unshown(http, redis):
    http.return_value = (
        _resp({"id": "q1", "status": "in_progress", "operation_id": "op1", "callback_secret": SECRET}),
        None,
    )
    answer = await _ask(OAUTH_TOKEN)(question="Review this NDA", callback_url="xtm-one", wait=0)
    sent = http.await_args.kwargs["json"]["callback_url"]
    assert sent.startswith("https://xtm.example.com/") and f"?{ws.CALLBACK_PARAM}=" in sent
    assert SECRET not in answer
    assert "operation.succeeded" in answer
    token = sent.rsplit("=", 1)[1]
    assert ws.callback_secret(await ws._callback_record(INTEGRATION, token)) == SECRET


@pytest.mark.asyncio
async def test_an_api_key_cannot_route_a_question_and_nothing_is_asked(http, redis):
    answer = await _ask("sk-ws-api1-test")(question="Review this NDA", callback_url="xtm-one")
    assert answer.startswith("Error:") and "OAuth" in answer
    http.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_external_callback_url_is_sent_as_given(http, redis):
    http.return_value = (_resp({"id": "q1", "status": "in_progress"}), None)
    await _ask(OAUTH_TOKEN)(question="Q", callback_url="https://hooks.example.com/ws", wait=0)
    assert http.await_args.kwargs["json"]["callback_url"] == "https://hooks.example.com/ws"
    assert redis.store == {}

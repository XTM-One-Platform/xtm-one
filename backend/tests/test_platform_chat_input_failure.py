"""An oversized input or an empty turn is an error status on the platform chat API.

``run_agent_chat`` ends a turn whose input does not fit the model (a context
overflow, a tool overflow, a provider 413) with a chat text a person reads and
a ``status: error`` event, and returns that text instead of raising. The
non-streaming platform path (``stream: false``, what an OpenCTI or OpenAEV
playbook AI step calls) answered it HTTP 200 with the apology as ``content``:
the caller took it for the agent's answer and skipped the step silently
(xtm-one #2626). It now answers 413 with ``error_type``, ``retryable: false``
and the terse API text, and an empty turn answers 500 ``internal_error``,
retryable; neither stores an assistant message. The streaming path carries the
same ``error_type`` and ``retryable`` in its ``status: error`` frame.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import platform as platform_api
from app.llm.base import ContextOverflowError, overflow_api_message, overflow_friendly_message
from app.llm.router import REQUEST_TOO_LARGE_STREAMING_MESSAGE


def _overflow(*, tool_dominated: bool = False) -> ContextOverflowError:
    return ContextOverflowError(
        estimated_tokens=1_024_073,
        context_window=1_000_000,
        model="m",
        tool_tokens=900_000 if tool_dominated else 1_000,
        message_tokens=1_000 if tool_dominated else 1_023_073,
    )


def _ends_on(error_type: str, text: str):
    """A ``run_agent_chat`` that ends the turn the way its overflow handlers do."""

    async def _run(*_args, **kwargs):
        if kwargs.get("on_token"):
            await kwargs["on_token"](text)
        await kwargs["on_event"]({"status": "error", "error_type": error_type})
        return text, None

    return _run


def _wire(monkeypatch, run_agent_chat) -> list:
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

    async def _collect(_db, _ctx, _initial, *, conversation_id, reply_text=None):
        return [], []

    async def _link(_db, _rows, *, conversation_id, message_id):
        return []

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _resolve)
    monkeypatch.setattr("app.channels.router.store_message", _store_message)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _link)
    return stored


class _DB:
    def __init__(self):
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        return None


async def _send(monkeypatch, run_agent_chat, *, stream: bool):
    stored = _wire(monkeypatch, run_agent_chat)
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/platform/chat/messages", "headers": [], "query_string": b""}
    )
    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content="transform this bundle", agent_slug="alpha", stream=stream),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="u@example.com"),  # type: ignore[arg-type]
    )
    return response, stored


# -- Non-streaming: an error status, no assistant message ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "text", "detail"),
    [
        ("context_overflow", overflow_friendly_message(_overflow()), overflow_api_message(_overflow())),
        (
            "tool_overflow",
            overflow_friendly_message(_overflow(tool_dominated=True)),
            overflow_api_message(_overflow(tool_dominated=True)),
        ),
    ],
)
async def test_an_overflow_answers_413_not_a_200_apology(monkeypatch, error_type, text, detail):
    response, stored = await _send(monkeypatch, _ends_on(error_type, text), stream=False)

    assert response.status_code == 413
    body = json.loads(response.body)
    assert body["error_type"] == error_type
    assert body["retryable"] is False
    assert body["detail"] == detail
    assert "content" not in body
    assert [role for role, _ in stored] == ["user"]


@pytest.mark.asyncio
async def test_a_provider_413_answers_413_request_too_large(monkeypatch):
    response, stored = await _send(
        monkeypatch, _ends_on("request_too_large", REQUEST_TOO_LARGE_STREAMING_MESSAGE), stream=False
    )

    assert response.status_code == 413
    body = json.loads(response.body)
    assert (body["error_type"], body["retryable"], body["last_status"]) == ("request_too_large", False, 413)
    assert "content" not in body
    assert [role for role, _ in stored] == ["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", "   ", None])
async def test_an_empty_turn_answers_a_retryable_500(monkeypatch, answer):
    async def _run(*_args, **_kwargs):
        return answer, None

    response, stored = await _send(monkeypatch, _run, stream=False)

    assert response.status_code == 500
    body = json.loads(response.body)
    assert (body["error_type"], body["retryable"]) == ("internal_error", True)
    assert "content" not in body
    assert [role for role, _ in stored] == ["user"]


@pytest.mark.asyncio
async def test_an_error_event_of_a_step_does_not_fail_a_turn_that_answered(monkeypatch):
    """A nested step (a consulted agent, a tool) may overflow; the turn still answered."""

    async def _run(*_args, **kwargs):
        await kwargs["on_event"]({"status": "error", "error_type": "context_overflow"})
        return "Here is the bundle.", None

    response, stored = await _send(monkeypatch, _run, stream=False)

    assert response["content"] == "Here is the bundle."
    assert [role for role, _ in stored] == ["user", "assistant"]


def test_only_input_kinds_are_input_failures():
    assert platform_api.platform_turn_input_failure([], "text") is None
    assert platform_api.platform_turn_input_failure(["rate_limited"], "text") is None
    text = overflow_friendly_message(_overflow())
    failure = platform_api.platform_turn_input_failure(["timeout", "context_overflow"], text)
    assert failure is not None
    assert (failure.status_code, failure.error_type, failure.retryable) == (413, "context_overflow", False)


# -- Streaming: the error frame carries the kind -------------------------------


def _events(chunks: list[str]) -> list[dict]:
    body = "".join(chunks)
    return [json.loads(line[len("data: ") :]) for line in body.split("\n") if line.startswith("data: ")]


@pytest.mark.asyncio
async def test_the_stream_error_frame_names_the_kind_and_is_not_retryable(monkeypatch):
    text = overflow_friendly_message(_overflow())
    response, _ = await _send(monkeypatch, _ends_on("context_overflow", text), stream=True)

    events = _events([chunk async for chunk in response.body_iterator])

    statuses = [e for e in events if e["type"] == "status"]
    assert statuses[-1]["status"] == "error"
    assert statuses[-1]["error_type"] == "context_overflow"
    assert statuses[-1]["retryable"] is False
    # Humans read the embedded chat: the friendly text still reaches it.
    assert "".join(e["content"] for e in events if e["type"] == "stream") == text

"""The platform chat hands the model its current message once (#12270).

The route stores the user message before it loads the history, so the history
read back ends with that message while ``run_agent_chat`` appends the turn's
text itself. Sent twice, a large input document was detached from the newest
copy only and the history copy reached the model whole on every iteration.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from app.api.v1 import platform as platform_api
from app.channels.router import without_current_turn
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import CompositeToolExecutor, FileContext
from app.tools.session_input import INPUT_MOUNT_NAME, detach_input_document, input_reference_for_run

_INSTRUCTIONS = "Add the label phishing.\n\n--- STIX BUNDLE ---\n"


def _bundle(chars: int) -> dict[str, Any]:
    filler = "x" * 1000
    notes = [
        {"type": "note", "id": f"note--{i:08d}-0000-4000-8000-000000000000", "content": filler}
        for i in range(max(1, chars // 1100))
    ]
    return {"type": "bundle", "id": "bundle--1", "objects": [{"type": "report", "id": "report--1"}, *notes]}


class _RecordingLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 1_000_000

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):  # type: ignore[no-untyped-def,override]
        self.requests.append(list(messages))
        return ChatResponse(content="done", model="stub")

    async def chat_stream(self, messages, **kwargs):  # type: ignore[no-untyped-def,override]
        yield ""

    async def embed(self, text, model=None):  # type: ignore[no-untyped-def]
        return [0.0]

    async def list_models(self) -> list[Any]:
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


class _DB:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj):  # type: ignore[no-untyped-def]
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        return None


async def _send(monkeypatch: pytest.MonkeyPatch, content: str, *, config: dict, stream: bool) -> dict[str, Any]:
    """Post *content* to the platform chat; the agent run is the real loop on a recording model.

    The stub ``run_agent_chat`` builds the list the real one hands the loop
    (``[*history, current user message]``) and runs it, so what the model
    receives is asserted, not what the route passes along.
    """
    stored: list[ChatMessage] = [
        ChatMessage(role="user", content="earlier question"),
        ChatMessage(role="assistant", content="earlier answer"),
    ]
    seen: dict[str, Any] = {"llm": _RecordingLLM(), "ctx": FileContext()}

    async def _resolve(_db, _slug):  # type: ignore[no-untyped-def]
        return SimpleNamespace(id=uuid.uuid4(), name="Transformer", config=config)

    async def _store(_db, _conv_id, role, content, channel, metadata_json=None):  # type: ignore[no-untyped-def]
        stored.append(ChatMessage(role=role, content=content))
        return SimpleNamespace(id=uuid.uuid4(), metadata_json=metadata_json, role=role)

    async def _history(_db, _conv_id):  # type: ignore[no-untyped-def]
        # The database read: every stored message, the current one included.
        return list(stored)

    async def _quota(_db, _user_id):  # type: ignore[no-untyped-def]
        return None

    async def _run_agent_chat(_db, _uid, agent, history, user_message, conversation_id, **kwargs):  # type: ignore[no-untyped-def]
        await al.run_agent_loop(
            llm=seen["llm"],
            messages=[*history, ChatMessage(role="user", content=user_message)],
            tools=[],
            executor=CompositeToolExecutor(),
            max_iterations=2,
            progressive_tools=False,
            input_reference=input_reference_for_run(agent.config, user_message),
            file_context=seen["ctx"],
        )
        return ("done", None)

    async def _collect(_db, _ctx, _initial, **_kwargs):  # type: ignore[no-untyped-def]
        return ([], [])

    async def _link(_db, attachment_rows, *, conversation_id, message_id):  # type: ignore[no-untyped-def]
        return attachment_rows

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _resolve)
    monkeypatch.setattr("app.channels.router.store_message", _store)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _link)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [(b"x-platform-product", b"opencti")],
            "query_string": b"",
        }
    )
    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content=content, agent_slug="transformer", stream=stream),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="u@example.com"),  # type: ignore[arg-type]
    )
    if stream:
        _ = [chunk async for chunk in response.body_iterator]
    return seen


def _user_texts(request: list[Any]) -> list[str]:
    return [m.content for m in request if getattr(m, "role", None) == "user" and isinstance(m.content, str)]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_large_bundle_reaches_the_model_detached_and_mounted_once(
    monkeypatch: pytest.MonkeyPatch, _quiet_langfuse: None, stream: bool
) -> None:
    bundle = _bundle(300_000)
    seen = await _send(monkeypatch, _INSTRUCTIONS + json.dumps(bundle), config={}, stream=stream)
    first = seen["llm"].requests[0]
    users = _user_texts(first)
    assert any("[Input document detached" in text for text in users)
    assert max(len(text) for text in users) < 100_000
    assert sum(1 for text in users if text.startswith(_INSTRUCTIONS)) == 1
    assert list(seen["ctx"].sandbox_inputs) == [INPUT_MOUNT_NAME]
    assert json.loads(seen["ctx"].sandbox_inputs[INPUT_MOUNT_NAME]) == bundle


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_an_opted_out_agent_still_gets_an_oversize_bundle_detached(
    monkeypatch: pytest.MonkeyPatch, _quiet_langfuse: None, stream: bool
) -> None:
    bundle = _bundle(1_600_000)
    seen = await _send(
        monkeypatch, _INSTRUCTIONS + json.dumps(bundle), config={"input_reference": False}, stream=stream
    )
    users = _user_texts(seen["llm"].requests[0])
    assert max(len(text) for text in users) < 100_000
    detached = [text for text in users if "[Input document detached" in text]
    assert len(detached) == 1
    assert f"attachments/{INPUT_MOUNT_NAME}" in detached[0]
    assert "Reference it as $input" not in detached[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_short_turn_is_sent_once(monkeypatch: pytest.MonkeyPatch, _quiet_langfuse: None, stream: bool) -> None:
    seen = await _send(monkeypatch, "which campaigns target banks?", config={}, stream=stream)
    users = _user_texts(seen["llm"].requests[0])
    assert users.count("which campaigns target banks?") == 1
    # The earlier turns are still there.
    assert "earlier question" in users


# ── without_current_turn ──────────────────────────────────────────────────────


def test_the_newest_stored_copy_of_the_turn_is_dropped() -> None:
    history = [ChatMessage(role="assistant", content="a"), ChatMessage(role="user", content="hello")]
    assert [m.content for m in without_current_turn(history, "hello")] == ["a"]


def test_the_text_is_compared_as_stored() -> None:
    history = [ChatMessage(role="user", content="hello")]
    assert without_current_turn(history, "hel\x00lo") == []


@pytest.mark.parametrize(
    "history",
    [
        [],
        [ChatMessage(role="user", content="hello"), ChatMessage(role="assistant", content="hi")],
        [ChatMessage(role="user", content="something else")],
        [ChatMessage(role="assistant", content="hello")],
    ],
)
def test_any_other_history_is_kept(history: list[ChatMessage]) -> None:
    assert without_current_turn(history, "hello") is history


# ── detach_input_document: the defence ────────────────────────────────────────


def test_every_copy_of_the_newest_message_is_detached() -> None:
    content = _INSTRUCTIONS + json.dumps(_bundle(300_000))
    original = ChatMessage(role="user", content=content)
    messages = [
        ChatMessage(role="system", content="s"),
        original,
        ChatMessage(role="system", content="reminder"),
        ChatMessage(role="user", content=content),
    ]
    _document, detached = detach_input_document(messages)
    assert detached is True
    assert messages[1].content == messages[3].content
    assert "[Input document detached" in messages[1].content
    assert len(messages[1].content) < 2_000
    # The caller's history object is never mutated.
    assert original.content == content

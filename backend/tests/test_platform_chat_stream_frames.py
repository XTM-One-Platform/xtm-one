"""What the platform chat SSE stream sends.

The embedded chat of OpenCTI / OpenAEV reads this stream through each
product's backend proxy. Answer text and reasoning are joined over a short
window instead of one event per provider delta, and forwarded status events
leave out the prompt copies and full tool payloads no consumer reads; the
reasoning-details trace is built from the full events before they are
forwarded, so it keeps the tool input and output.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import app.api.v1.platform as platform_api


def _events(chunks: list[str]) -> list[dict]:
    body = "".join(chunks)
    return [
        json.loads(line[len("data: ") :])
        for line in body.split("\n")
        if line.startswith("data: ")
    ]


class _DB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        if self.added and getattr(self.added[-1], "id", None) is None:
            self.added[-1].id = uuid.uuid4()

    async def commit(self):
        return None


def _wire(monkeypatch, run_agent_chat) -> None:
    async def _agent(_db, _slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _store(_db, _conversation_id, role, content, channel, metadata_json=None):
        return SimpleNamespace(id=uuid.uuid4())

    async def _history(_db, _conversation_id):
        return []

    async def _quota(_db, _user_id):
        return None

    async def _collect(_db, _ctx, _initial, *, conversation_id, reply_text=None):
        return [], []

    async def _link(_db, _rows, *, conversation_id, message_id):
        return []

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _agent)
    monkeypatch.setattr("app.channels.router.store_message", _store)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _link)


async def _stream(monkeypatch, run_agent_chat) -> list[dict]:
    _wire(monkeypatch, run_agent_chat)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )
    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content="find campaigns", agent_slug="agent", stream=True),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="user@example.com"),  # type: ignore[arg-type]
    )
    return _events([chunk async for chunk in response.body_iterator])


@pytest.mark.asyncio
async def test_text_is_joined_in_order_and_heavy_payloads_stay_on_the_server(monkeypatch):
    # The join is asserted, not the 50 ms window: a loaded xdist worker can spend
    # more than 50 ms between two awaits and close the window, so the window is
    # pinned wide here (the window itself is covered by the test below).
    monkeypatch.setattr(platform_api, "_SSE_TEXT_COALESCE_SECONDS", 60.0)

    async def _run(_db, _user_id, _agent, _history, _content, conversation_id, **kwargs):
        on_token, on_event = kwargs["on_token"], kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        for piece in ("Let me ", "look ", "this up."):
            await on_event({"status": "thinking_text", "content": piece})
        await on_event(
            {
                "status": "llm_step",
                "iteration": 1,
                "llm_model": "m",
                "llm_system_prompt": "S" * 30_000,
                "llm_user_prompt": "U" * 80_000,
                "llm_raw_response": "R" * 80_000,
            }
        )
        await on_event({"status": "tool_start", "tools": ["search"]})
        await on_event(
            {
                "status": "tool_done",
                "tool": "search",
                "tool_input": '{"q": "apt28"}',
                "tool_input_full": '{"q": "apt28"}',
                "tool_output": "3 campaigns",
                "tool_output_full": "X" * 500_000,
                "success": True,
            }
        )
        for piece in ("Found ", "three ", "campaigns."):
            await on_token(piece)
        return ("Found three campaigns.", None)

    events = await _stream(monkeypatch, _run)

    kinds = [(e["type"], e.get("status")) for e in events]
    assert kinds == [
        ("status", "thinking"),
        ("status", "thinking_text"),
        ("status", "llm_step"),
        ("status", "tool_start"),
        ("status", "tool_done"),
        ("stream", None),
        ("done", None),
    ]
    assert events[1]["content"] == "Let me look this up."
    assert events[5]["content"] == "Found three campaigns."
    assert not {"llm_system_prompt", "llm_user_prompt", "llm_raw_response"} & set(events[2])
    assert events[2]["llm_model"] == "m"
    assert not {"tool_output", "tool_output_full", "tool_input_full"} & set(events[4])
    assert events[4]["tool"] == "search"
    trace = events[6]["tool_call_trace"]
    assert trace == [
        {"name": "search", "input": '{"q": "apt28"}', "output": "3 campaigns", "success": True}
    ]
    assert events[6]["content"] == "Found three campaigns."


@pytest.mark.asyncio
async def test_text_arriving_after_the_window_is_sent_as_it_comes(monkeypatch):
    monkeypatch.setattr(platform_api, "_SSE_TEXT_COALESCE_SECONDS", 0.01)

    async def _run(_db, _user_id, _agent, _history, _content, conversation_id, **kwargs):
        await kwargs["on_token"]("First part. ")
        await asyncio.sleep(0.1)
        await kwargs["on_token"]("Second part.")
        return ("First part. Second part.", None)

    events = await _stream(monkeypatch, _run)

    assert [e["content"] for e in events if e["type"] == "stream"] == [
        "First part. ",
        "Second part.",
    ]


def test_only_events_differing_in_content_are_joined():
    text = platform_api._SSETextCoalescer()

    assert text.offer({"type": "stream", "content": "a"}, 0.0) == []
    assert text.offer({"type": "stream", "content": "b"}, 0.01) == []
    thinking = {"type": "status", "status": "thinking_text", "content": "c"}
    assert text.offer(thinking, 0.02) == [{"type": "stream", "content": "ab"}]
    tagged = {"type": "status", "status": "thinking_text", "content": "d", "agent": "x"}
    assert text.offer(tagged, 0.03) == [thinking]
    status = {"type": "status", "status": "tool_start", "tools": ["t"]}
    assert text.offer(status, 0.04) == [tagged, status]
    assert not text.holding
    assert text.flush() is None

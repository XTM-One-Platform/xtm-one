"""Adding and removing reactions on Teams chat messages (1:1 and group)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.teams_scope import DROPPED_WHEN_SCOPED, ScopedChannel
from app.services.integration_read_only import withhold_write_tools
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

GROUP_CHAT = "19:0a1b2c3d4e5f60718293a4b5c6d7e8f9@thread.v2"
ONE_ON_ONE = "19:11111111-2222-3333-4444-555555555555_66666666-7777-8888-9999-000000000000@unq.gbl.spaces"
GRAPH = "https://graph.microsoft.com/v1.0"
TOOLS = ["add_microsoft_chat_message_reaction", "remove_microsoft_chat_message_reaction"]


def _graph(monkeypatch, error: str | None = None):
    """Record every Graph request; answer each with an empty 204, or with *error*."""
    sent: list[tuple[str, str, Any]] = []

    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        sent.append((method, url, kwargs.get("json")))
        return (None, error) if error else (httpx.Response(204), None)

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return sent


async def _call(name: str, executor: CallableToolExecutor | None = None, **arguments: Any):
    if executor is None:
        executor = CallableToolExecutor()
        build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [GROUP_CHAT, ONE_ON_ONE], ids=["group", "one-on-one"])
@pytest.mark.parametrize(
    ("tool", "action", "verb"),
    [(TOOLS[0], "setReaction", "added to"), (TOOLS[1], "unsetReaction", "removed from")],
)
async def test_a_chat_message_reaction_is_set_or_unset(monkeypatch, chat, tool, action, verb):
    sent = _graph(monkeypatch)

    result = await _call(tool, chat_id=chat, message_id=" 1700000000200 ", reaction=" 🔁 ")

    assert sent == [("post", f"{GRAPH}/chats/{chat}/messages/1700000000200/{action}", {"reactionType": "🔁"})]
    assert result.is_error is False
    assert result.content == f"Reaction 🔁 {verb} chat message 1700000000200."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reaction", "sent_as"),
    [("like", "\U0001f44d"), (" Heart ", "\u2764\ufe0f"), ("\U0001f44d\U0001f3fd", "\U0001f44d\U0001f3fd")],
    ids=["legacy-name", "legacy-name-case", "skin-tone"],
)
async def test_a_chat_reaction_uses_the_channel_emoji_mapping(monkeypatch, reaction, sent_as):
    sent = _graph(monkeypatch)

    await _call(TOOLS[0], chat_id=GROUP_CHAT, message_id="1", reaction=reaction)

    assert sent == [("post", f"{GRAPH}/chats/{GROUP_CHAT}/messages/1/setReaction", {"reactionType": sent_as})]


@pytest.mark.asyncio
async def test_a_graph_refusal_on_a_chat_the_user_is_not_in_is_returned_as_is(monkeypatch):
    forbidden = {"error": {"code": "Forbidden", "message": "User is not a member of the chat."}}
    _graph(monkeypatch, format_http_error(httpx.Response(403, json=forbidden), "Microsoft", "setReaction"))

    result = await _call(TOOLS[0], chat_id=GROUP_CHAT, message_id="1", reaction="👍")

    assert result.content.startswith("Error: Microsoft access denied")
    assert "User is not a member of the chat." in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "arguments",
    [
        {"chat_id": "chat_id", "message_id": "1", "reaction": "👍"},
        {"chat_id": GROUP_CHAT, "message_id": "", "reaction": "👍"},
        {"chat_id": GROUP_CHAT, "message_id": "1", "reaction": "thumbs up"},
        {"chat_id": GROUP_CHAT, "message_id": "1", "reaction": "\U0001f44d\U0001f44e"},
        {"chat_id": GROUP_CHAT, "message_id": "1", "reaction": None},
    ],
    ids=["chat", "message", "words", "two-emojis", "null"],
)
async def test_bad_arguments_send_nothing(monkeypatch, tool, arguments):
    sent = _graph(monkeypatch)

    result = await _call(tool, **arguments)

    assert sent == []
    assert result.content.startswith("Error:")


@pytest.mark.parametrize("tool", TOOLS)
def test_both_tools_are_writes_a_read_only_binding_withholds(tool):
    assert infer_tool_type(tool) == "write"
    executor = CallableToolExecutor()
    defs = build_tools(token="dummy", executor=executor)
    assert tool in {d.name for d in defs}
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="Microsoft")}
    assert tool not in kept


@pytest.mark.parametrize("tool", TOOLS)
def test_a_channel_scope_drops_the_chat_reaction_tools(tool):
    scope = (ScopedChannel(team_id="5b3c2a1d-6e7f-4a8b-9c0d-1e2f3a4b5c6d", channel_id="19:a@thread.tacv2"),)
    names = {d.name for d in build_tools(token="dummy", executor=CallableToolExecutor(), teams_channel_scope=scope)}
    assert tool in DROPPED_WHEN_SCOPED
    assert tool not in names

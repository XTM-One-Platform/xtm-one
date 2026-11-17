"""Adding and removing reactions on Teams channel messages and thread replies."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.teams_scope import ScopedChannel
from app.services.integration_read_only import withhold_write_tools
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

TEAM = "5b3c2a1d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
CHANNEL = "19:0a1b2c3d4e5f60718293a4b5c6d7e8f9@thread.tacv2"
MESSAGE = f"https://graph.microsoft.com/v1.0/teams/{TEAM}/channels/{CHANNEL}/messages/1700000000200"
TOOLS = ["add_microsoft_channel_message_reaction", "remove_microsoft_channel_message_reaction"]


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
@pytest.mark.parametrize(
    ("tool", "action", "verb"),
    [(TOOLS[0], "setReaction", "added to"), (TOOLS[1], "unsetReaction", "removed from")],
)
async def test_a_channel_post_reaction_is_set_or_unset(monkeypatch, tool, action, verb):
    sent = _graph(monkeypatch)

    result = await _call(tool, team_id=TEAM, channel_id=CHANNEL, message_id="1700000000200", reaction=" 🔁 ")

    assert sent == [("post", f"{MESSAGE}/{action}", {"reactionType": "🔁"})]
    assert result.is_error is False
    assert result.content == f"Reaction 🔁 {verb} channel message 1700000000200."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "action", "verb"),
    [(TOOLS[0], "setReaction", "added to"), (TOOLS[1], "unsetReaction", "removed from")],
)
async def test_a_thread_reply_reaction_goes_through_its_root(monkeypatch, tool, action, verb):
    sent = _graph(monkeypatch)

    result = await _call(
        tool,
        team_id=TEAM,
        channel_id=CHANNEL,
        message_id="1700000000201",
        reaction="👍",
        parent_message_id="1700000000200",
    )

    assert sent == [("post", f"{MESSAGE}/replies/1700000000201/{action}", {"reactionType": "👍"})]
    assert result.content == f"Reaction 👍 {verb} channel reply 1700000000201."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reaction", "sent_as"),
    [
        ("like", "\U0001f44d"),
        (" Heart ", "\u2764\ufe0f"),
        ("\U0001f44d\U0001f3fd", "\U0001f44d\U0001f3fd"),  # a skin tone is kept, so unset removes what set added
        ("\U0001f468\u200d\U0001f4bb", "\U0001f468\u200d\U0001f4bb"),  # a ZWJ sequence
        ("\U0001f1eb\U0001f1f7", "\U0001f1eb\U0001f1f7"),  # a flag: two regional indicators
    ],
    ids=["legacy-name", "legacy-name-case", "skin-tone", "zwj", "flag"],
)
async def test_a_reaction_is_sent_to_graph_as_unicode(monkeypatch, reaction, sent_as):
    sent = _graph(monkeypatch)

    result = await _call(TOOLS[1], team_id=TEAM, channel_id=CHANNEL, message_id="1", reaction=reaction)

    assert sent == [("post", f"{MESSAGE.rsplit('/', 1)[0]}/1/unsetReaction", {"reactionType": sent_as})]
    assert result.content == f"Reaction {sent_as} removed from channel message 1."


@pytest.mark.asyncio
async def test_a_graph_refusal_is_returned_as_is(monkeypatch):
    forbidden = {"error": {"code": "Forbidden", "message": "Missing scope ChannelMessage.Send."}}
    _graph(monkeypatch, format_http_error(httpx.Response(403, json=forbidden), "Microsoft", "setReaction"))

    result = await _call(TOOLS[0], team_id=TEAM, channel_id=CHANNEL, message_id="1", reaction="👍")

    assert result.content.startswith("Error: Microsoft access denied")
    assert "Missing scope ChannelMessage.Send." in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "n/a", "reaction": "👍"},
        {"team_id": "na", "channel_id": CHANNEL, "message_id": "1", "reaction": "👍"},
        {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1", "reaction": "👍", "parent_message_id": "{{root}}"},
        {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1", "reaction": " "},
    ],
    ids=["message", "team", "parent", "reaction"],
)
async def test_bad_arguments_send_nothing(monkeypatch, arguments):
    sent = _graph(monkeypatch)

    result = await _call(TOOLS[0], **arguments)

    assert sent == []
    assert result.content.startswith("Error:")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "reaction",
    [
        "",
        "   ",
        "thumbs up",
        "\U0001f44d \U0001f44e",
        "\U0001f44d\U0001f44e",
        "\U0001f44d\U0001f44d",
        "\U0001f501\U0001f501\U0001f501",
        "\U0001f44d\U0001f3fd\U0001f44e",
        "\U0001f1eb\U0001f1f7\U0001f1e9\U0001f1ea",
        "\U0001f44d" * 17,
        "x" * 5000,
        "custom",
        "ok",
        "42",
        None,
    ],
    ids=[
        "empty",
        "blank",
        "words",
        "two-emojis-spaced",
        "two-emojis",
        "same-emoji-twice",
        "same-emoji-three-times",
        "two-emojis-skin-tone",
        "two-flags",
        "too-long",
        "very-long",
        "custom",
        "text",
        "digits",
        "null",
    ],
)
async def test_a_reaction_that_is_no_emoji_is_refused_before_graph(monkeypatch, tool, reaction):
    sent = _graph(monkeypatch)

    result = await _call(tool, team_id=TEAM, channel_id=CHANNEL, message_id="1", reaction=reaction)

    assert sent == []
    assert result.content.startswith(f"Error: {tool} needs one emoji")
    assert len(result.content) < 300


@pytest.mark.parametrize("tool", TOOLS)
def test_both_tools_are_writes_a_read_only_binding_withholds(tool):
    assert infer_tool_type(tool) == "write"
    executor = CallableToolExecutor()
    defs = build_tools(token="dummy", executor=executor)
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="Microsoft")}
    assert tool not in kept


@pytest.mark.asyncio
async def test_a_channel_scope_refuses_a_reaction_outside_its_channels(monkeypatch):
    sent = _graph(monkeypatch)
    executor = CallableToolExecutor()
    scope = (ScopedChannel(team_id=TEAM, channel_id=CHANNEL, team_name="Filigran", channel_name="Testing XTM One"),)
    names = {d.name for d in build_tools(token="dummy", executor=executor, teams_channel_scope=scope)}
    assert set(TOOLS) <= names

    refused = await _call(
        TOOLS[0], executor, team_id=TEAM, channel_id="19:other@thread.tacv2", message_id="1", reaction="👍"
    )
    assert refused.is_error is True
    assert refused.content.startswith("Refused:")
    assert sent == []

    allowed = await _call(TOOLS[0], executor, team_id=TEAM, channel_id=CHANNEL, message_id="1", reaction="👍")
    assert allowed.content == "Reaction 👍 added to channel message 1."

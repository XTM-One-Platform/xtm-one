"""A Microsoft Teams ID stays one Graph URL path segment.

The Teams tools put ``team_id``, ``channel_id``, ``message_id``,
``parent_message_id`` and ``chat_id`` into a Graph URL path, sent with the
user's delegated token. An ID holding ``/``, ``?``, ``#``, ``%``, whitespace or
a dot segment is refused before any request, by every tool and by the Teams
link parser alike; an ID that passes is percent-encoded into its segment.
Synthetic tenant, fake GUIDs, no Graph request.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import graph_path
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft import files as ms_files
from app.integrations.providers.microsoft._teams import _parse_teams_message_link, _teams_segment
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
TEAM = "11111111-1111-4111-8111-111111111111"
CHANNEL = "19:0123456789abcdef0123456789abcdef@thread.tacv2"
CHAT = "19:aaaabbbbccccddddeeeeffff00001111@thread.v2"
ROOT_ID = "1700000000001"
REPLY_ID = "1700000000002"

#: Values that would leave their path segment, or are refused by the shared rule.
HOSTILE_IDS = [
    "1/softDelete",
    "..",
    ".",
    "1?$select=id",
    "1#fragment",
    "1%2FsoftDelete",
    "1%252FsoftDelete",
    "1 2",
    "1\tx",
    "1\\softDelete",
]

#: Every Teams tool that puts an ID into a Graph path, with valid arguments.
TOOLS: dict[str, dict[str, str]] = {
    "list_microsoft_channel_messages": {"team_id": TEAM, "channel_id": CHANNEL},
    "get_microsoft_channel_message": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "message_id": REPLY_ID,
        "parent_message_id": ROOT_ID,
    },
    "send_microsoft_channel_message": {"team_id": TEAM, "channel_id": CHANNEL, "message": "hello"},
    "update_microsoft_channel_message": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "message_id": REPLY_ID,
        "parent_message_id": ROOT_ID,
        "message": "hello",
    },
    "delete_microsoft_channel_message": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "message_id": REPLY_ID,
        "parent_message_id": ROOT_ID,
    },
    "add_microsoft_channel_message_reaction": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "message_id": REPLY_ID,
        "parent_message_id": ROOT_ID,
        "reaction": "like",
    },
    "remove_microsoft_channel_message_reaction": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "message_id": REPLY_ID,
        "parent_message_id": ROOT_ID,
        "reaction": "like",
    },
    "reply_microsoft_channel_message": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "message_id": ROOT_ID,
        "message": "hello",
    },
    "create_microsoft_channel": {"team_id": TEAM, "display_name": "Ops"},
    "list_microsoft_channel_members": {"team_id": TEAM, "channel_id": CHANNEL},
    "download_microsoft_channel_message_image": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "message_id": REPLY_ID,
        "parent_message_id": ROOT_ID,
    },
    "send_microsoft_channel_file": {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "file_id": "file-1",
        "reply_to_message_id": ROOT_ID,
    },
    "list_microsoft_chat_messages": {"chat_id": CHAT},
    "get_microsoft_chat_message": {"chat_id": CHAT, "message_id": ROOT_ID},
    "send_microsoft_chat_message": {"chat_id": CHAT, "message": "hello"},
    "update_microsoft_chat_message": {"chat_id": CHAT, "message_id": ROOT_ID, "message": "hello"},
    "delete_microsoft_chat_message": {"chat_id": CHAT, "message_id": ROOT_ID},
    "add_microsoft_chat_message_reaction": {"chat_id": CHAT, "message_id": ROOT_ID, "reaction": "like"},
    "remove_microsoft_chat_message_reaction": {"chat_id": CHAT, "message_id": ROOT_ID, "reaction": "like"},
    "download_microsoft_chat_message_image": {"chat_id": CHAT, "message_id": ROOT_ID},
    "send_microsoft_chat_file": {"chat_id": CHAT, "file_id": "file-1"},
    "list_microsoft_chat_members": {"chat_id": CHAT},
    "add_microsoft_chat_members": {"chat_id": CHAT, "members": "alice@example.com"},
    "remove_microsoft_chat_member": {"chat_id": CHAT, "member": "alice@example.com"},
    "add_microsoft_team_member": {"team_id": TEAM, "members": "alice@example.com"},
    "remove_microsoft_team_member": {"team_id": TEAM, "member": "alice@example.com"},
    "add_microsoft_channel_member": {"team_id": TEAM, "channel_id": CHANNEL, "members": "alice@example.com"},
    "remove_microsoft_channel_member": {"team_id": TEAM, "channel_id": CHANNEL, "member": "alice@example.com"},
}

#: The ID arguments, and the argument the refusal names.
_ID_ARGUMENTS = {
    "team_id": "team_id",
    "channel_id": "channel_id",
    "message_id": "message_id",
    "parent_message_id": "message_id",
    "reply_to_message_id": "message_id",
    "chat_id": "chat_id",
}

_CASES = [
    pytest.param(tool, argument, value, id=f"{tool}-{argument}-{value!r}")
    for tool, arguments in TOOLS.items()
    for argument in arguments
    if argument in _ID_ARGUMENTS
    for value in HOSTILE_IDS
]


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """A fake Graph recording every URL a tool requests or downloads; it answers nothing."""
    sent: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        sent.append(url)
        return None, "Error: Microsoft: nothing here"

    async def fake_fetch_file(_client, _method, url, **_kwargs):
        sent.append(url)
        raise AssertionError("no download expected")

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    monkeypatch.setattr(ms_files, "fetch_file", fake_fetch_file)
    return sent


def _executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return executor


async def _call(tool: str, **arguments: Any):
    return await _executor().execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "argument", "value"), _CASES)
async def test_an_id_that_leaves_its_path_segment_sends_nothing(graph, tool, argument, value):
    result = await _call(tool, **{**TOOLS[tool], argument: value})

    assert result.is_error
    assert result.content.startswith(f"Error: {_ID_ARGUMENTS[argument]} is missing or invalid (")
    assert graph == []


def test_every_teams_tool_with_an_id_argument_is_covered():
    """A new Teams tool taking an ID joins ``TOOLS``, so its refusal is tested too."""
    definitions = build_tools(token="dummy", executor=CallableToolExecutor())
    with_ids = {
        definition.name
        for definition in definitions
        # A Teams tool names its team, channel or chat (``message_id`` alone is an email's too).
        if set((definition.input_schema or {}).get("properties", {})) & {"team_id", "channel_id", "chat_id"}
    }

    assert with_ids == set(TOOLS)


@pytest.mark.asyncio
async def test_an_id_that_passes_is_percent_encoded_into_its_segment(graph):
    await _call(
        "get_microsoft_channel_message",
        team_id=f" {TEAM} ",
        channel_id=CHANNEL,
        message_id="1700000000002;x=1",
        parent_message_id=ROOT_ID,
    )
    await _call("get_microsoft_chat_message", chat_id=CHAT, message_id="1700000000003&x")

    assert graph == [
        f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/messages/{ROOT_ID}/replies/1700000000002%3Bx%3D1",
        f"{GRAPH}/chats/{CHAT}/messages/1700000000003%26x",
    ]


@pytest.mark.parametrize(
    ("value", "segment"),
    [
        (TEAM, TEAM),
        (CHANNEL, CHANNEL),
        (CHAT, CHAT),
        ("19:meeting_abc@thread.v2", "19:meeting_abc@thread.v2"),
        ("1700000000001", "1700000000001"),
        ("  1700000000001 ", "1700000000001"),
        ("a;b=c+d&e", "a%3Bb%3Dc%2Bd%26e"),
        ("café", "caf%C3%A9"),
        (None, ""),
    ],
)
def test_a_teams_id_is_sent_as_teams_writes_it_and_encoded_otherwise(value, segment):
    assert _teams_segment(value) == segment


@pytest.mark.parametrize(
    "url",
    [
        f"https://teams.microsoft.com/l/message/{CHANNEL}/1%252FsoftDelete?groupId={TEAM}",
        f"https://teams.microsoft.com/l/message/{CHANNEL}/{ROOT_ID}?groupId={TEAM}%252Fchannels",
        f"https://teams.microsoft.com/l/message/{CHANNEL}/{REPLY_ID}?groupId={TEAM}&parentMessageId=1%252F2",
        f"https://teams.microsoft.com/l/message/%2E%2E/{ROOT_ID}?groupId={TEAM}",
        f"https://teams.microsoft.com/l/message/{CHANNEL}/%2E?groupId={TEAM}",
        f"https://teams.microsoft.com/l/message/{CHANNEL}/{ROOT_ID}?groupId=..",
        f"https://teams.microsoft.com/l/message/{CHANNEL}/{REPLY_ID}?groupId={TEAM}&parentMessageId=..",
        f"https://teams.microsoft.com/l/message/{CHAT}/1%25",
    ],
    ids=[
        "double-encoded-message",
        "double-encoded-group",
        "double-encoded-parent",
        "dot-dot-conversation",
        "dot-message",
        "dot-dot-group",
        "dot-dot-parent",
        "percent-message",
    ],
)
def test_a_link_naming_an_id_the_tools_refuse_is_no_teams_message_link(url):
    link, err = _parse_teams_message_link(url)

    assert link is None
    assert err is not None and err.startswith("Error:") and "is not a Teams message link" in err


@pytest.mark.asyncio
async def test_a_link_with_a_double_encoded_id_sends_nothing(graph):
    result = await _call(
        "get_microsoft_channel_message",
        url=f"https://teams.microsoft.com/l/message/{CHANNEL}/1%252FsoftDelete?groupId={TEAM}",
    )

    assert result.is_error
    assert "is not a Teams message link" in result.content
    assert graph == []


@pytest.mark.parametrize("value", [".", ".."])
def test_a_dot_segment_is_no_graph_path_segment(value):
    assert graph_path._validate_path_segment(value, "id") == f"Error: Invalid id: {value!r}."


@pytest.mark.parametrize("value", [TEAM, CHANNEL, CHAT, "...", ".hidden", "a.b"])
def test_a_dotted_id_stays_a_graph_path_segment(value):
    assert graph_path._validate_path_segment(value, "id") is None

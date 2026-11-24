"""#1829: team and channel member writes, SharePoint list columns, raw MIME
export with whole headers, and ``[[FILE:id]]`` markers sent as Teams files.

Synthetic tenant and IDs; Microsoft Graph is a fake recording every request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import MicrosoftIntegration, build_tools
from app.integrations.providers.microsoft._chat_file_markers import split_file_markers
from app.integrations.providers.microsoft._internet_headers import (
    FULL_HEADERS_HINT,
    MAX_HEADER_VALUE_CHARS,
    format_internet_headers,
    header_section,
)
from app.integrations.providers.microsoft.sharepoint import column_type
from app.integrations.providers.microsoft.teams_scope import CHANNEL_ID_TOOLS, DROPPED_WHEN_SCOPED
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

GRAPH = "https://graph.microsoft.com/v1.0"
TEAM = "5b3c2a1d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
CHANNEL = "19:0a1b2c3d4e5f60718293a4b5c6d7e8f9@thread.tacv2"
CHAT = "19:0f1e2d3c4b5a69788796a5b4c3d2e1f0@thread.v2"
MESSAGE = "AAMkADExampleMessageId" + "A" * 40 + "="
FILE_A = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"
FILE_B = "1c2d3e4f-5061-4728-93a4-b5c6d7e8f9a0"
ALICE = {"id": "MCMjMSMj-alice", "displayName": "Alice Example", "email": "alice@example.com", "userId": "u-alice"}
BOB = {"id": "MCMjMSMj-bob", "displayName": "Bob Example", "email": "bob@example.com", "userId": "u-bob"}
FORBIDDEN = {"error": {"code": "Forbidden", "message": "Missing scope permissions on the request."}}


class _Response:
    def __init__(self, payload: Any = None, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


class _Members:
    """A fake Graph holding the members of one team and of one of its channels."""

    def __init__(self, deny_post: bool = False) -> None:
        self.members = {f"{GRAPH}/teams/{TEAM}": [ALICE], f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}": [ALICE]}
        self.deny_post = deny_post
        self.sent: list[tuple[str, str, Any]] = []

    async def __call__(self, _client, method, url, _provider, op, **kwargs):
        self.sent.append((method, url, kwargs.get("json")))
        for owner, members in self.members.items():
            if url == f"{owner}/members" and method == "get":
                return _Response({"value": list(members)}), None
            if url == f"{owner}/members" and method == "post":
                if self.deny_post:
                    return None, format_http_error(httpx.Response(403, json=FORBIDDEN), "Microsoft", op)
                members.append(BOB)
                return _Response({"id": BOB["id"]}), None
            if method == "delete" and url.startswith(f"{owner}/members/"):
                membership = url.rsplit("/", 1)[-1]
                self.members[owner] = [m for m in members if m["id"] != membership]
                return _Response(None), None
        raise AssertionError(f"unexpected {method} {url}")


async def _call(name: str, **arguments: Any):
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))


@pytest.fixture
def members(monkeypatch):
    fake = _Members()
    monkeypatch.setattr(ms._common, "safe_request", fake)
    return fake


@pytest.mark.asyncio
async def test_a_team_member_is_added_and_the_members_are_read_back(members):
    result = await _call("add_microsoft_team_member", team_id=TEAM, members="bob@example.com")

    assert not result.is_error, result.content
    assert "Added to the team as member: bob@example.com." in result.content
    assert "Bob Example <bob@example.com>" in result.content
    posted = [body for method, url, body in members.sent if method == "post"]
    assert posted == [
        {
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": [],
            "user@odata.bind": f"{GRAPH}/users('bob@example.com')",
        }
    ]


@pytest.mark.asyncio
async def test_a_channel_member_is_added_as_owner_on_the_channel_url(members):
    result = await _call(
        "add_microsoft_channel_member", team_id=TEAM, channel_id=CHANNEL, members="bob@example.com", owner=True
    )

    assert not result.is_error, result.content
    assert "Added to the channel as owner" in result.content
    post = next((url, body) for method, url, body in members.sent if method == "post")
    assert post[0] == f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/members"
    assert post[1]["roles"] == ["owner"]


@pytest.mark.asyncio
async def test_a_member_already_there_is_left_unchanged(members):
    result = await _call("add_microsoft_team_member", team_id=TEAM, members="ALICE@example.com")

    assert "Already members, left unchanged: ALICE@example.com." in result.content
    assert not [m for m, _, _ in members.sent if m == "post"]


@pytest.mark.asyncio
async def test_a_member_is_removed_by_email(members):
    result = await _call(
        "remove_microsoft_channel_member", team_id=TEAM, channel_id=CHANNEL, member="alice@example.com"
    )

    assert not result.is_error, result.content
    assert "Removed Alice Example <alice@example.com> from the channel." in result.content
    assert ("delete", f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/members/{ALICE['id']}", None) in members.sent


@pytest.mark.asyncio
async def test_a_denied_add_names_the_permission(monkeypatch):
    fake = _Members(deny_post=True)
    monkeypatch.setattr(ms._common, "safe_request", fake)

    result = await _call("add_microsoft_team_member", team_id=TEAM, members="bob@example.com")

    assert result.is_error
    assert result.content.startswith("Error: nobody was added to the team.")
    assert "TeamMember.ReadWrite.All" in result.content


def test_the_member_writes_are_write_tools_and_follow_the_channel_scope():
    for tool in (
        "add_microsoft_team_member",
        "remove_microsoft_team_member",
        "add_microsoft_channel_member",
        "remove_microsoft_channel_member",
    ):
        assert infer_tool_type(tool) == "write"
    assert {"add_microsoft_team_member", "remove_microsoft_team_member"} <= DROPPED_WHEN_SCOPED
    assert {"add_microsoft_channel_member", "remove_microsoft_channel_member"} <= CHANNEL_ID_TOOLS


def test_the_integration_requests_the_member_write_permissions():
    scopes = MicrosoftIntegration().get_oauth_defaults().scopes
    assert {"TeamMember.ReadWrite.All", "ChannelMember.ReadWrite.All"} <= set(scopes)


@pytest.mark.asyncio
async def test_list_columns_answers_the_definitions_without_the_hidden_ones(monkeypatch):
    sent: list[str] = []
    columns = [
        {"name": "Title", "displayName": "Title", "text": {}, "required": True},
        {"name": "Status0", "displayName": "Status", "choice": {"choices": ["Open", "Closed"]}},
        {"name": "_ModerationStatus", "displayName": "Approval", "hidden": True, "number": {}},
    ]

    async def fake(_client, method, url, _provider, _op, **_kwargs):
        sent.append(url)
        if url.endswith("/columns"):
            return _Response({"value": columns}), None
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(ms._common, "safe_request", fake)
    result = await _call("list_microsoft_list_columns", site_id="site-1", library="Risks")

    assert not result.is_error, result.content
    assert "2 column(s) of 'Risks' (hidden ones left out)" in result.content
    assert "- Title (display: Title, type: text, required)" in result.content
    assert "- Status0 (display: Status, type: choice) choices: Open, Closed" in result.content
    assert "_ModerationStatus" not in result.content
    assert sent[-1] == f"{GRAPH}/sites/site-1/lists/Risks/columns"


def test_a_column_type_is_its_facet():
    assert column_type({"dateTime": {"format": "dateOnly"}}) == "dateTime"
    assert column_type({"name": "x"}) == "unknown"


def test_a_cut_header_block_names_the_whole_export():
    block = format_internet_headers([{"name": "ARC-Seal", "value": "x" * (MAX_HEADER_VALUE_CHARS + 10)}])
    assert "[10 chars cut]" in block
    assert FULL_HEADERS_HINT in block
    assert FULL_HEADERS_HINT not in format_internet_headers([{"name": "From", "value": "a@example.com"}])


def test_the_header_section_stops_at_the_body():
    raw = b"Received: from a\r\n\tby b\r\nSubject: hi\r\n\r\nbody text\r\n"
    assert header_section(raw) == "Received: from a\n\tby b\nSubject: hi"


@pytest.mark.asyncio
async def test_the_raw_mime_is_kept_as_an_eml_and_its_headers_answered_whole(monkeypatch):
    long_value = "y" * 1200
    raw = f"Received: {long_value}\r\nSubject: hi\r\n\r\nbody\r\n".encode()
    kept: list[tuple[str, bytes, str]] = []

    async def fake(_client, method, url, _provider, _op, **_kwargs):
        assert (method, url) == ("get", f"{GRAPH}/me/messages/{MESSAGE}/$value")
        return _Response(content=raw), None

    async def persist(name, data, mime):
        kept.append((name, data, mime))
        return None  # keep_file then keeps it in memory, as without a conversation

    monkeypatch.setattr(ms._common, "safe_request", fake)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor, persist_file_fn=persist)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="export_microsoft_email_mime", arguments={"message_id": MESSAGE})
    )

    assert not result.is_error, result.content
    assert kept and kept[0][0] == "message.eml" and kept[0][1] == raw and kept[0][2] == "message/rfc822"
    assert long_value in result.content
    assert "body" not in result.content.split("Header section, whole")[1]


def test_file_markers_are_split_out_of_the_text():
    text, ids = split_file_markers(f"Here you go:\n[[FILE:{FILE_A}]]\n[[file:{FILE_B}]] and [[FILE:{FILE_A}]]")
    assert text == "Here you go:\n\n and"
    assert ids == [FILE_A, FILE_B]


@pytest.mark.asyncio
async def test_a_chat_message_with_markers_sends_the_text_then_each_file():
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    calls: list[tuple[str, dict]] = []

    async def fake_send(**kwargs):
        calls.append(("message", kwargs))
        return "Chat message sent. Message ID: m1."

    async def fake_file(**kwargs):
        calls.append(("file", kwargs))
        return f"File sent: {kwargs['file_id']}"

    executor.register("send_microsoft_chat_message", fake_send)
    executor.register("send_microsoft_chat_file", fake_file)
    from app.integrations.providers.microsoft._chat_file_markers import wrap_chat_send_file_markers

    wrap_chat_send_file_markers(executor)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="send_microsoft_chat_message",
            arguments={"chat_id": CHAT, "message": f"Screens:\n[[FILE:{FILE_A}]]\n[[FILE:{FILE_B}]]"},
        )
    )

    assert not result.is_error, result.content
    assert [kind for kind, _ in calls] == ["message", "file", "file"]
    assert calls[0][1]["message"] == "Screens:"
    assert [kw["file_id"] for kind, kw in calls if kind == "file"] == [FILE_A, FILE_B]
    assert "2 file marker(s) sent as Teams files" in result.content
    assert "[[FILE:" not in calls[0][1]["message"]


@pytest.mark.asyncio
async def test_a_file_that_fails_makes_the_send_an_error():
    executor = CallableToolExecutor()

    async def fake_send(**kwargs):
        return "Chat message sent."

    async def fake_file(**kwargs):
        return ToolResult(tool_call_id="", content="Error: no such file", is_error=True)

    executor.register("send_microsoft_chat_message", fake_send)
    executor.register("send_microsoft_chat_file", fake_file)
    from app.integrations.providers.microsoft._chat_file_markers import wrap_chat_send_file_markers

    wrap_chat_send_file_markers(executor)
    result = await executor.execute_tool(
        ToolCall(
            id="c1", name="send_microsoft_chat_message", arguments={"chat_id": CHAT, "message": f"[[FILE:{FILE_A}]]"}
        )
    )

    assert result.is_error
    assert "1 file marker(s) NOT sent" in result.content


@pytest.mark.asyncio
async def test_file_markers_survive_the_outbound_rewrite_of_an_agent_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform wraps every send tool with ``send_markers_as_links`` after
    ``build_tools`` (``integration_tools``): the chat send keeps its file markers
    through it and sends them as files; any other marker is still linked."""
    from contextlib import asynccontextmanager

    from app.integrations.providers.microsoft._chat_file_markers import wrap_chat_send_file_markers
    from app.services.outbound_markers import send_markers_as_links

    deck = "2d3e4f50-6172-4839-a4b5-c6d7e8f9a0b1"

    async def link_files(_db: Any, text: str, _base: str, **_scope: Any) -> str:
        return text.replace(f"[[FILE:{FILE_A}]]", "[a.pdf](https://api.example.com/f)")

    async def link_decks(_db: Any, text: str, **_scope: Any) -> str:
        return text.replace(f"[[PRESENTATION:{deck}]]", "[Deck](https://app.example.com/d)")

    @asynccontextmanager
    async def session() -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr("app.channels.base.resolve_channel_file_markers", link_files)
    monkeypatch.setattr("app.services.platform_chat_artifacts.markers_as_links", link_decks)
    monkeypatch.setattr("app.core.database.async_session", session)

    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_send(**kwargs: Any) -> str:
        calls.append(("message", kwargs))
        return "Chat message sent. Message ID: m1."

    async def fake_file(**kwargs: Any) -> str:
        calls.append(("file", kwargs))
        return f"File sent: {kwargs['file_id']}"

    defs = build_tools(token="dummy", executor=CallableToolExecutor())
    executor = CallableToolExecutor()
    executor.register("send_microsoft_chat_message", fake_send)
    executor.register("send_microsoft_chat_file", fake_file)
    wrap_chat_send_file_markers(executor)
    send_markers_as_links(defs, executor, {"conversation_id": TEAM, "user_id": TEAM})

    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="send_microsoft_chat_message",
            arguments={"chat_id": CHAT, "message": f"See [[PRESENTATION:{deck}]]\n[[FILE:{FILE_A}]]"},
        )
    )

    assert not result.is_error, result.content
    assert [kind for kind, _ in calls] == ["message", "file"]
    assert calls[0][1]["message"] == "See [Deck](https://app.example.com/d)"
    assert calls[1][1]["file_id"] == FILE_A

"""Adding, listing and removing the members of a Microsoft Teams chat.

Synthetic tenant and IDs; Microsoft Graph is a fake recording every request.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import MicrosoftIntegration, build_tools
from app.integrations.providers.microsoft.teams_scope import DROPPED_WHEN_SCOPED, ScopedChannel
from app.services.integration_read_only import withhold_write_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
CHAT = "19:0f1e2d3c4b5a69788796a5b4c3d2e1f0@thread.v2"
CREATED = "2026-10-30T08:00:00.000Z"
TEAM = "5b3c2a1d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
CHANNEL = "19:0a1b2c3d4e5f60718293a4b5c6d7e8f9@thread.tacv2"

ME = {
    "id": "MCMjMSMj-me",
    "displayName": "Signed In",
    "email": "me@example.com",
    "userId": "a1b2c3d4-0000-4000-8000-000000000001",
    "roles": ["owner"],
}
ALICE = {
    "id": "MCMjMSMj-alice",
    "displayName": "Alice Example",
    "email": "alice@example.com",
    "userId": "a1b2c3d4-0000-4000-8000-000000000002",
    "roles": ["owner"],
}
BOB = {
    "id": "MCMjMSMj-bob",
    "displayName": "Bob Example",
    "email": "bob@example.com",
    "userId": "a1b2c3d4-0000-4000-8000-000000000003",
    "roles": ["owner"],
}
FORBIDDEN = {"error": {"code": "Forbidden", "message": "Missing scope permissions on the request."}}


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


class _Graph:
    """A fake Graph holding one chat; records every request."""

    def __init__(
        self,
        chat_type: str = "group",
        members: list[dict] | None = None,
        deny: str = "",
        page_size: int = 100,
        refuse: str = "",
    ) -> None:
        self.chat_type = chat_type
        self.members = list(members if members is not None else [ME, ALICE])
        self.deny = deny
        self.page_size = page_size
        self.refuse = refuse
        self.sent: list[tuple[str, str, dict | None]] = []

    async def __call__(self, _client, method, url, _provider, op, **kwargs):
        self.sent.append((method, url, kwargs.get("json")))
        if self.deny and self.deny == method:
            return None, format_http_error(httpx.Response(403, json=FORBIDDEN), "Microsoft", op)
        if method == "get" and url == f"{GRAPH}/chats/{CHAT}":
            return _Response({"id": CHAT, "chatType": self.chat_type, "createdDateTime": CREATED}), None
        if method == "get" and url.startswith(f"{GRAPH}/chats/{CHAT}/members"):
            start = int(url.split("skip=")[1]) if "skip=" in url else 0
            page: dict = {"value": self.members[start : start + self.page_size]}
            if start + self.page_size < len(self.members):
                page["@odata.nextLink"] = f"{GRAPH}/chats/{CHAT}/members?skip={start + self.page_size}"
            return _Response(page), None
        if method == "post" and url == f"{GRAPH}/chats/{CHAT}/members":
            ref = kwargs["json"]["user@odata.bind"].split("'")[1]
            if ref == self.refuse:
                return None, format_http_error(httpx.Response(400, json=FORBIDDEN), "Microsoft", op)
            self.members.append({**BOB, "email": ref} if ref != BOB["email"] else BOB)
            return _Response({"id": "MCMjMSMj-new"}), None
        if method == "delete" and url.startswith(f"{GRAPH}/chats/{CHAT}/members/"):
            membership = url.rsplit("/", 1)[-1]
            self.members = [m for m in self.members if m["id"] != membership]
            return _Response(None), None
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def graph(monkeypatch):
    fake = _Graph()
    monkeypatch.setattr(ms._common, "safe_request", fake)
    return fake


async def _call(name: str, **arguments: Any):
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))


@pytest.mark.asyncio
async def test_a_member_is_added_with_the_history_and_the_members_are_read_back(graph):
    result = await _call("add_microsoft_chat_members", chat_id=CHAT, members="bob@example.com")

    posts = [(url, body) for method, url, body in graph.sent if method == "post"]
    assert posts == [
        (
            f"{GRAPH}/chats/{CHAT}/members",
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{GRAPH}/users('bob@example.com')",
                "visibleHistoryStartDateTime": CREATED,
            },
        )
    ]
    assert graph.sent[-1][:2] == ("get", f"{GRAPH}/chats/{CHAT}/members")
    assert result.is_error is False
    assert result.content.startswith(f"Added to chat {CHAT} (with the chat history): bob@example.com.")
    assert "The chat now has 3 member(s):" in result.content
    assert "Bob Example <bob@example.com>" in result.content


@pytest.mark.asyncio
async def test_without_history_no_history_start_is_sent(graph):
    result = await _call(
        "add_microsoft_chat_members", chat_id=CHAT, members="bob@example.com", include_history=False
    )

    body = next(body for method, _url, body in graph.sent if method == "post")
    assert "visibleHistoryStartDateTime" not in body
    assert "(without the earlier history)" in result.content


@pytest.mark.asyncio
async def test_an_existing_member_is_left_unchanged(graph):
    result = await _call("add_microsoft_chat_members", chat_id=CHAT, members="ALICE@example.com, bob@example.com")

    posts = [body["user@odata.bind"] for method, _url, body in graph.sent if method == "post"]
    assert posts == [f"{GRAPH}/users('bob@example.com')"]
    assert "Already members, left unchanged: ALICE@example.com." in result.content


@pytest.mark.asyncio
async def test_a_partial_add_is_reported_as_an_error(monkeypatch):
    fake = _Graph(refuse="carol@example.com")
    monkeypatch.setattr(ms._common, "safe_request", fake)

    result = await _call(
        "add_microsoft_chat_members", chat_id=CHAT, members="alice@example.com, carol@example.com"
    )

    assert result.is_error is True
    assert result.content.startswith(f"Error: not every member was added to chat {CHAT}.")
    assert "Already members, left unchanged: alice@example.com." in result.content
    assert "Not added:\n- carol@example.com: " in result.content


@pytest.mark.asyncio
async def test_every_page_of_members_is_read(monkeypatch):
    fake = _Graph(members=[ME, ALICE, BOB], page_size=2)
    monkeypatch.setattr(ms._common, "safe_request", fake)

    listing = await _call("list_microsoft_chat_members", chat_id=CHAT)
    assert listing.content.startswith(f"Chat {CHAT} has 3 member(s):")

    added = await _call("add_microsoft_chat_members", chat_id=CHAT, members="bob@example.com")
    assert "Already members, left unchanged: bob@example.com." in added.content
    assert not [m for m, _u, _b in fake.sent if m == "post"]

    removed = await _call("remove_microsoft_chat_member", chat_id=CHAT, member="bob@example.com")
    assert ("delete", f"{GRAPH}/chats/{CHAT}/members/MCMjMSMj-bob", None) in fake.sent
    assert removed.is_error is False


@pytest.mark.asyncio
async def test_a_one_on_one_chat_is_refused_before_any_write(monkeypatch):
    fake = _Graph(chat_type="oneOnOne")
    monkeypatch.setattr(ms._common, "safe_request", fake)

    result = await _call("add_microsoft_chat_members", chat_id=CHAT, members="bob@example.com")

    assert result.is_error is True
    assert "one-on-one chat" in result.content
    assert [method for method, _url, _body in fake.sent] == ["get"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "members",
    ["", " , ", "bob", "bob@example.com')/x", "x@y.com,users('a')"],
    ids=["empty", "commas", "no-domain", "quote", "second-bad"],
)
async def test_a_member_that_is_no_email_or_object_id_sends_nothing(graph, members):
    result = await _call("add_microsoft_chat_members", chat_id=CHAT, members=members)

    assert result.is_error is True
    assert result.content.startswith("Error:")
    assert graph.sent == []


@pytest.mark.asyncio
async def test_a_refused_add_names_the_permission(monkeypatch):
    fake = _Graph(deny="post")
    monkeypatch.setattr(ms._common, "safe_request", fake)

    result = await _call("add_microsoft_chat_members", chat_id=CHAT, members="bob@example.com")

    assert result.is_error is True
    assert result.content.startswith(f"Error: nobody was added to chat {CHAT}.")
    assert "access denied" in result.content
    assert "ChatMember.ReadWrite" in result.content


@pytest.mark.asyncio
async def test_the_members_are_listed(graph):
    result = await _call("list_microsoft_chat_members", chat_id=CHAT)

    assert graph.sent == [("get", f"{GRAPH}/chats/{CHAT}/members", None)]
    assert result.content.startswith(f"Chat {CHAT} has 2 member(s):")
    assert "Alice Example <alice@example.com>, role: owner (membership id: MCMjMSMj-alice" in result.content


@pytest.mark.asyncio
async def test_a_member_is_removed_by_email_through_its_membership_id(graph):
    result = await _call("remove_microsoft_chat_member", chat_id=CHAT, member="Alice@Example.com")

    assert ("delete", f"{GRAPH}/chats/{CHAT}/members/MCMjMSMj-alice", None) in graph.sent
    assert result.is_error is False
    assert result.content.startswith(f"Removed Alice Example <alice@example.com> from chat {CHAT}.")
    assert "The chat now has 1 member(s):" in result.content


@pytest.mark.asyncio
async def test_removing_someone_who_is_not_a_member_deletes_nothing(graph):
    result = await _call("remove_microsoft_chat_member", chat_id=CHAT, member="bob@example.com")

    assert result.is_error is True
    assert "is not a member" in result.content
    assert all(method == "get" for method, _url, _body in graph.sent)


@pytest.mark.asyncio
async def test_a_refused_removal_names_the_permission(monkeypatch):
    fake = _Graph(deny="delete")
    monkeypatch.setattr(ms._common, "safe_request", fake)

    result = await _call("remove_microsoft_chat_member", chat_id=CHAT, member="alice@example.com")

    assert result.is_error is True
    assert "ChatMember.ReadWrite" in result.content


def test_the_listing_is_a_read_and_add_and_remove_are_writes():
    assert infer_tool_type("list_microsoft_chat_members") == "read"
    assert infer_tool_type("add_microsoft_chat_members") == "write"
    assert infer_tool_type("remove_microsoft_chat_member") == "write"
    executor = CallableToolExecutor()
    defs = build_tools(token="dummy", executor=executor)
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="Microsoft")}
    assert "list_microsoft_chat_members" in kept
    assert "add_microsoft_chat_members" not in kept
    assert "remove_microsoft_chat_member" not in kept


def test_a_teams_channel_scope_withdraws_the_chat_member_tools():
    executor = _SafeIntegrationExecutor()
    scope = (ScopedChannel(team_id=TEAM, channel_id=CHANNEL),)
    names = {d.name for d in build_tools(token="dummy", executor=executor, teams_channel_scope=scope)}

    for tool in ("list_microsoft_chat_members", "add_microsoft_chat_members", "remove_microsoft_chat_member"):
        assert tool in DROPPED_WHEN_SCOPED
        assert tool not in names


def test_the_integration_requests_the_chat_member_permission():
    assert "ChatMember.ReadWrite" in MicrosoftIntegration().get_oauth_defaults().scopes

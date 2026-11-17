"""A Microsoft Teams message is read in full, and never cut silently.

The channel and chat listings show each message whole up to
``MICROSOFT_TEAMS_MESSAGE_TEXT_LIMIT``; a longer one is cut at a word boundary
and ends with the exact read call, with its real IDs, which the read-one tools
(``get_microsoft_channel_message``, ``get_microsoft_chat_message``) answer with
the whole message. They also take the Teams link of a message instead of its
IDs. Synthetic tenant: Alice, Bob, fake GUIDs.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._teams import (
    _TEAMS_LISTING_TEXT_BUDGET,
    TeamsMessageLink,
    _clip_listing_texts,
    _clip_message_text,
    _parse_teams_message_link,
)
from app.integrations.providers.microsoft.teams_scope import (
    TEAMS_CHANNEL_SCOPE_KEY,
    teams_channel_scope_from_bindings,
)
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
TENANT = "00000000-0000-4000-8000-000000000000"
TEAM = "11111111-1111-4111-8111-111111111111"
CHANNEL = "19:0123456789abcdef0123456789abcdef@thread.tacv2"
OTHER_CHANNEL = "19:fedcba9876543210fedcba9876543210@thread.tacv2"
CHAT = "19:aaaabbbbccccddddeeeeffff00001111@thread.v2"
ROOT_ID = "1700000000001"
REPLY_ID = "1700000000002"
CHAT_MESSAGE_ID = "1700000000003"
CHANNEL_MESSAGES = f"/teams/{TEAM}/channels/{CHANNEL}/messages"

REPLY_LINK = (
    f"https://teams.microsoft.com/l/message/{CHANNEL}/{REPLY_ID}?tenantId={TENANT}&groupId={TEAM}"
    f"&parentMessageId={ROOT_ID}&teamName=Security&channelName=General&createdTime={REPLY_ID}"
)
OTHER_CHANNEL_LINK = (
    f"https://teams.microsoft.com/l/message/{OTHER_CHANNEL}/{ROOT_ID}?tenantId={TENANT}&groupId={TEAM}"
    f"&parentMessageId={ROOT_ID}"
)
CHAT_LINK = (
    f"https://teams.microsoft.com/l/message/{CHAT}/{CHAT_MESSAGE_ID}"
    "?context=%7B%22contextType%22%3A%22chat%22%7D"
)

#: The read call a truncation marker names, as ``(tool, "k=v, k=v")``.
_READ_CALL_RE = re.compile(r"read it in full with (get_microsoft_\w+)\(([^)]*)\)\]")


def _recent(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _memo(words: int, author: str) -> str:
    """A long post of numbered words, so a cut shows where it fell."""
    return f"Memo from {author}: " + " ".join(f"point{i}" for i in range(words))


def _message(message_id: str, author: str, text: str, minutes_ago: int = 5, **extra: Any) -> dict:
    return {
        "id": message_id,
        "messageType": "message",
        "createdDateTime": _recent(minutes_ago),
        "from": {"user": {"id": f"user-{author.lower()}", "displayName": author}},
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
        **extra,
    }


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """A fake Graph: ``routes`` maps a URL path (after ``/v1.0``) to its JSON answer."""
    routes: dict[str, dict] = {}
    sent: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        sent.append(url)
        path = url.removeprefix(GRAPH)
        if path in routes:
            return _Response(routes[path]), None
        return None, f"Microsoft: nothing at {path}"

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return routes, sent


def _executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return executor


async def _call(executor, tool: str, **arguments: Any):
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


def _read_calls(listing: str) -> list[tuple[str, dict[str, str]]]:
    """Every read call the listing's truncation markers name, in order."""
    return [
        (tool, dict(argument.split("=", 1) for argument in arguments.split(", ")))
        for tool, arguments in _READ_CALL_RE.findall(listing)
    ]


def _shown(listing: str, text: str) -> str:
    """The part of *text* a listing shows before its truncation marker."""
    start = listing.index(text[:40])
    return listing[start : listing.index(" ... [truncated", start)]


# ---------------------------------------------------------------------------
# Listings cut a message only when it does not fit, and say how to read it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_long_post_and_reply_are_listed_with_the_marker_and_read_in_full(graph):
    routes, sent = graph
    post_text, reply_text = _memo(1500, "Alice"), _memo(1200, "Bob")
    post = _message(ROOT_ID, "Alice", post_text, minutes_ago=10)
    reply = _message(REPLY_ID, "Bob", reply_text, minutes_ago=5, replyToId=ROOT_ID)
    routes[CHANNEL_MESSAGES] = {"value": [post]}
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}/replies"] = {"value": [reply]}
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}"] = post
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}/replies/{REPLY_ID}"] = reply
    executor = _executor()

    listing = await _call(executor, "list_microsoft_channel_messages", team_id=TEAM, channel_id=CHANNEL)

    assert not listing.is_error, listing.content
    calls = _read_calls(listing.content)
    assert calls == [
        ("get_microsoft_channel_message", {"team_id": TEAM, "channel_id": CHANNEL, "message_id": ROOT_ID}),
        (
            "get_microsoft_channel_message",
            {"team_id": TEAM, "channel_id": CHANNEL, "message_id": REPLY_ID, "parent_message_id": ROOT_ID},
        ),
    ]
    for text in (post_text, reply_text):
        shown = _shown(listing.content, text)
        assert text.startswith(shown)
        assert len(shown) <= settings.microsoft_teams_message_text_limit
        assert text[len(shown)] == " ", "the cut falls between two words"
        assert f"[truncated, {len(text) - len(shown)} more characters:" in listing.content

    for (tool, arguments), text in zip(calls, (post_text, reply_text), strict=True):
        full = await _call(executor, tool, **arguments)
        assert not full.is_error, full.content
        assert text in full.content
        assert "[truncated" not in full.content
    assert f"{GRAPH}{CHANNEL_MESSAGES}/{ROOT_ID}/replies/{REPLY_ID}" in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("listing_tool", ["list_microsoft_chat_messages", "list_all_microsoft_chat_messages"])
async def test_a_long_chat_message_is_listed_with_the_marker_and_read_in_full(graph, listing_tool):
    routes, sent = graph
    text = _memo(1500, "Bob")
    message = _message(CHAT_MESSAGE_ID, "Bob", text, chatId=CHAT)
    routes["/me/chats"] = {
        "value": [
            {
                "id": CHAT,
                "chatType": "group",
                "topic": "Launch",
                "members": [{"displayName": "Alice"}, {"displayName": "Bob"}],
            }
        ]
    }
    routes[f"/me/chats/{CHAT}/messages"] = {"value": [message]}
    routes[f"/chats/{CHAT}/messages/{CHAT_MESSAGE_ID}"] = message
    executor = _executor()

    arguments = {"chat_id": CHAT} if listing_tool == "list_microsoft_chat_messages" else {}
    listing = await _call(executor, listing_tool, **arguments)

    assert not listing.is_error, listing.content
    calls = _read_calls(listing.content)
    assert calls == [("get_microsoft_chat_message", {"chat_id": CHAT, "message_id": CHAT_MESSAGE_ID})]
    shown = _shown(listing.content, text)
    assert text.startswith(shown) and text[len(shown)] == " "
    assert f"[truncated, {len(text) - len(shown)} more characters:" in listing.content

    tool, call_arguments = calls[0]
    full = await _call(executor, tool, **call_arguments)
    assert not full.is_error, full.content
    assert text in full.content
    assert f"Teams chat message {CHAT_MESSAGE_ID} (chat_id: {CHAT})" in full.content
    assert sent[-1] == f"{GRAPH}/chats/{CHAT}/messages/{CHAT_MESSAGE_ID}"


@pytest.mark.asyncio
async def test_a_message_that_fits_is_listed_verbatim(graph):
    routes, _ = graph
    limit = settings.microsoft_teams_message_text_limit
    exact, short, over = "x" * limit, "Short note from Bob: ship it on Friday.", "y" * (limit + 1)
    routes[CHANNEL_MESSAGES] = {
        "value": [
            _message("1700000000020", "Alice", exact, minutes_ago=9),
            _message("1700000000021", "Bob", short, minutes_ago=8),
            _message("1700000000022", "Alice", over, minutes_ago=7),
        ]
    }

    listing = await _call(
        _executor(), "list_microsoft_channel_messages", team_id=TEAM, channel_id=CHANNEL, max_reply_threads=0
    )

    assert f"\n{exact}\n(Message ID: 1700000000020)" in listing.content
    assert f"\n{short}\n(Message ID: 1700000000021)" in listing.content
    # No space to cut at: the cut is exact, and the one character left is counted.
    assert f"\n{'y' * limit} ... [truncated, 1 more characters:" in listing.content
    assert listing.content.count("[truncated") == 1


@pytest.mark.asyncio
async def test_a_card_only_and_a_deleted_message_keep_their_rendering(graph):
    routes, _ = graph
    card = {
        "type": "AdaptiveCard",
        "body": [
            {"type": "TextBlock", "text": "Weekly sync"},
            {"type": "FactSet", "facts": [{"title": "Owner", "value": "Alice"}]},
        ],
    }
    card_message = {
        "id": "1700000000010",
        "messageType": "message",
        "createdDateTime": _recent(9),
        "from": {"application": {"id": "app-1", "displayName": "Workflows"}},
        "body": {"contentType": "html", "content": '<attachment id="c1"></attachment>'},
        "attachments": [
            {"id": "c1", "contentType": "application/vnd.microsoft.card.adaptive", "content": json.dumps(card)}
        ],
    }
    deleted = _message("1700000000011", "Bob", "", minutes_ago=8, deletedDateTime=_recent(1))
    routes[CHANNEL_MESSAGES] = {"value": [card_message, deleted]}
    routes[f"{CHANNEL_MESSAGES}/1700000000011"] = deleted
    routes[f"{CHANNEL_MESSAGES}/1700000000011/replies"] = {"value": []}
    executor = _executor()

    listing = await _call(
        executor, "list_microsoft_channel_messages", team_id=TEAM, channel_id=CHANNEL, max_reply_threads=0
    )

    assert (
        "Workflows (application):\n[adaptive card] Weekly sync\nOwner: Alice\n(Message ID: 1700000000010)"
        in listing.content
    )
    assert "Bob (user):\n[message deleted]\n(Message ID: 1700000000011)" in listing.content
    read = await _call(
        executor, "get_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, message_id="1700000000011"
    )
    assert f"Deleted: {deleted['deletedDateTime']}" in read.content
    assert read.content.endswith("\n\n[message deleted]")


def test_a_busy_listing_shares_its_budget_and_keeps_short_messages_whole():
    long_texts = [_memo(1000, f"Bob{i}") for i in range(30)]
    short_texts = [f"Short note {i} from Alice." for i in range(5)]
    entries = [(text, f"read_call_{i}()") for i, text in enumerate(long_texts + short_texts)]

    shown = _clip_listing_texts(entries)

    assert shown[30:] == short_texts
    kept = [text.split(" ... [truncated", 1)[0] for text in shown[:30]]
    for i, text in enumerate(shown[:30]):
        assert text.endswith(f"read it in full with read_call_{i}()]")
    assert sum(map(len, kept)) + sum(map(len, short_texts)) <= _TEAMS_LISTING_TEXT_BUDGET
    assert all(len(text) < settings.microsoft_teams_message_text_limit for text in kept)


def test_a_cut_falls_on_a_word_boundary_and_counts_what_is_left():
    marker = " ... [truncated, {} more characters: read it in full with c()]"
    assert _clip_message_text("fits", 4, "c()") == "fits"
    assert _clip_message_text("alpha beta gamma delta", 13, "c()") == "alpha beta" + marker.format(12)
    assert _clip_message_text("alpha beta gamma", 10, "c()") == "alpha beta" + marker.format(6)
    # A boundary further back than half the cap is not worth it: cut exactly.
    assert _clip_message_text("a " + "x" * 30, 20, "c()") == "a " + "x" * 18 + marker.format(12)


@pytest.mark.asyncio
async def test_the_limit_is_configurable(graph, monkeypatch):
    monkeypatch.setattr(settings, "microsoft_teams_message_text_limit", 100)
    routes, _ = graph
    text = _memo(40, "Alice")
    routes[CHANNEL_MESSAGES] = {"value": [_message(ROOT_ID, "Alice", text)]}

    listing = await _call(
        _executor(), "list_microsoft_channel_messages", team_id=TEAM, channel_id=CHANNEL, max_reply_threads=0
    )

    assert len(_shown(listing.content, text)) <= 100
    assert f"message_id={ROOT_ID})]" in listing.content


def test_the_limit_must_be_positive():
    assert Settings(_env_file=None).microsoft_teams_message_text_limit == 4000
    with pytest.raises(ValidationError, match="must be a positive integer"):
        Settings(_env_file=None, microsoft_teams_message_text_limit=0)


# ---------------------------------------------------------------------------
# One message on its own, by IDs or by its Teams link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_read_shows_the_message_whole_with_everything_around_it(graph):
    routes, _ = graph
    post = _message(
        ROOT_ID,
        "Alice",
        _memo(800, "Alice"),
        lastEditedDateTime=_recent(2),
        webUrl=f"https://teams.microsoft.com/l/message/{CHANNEL}/{ROOT_ID}?groupId={TEAM}",
        attachments=[
            {
                "id": "f1",
                "contentType": "reference",
                "name": "report.pdf",
                "contentUrl": "https://example.sharepoint.com/sites/security/Shared%20Documents/report.pdf",
            }
        ],
        mentions=[{"id": 0, "mentionText": "Bob", "mentioned": {"user": {"id": "user-bob", "displayName": "Bob"}}}],
        reactions=[
            {"reactionType": "like", "user": {"user": {"id": "user-bob", "displayName": "Bob"}}},
            {"reactionType": "\U0001f44d", "user": {"user": {"id": "user-carol", "displayName": None}}},
        ],
    )
    reply = _message(REPLY_ID, "Bob", "Agreed.", minutes_ago=1, replyToId=ROOT_ID)
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}"] = post
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}/replies"] = {"value": [reply]}

    read = await _call(
        _executor(), "get_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, message_id=ROOT_ID
    )

    content = read.content
    assert content.startswith(f"Teams channel post {ROOT_ID} (team_id: {TEAM}, channel_id: {CHANNEL})\n")
    assert "From: Alice (user)" in content
    assert f"Last edited: {post['lastEditedDateTime']}" in content
    assert f"Link: {post['webUrl']}" in content
    assert "Replies: 1" in content
    assert _memo(800, "Alice") in content
    assert "report.pdf <https://example.sharepoint.com/sites/security/Shared%20Documents/report.pdf>" in content
    assert '[mentions: user "Bob" (user-bob)]' in content
    assert "[reactions: \U0001f44d 2 (Bob, user user-carol)]" in content
    assert (
        f"[replies, newest first: {REPLY_ID} (Bob, {reply['createdDateTime']}) - read one in full with "
        f"get_microsoft_channel_message(team_id={TEAM}, channel_id={CHANNEL}, message_id=REPLY_ID, "
        f"parent_message_id={ROOT_ID}), REPLY_ID being one of these IDs]"
    ) in content


@pytest.mark.asyncio
async def test_a_long_thread_is_counted_as_more_than_what_was_read(graph, monkeypatch):
    from app.integrations.providers.microsoft import teams_channels

    monkeypatch.setattr(teams_channels, "_THREAD_REPLY_PAGES", 1)
    routes, _ = graph
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}"] = _message(ROOT_ID, "Alice", "Kickoff")
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}/replies"] = {
        "value": [_message(REPLY_ID, "Bob", "First")],
        "@odata.nextLink": f"{GRAPH}{CHANNEL_MESSAGES}/{ROOT_ID}/replies?$skiptoken=next",
    }

    read = await _call(
        _executor(), "get_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, message_id=ROOT_ID
    )

    assert "Replies: more than 1" in read.content


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (REPLY_LINK, TeamsMessageLink(CHANNEL, REPLY_ID, TEAM, ROOT_ID)),
        (
            f"https://teams.microsoft.com/l/message/{CHANNEL}/{ROOT_ID}?groupId={TEAM}&parentMessageId={ROOT_ID}",
            TeamsMessageLink(CHANNEL, ROOT_ID, TEAM, ""),
        ),
        (
            "https://teams.cloud.microsoft/l/message/19%3A0123456789abcdef0123456789abcdef%40thread.tacv2"
            f"/{REPLY_ID}?groupId={TEAM}&parentMessageId={ROOT_ID}",
            TeamsMessageLink(CHANNEL, REPLY_ID, TEAM, ROOT_ID),
        ),
        (CHAT_LINK, TeamsMessageLink(CHAT, CHAT_MESSAGE_ID)),
    ],
    ids=["reply", "post", "encoded-new-teams", "chat"],
)
def test_a_teams_link_is_read_for_its_ids(url, expected):
    assert _parse_teams_message_link(url) == (expected, None)


@pytest.mark.parametrize(
    "url",
    [
        f"http://teams.microsoft.com/l/message/{CHANNEL}/{ROOT_ID}",
        f"https://teams.example.com/l/message/{CHANNEL}/{ROOT_ID}",
        f"https://teams.microsoft.com/l/channel/{CHANNEL}/General",
        f"https://teams.microsoft.com/l/message/{CHANNEL}",
        f"https://teams.microsoft.com/l/message/19%3Ax%2F..%2F..%2Fme@thread.tacv2/{ROOT_ID}?groupId={TEAM}",
        f"https://teams.microsoft.com/l/message/{CHANNEL}/{ROOT_ID}?groupId={TEAM}%2Fchannels",
        "",
        None,
    ],
    ids=["http", "other-host", "channel-link", "no-message", "encoded-path-in-id", "path-in-group-id", "empty", "none"],
)
def test_anything_else_is_not_a_teams_message_link(url):
    link, err = _parse_teams_message_link(url)
    assert link is None
    assert err is not None and err.startswith("Error:") and "is not a Teams message link" in err


@pytest.mark.asyncio
async def test_the_read_tools_take_a_link_instead_of_the_ids(graph):
    routes, sent = graph
    reply = _message(REPLY_ID, "Bob", "Thread answer from Bob", replyToId=ROOT_ID)
    chat_message = _message(CHAT_MESSAGE_ID, "Alice", "Chat note from Alice", chatId=CHAT)
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}/replies/{REPLY_ID}"] = reply
    routes[f"/chats/{CHAT}/messages/{CHAT_MESSAGE_ID}"] = chat_message
    executor = _executor()

    channel_read = await _call(executor, "get_microsoft_channel_message", url=REPLY_LINK)
    chat_read = await _call(executor, "get_microsoft_chat_message", url=CHAT_LINK)

    assert sent == [
        f"{GRAPH}{CHANNEL_MESSAGES}/{ROOT_ID}/replies/{REPLY_ID}",
        f"{GRAPH}/chats/{CHAT}/messages/{CHAT_MESSAGE_ID}",
    ]
    assert channel_read.content.startswith(
        f"Teams channel reply {REPLY_ID} in the thread of {ROOT_ID} (team_id: {TEAM}, channel_id: {CHANNEL})"
    )
    assert "Thread answer from Bob" in channel_read.content
    assert "Chat note from Alice" in chat_read.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "error"),
    [
        ("get_microsoft_channel_message", {"url": CHAT_LINK}, "read it with get_microsoft_chat_message"),
        ("get_microsoft_chat_message", {"url": REPLY_LINK}, "read it with get_microsoft_channel_message"),
        (
            "get_microsoft_channel_message",
            {"url": REPLY_LINK, "channel_id": OTHER_CHANNEL},
            "channel_id naming another message",
        ),
        ("get_microsoft_channel_message", {"url": "https://example.com/post/1"}, "is not a Teams message link"),
        ("get_microsoft_channel_message", {}, "team_id is missing or invalid"),
        ("get_microsoft_chat_message", {"chat_id": CHAT}, "message_id is missing or invalid"),
    ],
    ids=["chat-link-to-channel-tool", "channel-link-to-chat-tool", "conflicting-id", "not-a-link", "nothing", "no-id"],
)
async def test_a_read_that_names_no_single_message_sends_nothing(graph, tool, arguments, error):
    _, sent = graph

    result = await _call(_executor(), tool, **arguments)

    assert result.is_error
    assert error in result.content
    assert sent == []


# ---------------------------------------------------------------------------
# The Teams channel Scope(s) holds for the read tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_a_channel_scope_a_link_is_checked_like_the_ids(graph):
    routes, sent = graph
    routes[f"{CHANNEL_MESSAGES}/{ROOT_ID}/replies/{REPLY_ID}"] = _message(REPLY_ID, "Bob", "In scope", replyToId=ROOT_ID)
    scope = teams_channel_scope_from_bindings(
        {TEAMS_CHANNEL_SCOPE_KEY: [{"id": f"{TEAM}:{CHANNEL}", "name": "General", "group": "Security"}]}
    )
    executor = _SafeIntegrationExecutor()
    names = {definition.name for definition in build_tools("token", executor, teams_channel_scope=scope)}

    assert "get_microsoft_channel_message" in names
    assert "get_microsoft_chat_message" not in names

    outside = await _call(executor, "get_microsoft_channel_message", url=OTHER_CHANNEL_LINK)
    laundered = await _call(
        executor, "get_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, url=OTHER_CHANNEL_LINK
    )
    assert outside.is_error and "outside the Scope(s)" in outside.content
    assert laundered.is_error and "naming another message" in laundered.content
    assert sent == []

    inside = await _call(executor, "get_microsoft_channel_message", url=REPLY_LINK)
    assert not inside.is_error, inside.content
    assert sent == [f"{GRAPH}{CHANNEL_MESSAGES}/{ROOT_ID}/replies/{REPLY_ID}"]

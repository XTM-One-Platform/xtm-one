"""Adaptive Cards on the Microsoft Teams message tools.

Graph posts a card as an ``attachments`` entry holding the serialized card and
an ``<attachment id="...">`` tag in the HTML body. The five message tools take
``card`` as data and write both; ``message`` becomes optional. A card holding
an input or any action but ``Action.OpenUrl`` needs a bot, so it is refused
before any request, as is a card that is not one Adaptive Card object or is
too large. A call without ``card`` sends the payload exactly as before.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._cards import ADAPTIVE_CARD_CONTENT_TYPE, MAX_CARD_BYTES
from app.integrations.providers.microsoft._teams import _format_chat_listing
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
TEAM = "6c4d3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
CHANNEL = "19:1b2c3d4e5f60718293a4b5c6d7e8f9a0@thread.tacv2"
SELF_CHAT = "48:notes"
GROUP_CHAT = "19:1f2e3d4c5b6a79880716253443526170@thread.v2"
JANE_ID = "ef1c916a-3135-4417-ba27-8eb7bd084193"
MESSAGE_ID = "1789541727000"

CARD: dict[str, Any] = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Daily digest", "weight": "Bolder"}],
    "actions": [{"type": "Action.OpenUrl", "title": "Open", "url": "https://example.com/digest"}],
}

_ATTACHMENT_TAG = re.compile(r'<attachment id="([0-9a-f]{32})"></attachment>$')


class _Response:
    def __init__(self, payload: dict[str, Any] | None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._payload


class _Graph:
    """A Graph stand-in that echoes posted messages back."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.echo_attachments: str = "kept"  # "kept" | "dropped" | "absent"
        self.refuse_posts: str | None = None
        # The attachments of the message an edit reads first.
        self.message_attachments: list[dict[str, Any]] = []

    def writes(self) -> list[tuple[str, str, dict]]:
        return [call for call in self.calls if call[0] != "get"]

    async def __call__(self, _client, method, url, _provider, _action, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "get":
            if url == f"{GRAPH}/users/jane@filigran.io":
                return _Response({"id": JANE_ID, "displayName": "Jane Doe"}), None
            if "/messages/" in url:
                return _Response({"id": MESSAGE_ID, "attachments": self.message_attachments}), None
            return None, f"Error: Microsoft resource not found ({url})."
        if self.refuse_posts:
            return None, self.refuse_posts
        if method == "patch":
            # Graph answers an edit with 204 No Content.
            return _Response(None, 204), None
        payload = kwargs.get("json") or {}
        echoed: dict[str, Any] = {
            "id": MESSAGE_ID,
            "createdDateTime": "2026-10-08T09:00:00Z",
            "mentions": payload.get("mentions", []),
        }
        if self.echo_attachments == "kept":
            echoed["attachments"] = payload.get("attachments", [])
        elif self.echo_attachments == "dropped":
            echoed["attachments"] = []
        return _Response(echoed), None


@pytest.fixture
def graph(monkeypatch) -> _Graph:
    fake = _Graph()
    monkeypatch.setattr(ms._common, "safe_request", fake)
    return fake


async def _call(tool: str, **arguments):
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


def _only_write(graph: _Graph) -> tuple[str, str, dict]:
    writes = graph.writes()
    assert len(writes) == 1, writes
    method, url, kwargs = writes[0]
    return method, url, kwargs["json"]


def _sent_card(payload: dict) -> tuple[str, dict]:
    """The attachment id written in the body and the card it carries."""
    match = _ATTACHMENT_TAG.search(payload["body"]["content"])
    assert match is not None, payload["body"]["content"]
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["id"] == match.group(1)
    assert attachment["contentType"] == ADAPTIVE_CARD_CONTENT_TYPE
    assert isinstance(attachment["content"], str)
    return match.group(1), json.loads(attachment["content"])


# -- posting a card -----------------------------------------------------------


async def test_a_card_alone_is_posted_to_the_self_chat_as_an_attachment(graph):
    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=CARD)

    assert not result.is_error, result.content
    method, url, payload = _only_write(graph)
    assert (method, url) == ("post", f"{GRAPH}/me/chats/{SELF_CHAT}/messages")
    attachment_id, card = _sent_card(payload)
    assert payload["body"] == {
        "contentType": "html",
        "content": f'<attachment id="{attachment_id}"></attachment>',
    }
    # A chat caps a card at the bubble width: no width hint is added there.
    assert card == CARD
    assert set(payload) == {"body", "attachments"}
    assert result.content == (
        f"Chat message sent. Message ID: {MESSAGE_ID}. Sent at: 2026-10-08T09:00:00Z. Adaptive Card attached."
    )


async def test_the_text_shows_above_the_card(graph):
    result = await _call("send_microsoft_chat_message", chat_id=GROUP_CHAT, message="Here is **today**:", card=CARD)

    assert not result.is_error, result.content
    _, _, payload = _only_write(graph)
    attachment_id, _ = _sent_card(payload)
    assert payload["body"]["content"] == (
        f'<p>Here is <strong>today</strong>:</p><attachment id="{attachment_id}"></attachment>'
    )


async def test_a_card_given_as_its_json_string_is_read(graph):
    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=json.dumps(CARD))

    assert not result.is_error, result.content
    _, _, payload = _only_write(graph)
    assert _sent_card(payload)[1] == CARD


async def test_a_card_and_mentions_are_sent_together(graph):
    result = await _call(
        "send_microsoft_chat_message",
        chat_id=GROUP_CHAT,
        card=CARD,
        mentions=[{"type": "user", "id": "jane@filigran.io"}],
    )

    assert not result.is_error, result.content
    _, _, payload = _only_write(graph)
    attachment_id, _ = _sent_card(payload)
    assert payload["body"]["content"] == (
        f'<p><at id="0">Jane Doe</at></p><attachment id="{attachment_id}"></attachment>'
    )
    assert [entry["mentioned"]["user"]["id"] for entry in payload["mentions"]] == [JANE_ID]
    assert result.content.endswith('Mentions created: user "Jane Doe". Adaptive Card attached.')


async def test_each_post_gets_its_own_attachment_id(graph):
    await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=CARD)
    await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=CARD)

    ids = [call[2]["json"]["attachments"][0]["id"] for call in graph.writes()]
    assert len(ids) == 2
    assert ids[0] != ids[1]


_CHANNEL_TOOLS = [
    (
        "send_microsoft_channel_message",
        {"team_id": TEAM, "channel_id": CHANNEL},
        "post",
        f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/messages",
    ),
    (
        "reply_microsoft_channel_message",
        {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1700000000300"},
        "post",
        f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/messages/1700000000300/replies",
    ),
    (
        "update_microsoft_channel_message",
        {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1700000000300"},
        "patch",
        f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/messages/1700000000300",
    ),
]

# The edit tools: Graph answers with 204 and no message, so the card is only "sent".
_SENT_ONLY = {"update_microsoft_channel_message"}


@pytest.mark.parametrize(("tool", "arguments", "method", "url"), _CHANNEL_TOOLS)
async def test_a_channel_card_renders_full_width(graph, tool, arguments, method, url):
    result = await _call(tool, card=CARD, **arguments)

    assert not result.is_error, result.content
    sent_method, sent_url, payload = _only_write(graph)
    assert (sent_method, sent_url) == (method, url)
    assert _sent_card(payload)[1] == {**CARD, "msteams": {"width": "Full"}}
    assert result.content.endswith(" Adaptive Card sent." if tool in _SENT_ONLY else " Adaptive Card attached.")


async def test_a_channel_card_keeps_its_own_width(graph):
    card = {**CARD, "msteams": {"width": "Auto"}}

    await _call("send_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, card=card)

    _, _, payload = _only_write(graph)
    assert _sent_card(payload)[1] == card


async def test_editing_a_chat_message_replaces_its_card(graph):
    result = await _call("update_microsoft_chat_message", chat_id=GROUP_CHAT, message_id=MESSAGE_ID, card=CARD)

    assert not result.is_error, result.content
    method, url, payload = _only_write(graph)
    assert (method, url) == ("patch", f"{GRAPH}/chats/{GROUP_CHAT}/messages/{MESSAGE_ID}")
    assert _sent_card(payload)[1] == CARD
    assert result.content == f"Chat message updated. Message ID: {MESSAGE_ID}. Last edited: . Adaptive Card sent."


async def test_editing_a_chat_message_sends_its_card_and_mentions_together(graph):
    result = await _call(
        "update_microsoft_chat_message",
        chat_id=GROUP_CHAT,
        message_id=MESSAGE_ID,
        message="Updated:",
        card=CARD,
        mentions=[{"type": "user", "id": "jane@filigran.io"}],
    )

    assert not result.is_error, result.content
    method, url, payload = _only_write(graph)
    assert (method, url) == ("patch", f"{GRAPH}/chats/{GROUP_CHAT}/messages/{MESSAGE_ID}")
    attachment_id, card = _sent_card(payload)
    assert card == CARD
    assert payload["body"]["content"] == (
        f'<p><at id="0">Jane Doe</at></p><br/><p>Updated:</p><attachment id="{attachment_id}"></attachment>'
    )
    assert [entry["mentioned"]["user"]["id"] for entry in payload["mentions"]] == [JANE_ID]
    assert set(payload) == {"body", "mentions", "attachments"}
    assert result.content.endswith(" Adaptive Card sent.")


_REPLY = {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1700000000300"}


async def test_a_reply_reads_its_card_back_from_the_returned_reply(graph):
    result = await _call("reply_microsoft_channel_message", message="See below", card=CARD, **_REPLY)

    assert not result.is_error, result.content
    assert result.content == (
        f"Reply sent in channel thread. Reply ID: {MESSAGE_ID}. Sent at: 2026-10-08T09:00:00Z. Adaptive Card attached."
    )


async def test_a_reply_whose_card_teams_dropped_is_a_warning(graph):
    graph.echo_attachments = "dropped"

    result = await _call("reply_microsoft_channel_message", card=CARD, **_REPLY)

    assert result.content.endswith(" Warning: Teams did not keep the Adaptive Card; the message shows without it.")


async def test_a_reply_whose_card_teams_did_not_return_names_the_channel_listing(graph):
    graph.echo_attachments = "absent"

    result = await _call("reply_microsoft_channel_message", card=CARD, **_REPLY)

    assert result.content.endswith(
        " Adaptive Card sent; Teams did not return it, read the message back with "
        "list_microsoft_channel_messages to check."
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("update_microsoft_chat_message", {"chat_id": GROUP_CHAT, "message_id": MESSAGE_ID}),
        ("update_microsoft_channel_message", {"team_id": TEAM, "channel_id": CHANNEL, "message_id": MESSAGE_ID}),
    ],
)
async def test_an_edit_answered_with_204_is_a_success(graph, tool, arguments):
    result = await _call(tool, message="Fixed", **arguments)

    assert not result.is_error, result.content
    assert result.content.endswith(f"Message ID: {MESSAGE_ID}. Last edited: .")


async def test_a_message_without_card_is_sent_as_before(graph):
    result = await _call("send_microsoft_chat_message", chat_id=GROUP_CHAT, message="Hello")

    assert not result.is_error, result.content
    _, _, payload = _only_write(graph)
    assert payload == {"body": {"contentType": "html", "content": "<p>Hello</p>"}}
    assert "Adaptive Card" not in result.content


# -- what Teams kept ----------------------------------------------------------


async def test_a_card_teams_did_not_return_is_reported(graph):
    graph.echo_attachments = "absent"

    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=CARD)

    assert result.content.endswith(
        " Adaptive Card sent; Teams did not return it, read the message back with "
        "list_microsoft_chat_messages to check."
    )


async def test_a_card_teams_dropped_is_a_warning(graph):
    graph.echo_attachments = "dropped"

    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=CARD)

    assert result.content.endswith(" Warning: Teams did not keep the Adaptive Card; the message shows without it.")


async def test_a_graph_refusal_is_returned_and_never_retried_without_the_card(graph):
    graph.refuse_posts = "Error: Microsoft Graph returned 400 (BadRequest)."

    result = await _call("send_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, message="Hi", card=CARD)

    assert result.content == "Error: Microsoft Graph returned 400 (BadRequest)."
    assert len(graph.writes()) == 1


# -- refusals before any request ----------------------------------------------


def _with_action(action: dict) -> dict:
    return {**CARD, "actions": [action]}


_REFUSED_CARDS = [
    pytest.param(_with_action({"type": "Action.Submit", "title": "Go"}), "Action.Submit", id="submit"),
    pytest.param(_with_action({"type": "Action.Execute", "verb": "x"}), "Action.Execute", id="execute"),
    pytest.param(
        _with_action({"type": "Action.ShowCard", "card": {"type": "AdaptiveCard", "body": []}}),
        "Action.ShowCard",
        id="show-card",
    ),
    pytest.param(
        _with_action({"type": "Action.ToggleVisibility", "targetElements": ["a"]}),
        "Action.ToggleVisibility",
        id="toggle",
    ),
    pytest.param(
        {**CARD, "body": [{"type": "Container", "items": [{"type": "Input.Text", "id": "q"}]}]},
        "Input.Text",
        id="nested-input",
    ),
    pytest.param(
        {**CARD, "body": [{"type": "Container", "items": [], "selectAction": {"type": "Action.Submit"}}]},
        "Action.Submit",
        id="select-action",
    ),
]


_ALL_CARD_TOOLS = [
    ("send_microsoft_chat_message", {"chat_id": SELF_CHAT}),
    ("update_microsoft_chat_message", {"chat_id": GROUP_CHAT, "message_id": MESSAGE_ID}),
    *[(tool, arguments) for tool, arguments, _method, _url in _CHANNEL_TOOLS],
]


@pytest.mark.parametrize(("tool", "arguments"), _ALL_CARD_TOOLS)
async def test_every_message_tool_refuses_a_bot_card_before_any_request(graph, tool, arguments):
    card = _with_action({"type": "Action.Submit", "title": "Go"})

    result = await _call(tool, card=card, mentions=[{"type": "user", "id": "jane@filigran.io"}], **arguments)

    assert graph.calls == []
    assert result.content.startswith("Error: the card holds Action.Submit, which Teams runs only for a bot;")


@pytest.mark.parametrize(("card", "named"), _REFUSED_CARDS)
async def test_a_card_that_needs_a_bot_is_refused_before_any_request(graph, card, named):
    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=card)

    assert graph.calls == []
    assert result.content.startswith(f"Error: the card holds {named}, which Teams runs only for a bot;")
    assert "send_teams_bot_chat_card" in result.content
    assert result.content.endswith("Nothing was posted.")


_INVALID_CARDS = [
    pytest.param("{not json", "Error: card is not valid JSON. Nothing was posted.", id="bad-json"),
    pytest.param([CARD], "Error: card must be one Adaptive Card object. Nothing was posted.", id="list"),
    pytest.param("[]", "Error: card must be one Adaptive Card object. Nothing was posted.", id="json-list"),
    pytest.param("null", "Error: card must be one Adaptive Card object. Nothing was posted.", id="json-null"),
    pytest.param("42", "Error: card must be one Adaptive Card object. Nothing was posted.", id="json-number"),
    pytest.param('"AdaptiveCard"', "Error: card must be one Adaptive Card object. Nothing was posted.", id="json-string"),
    pytest.param("true", "Error: card must be one Adaptive Card object. Nothing was posted.", id="json-bool"),
    pytest.param(42, "Error: card must be one Adaptive Card object. Nothing was posted.", id="number"),
    pytest.param(
        {"type": "HeroCard", "title": "x"},
        "Error: card has type 'HeroCard'; an Adaptive Card has \"type\": \"AdaptiveCard\". Nothing was posted.",
        id="hero-card",
    ),
    pytest.param(
        {"body": []},
        "Error: card has type 'None'; an Adaptive Card has \"type\": \"AdaptiveCard\". Nothing was posted.",
        id="no-type",
    ),
]


@pytest.mark.parametrize(("card", "error"), _INVALID_CARDS)
async def test_an_invalid_card_is_refused_before_any_request(graph, card, error):
    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, message="Hi", card=card)

    assert graph.calls == []
    assert result.content == error


_TOO_LARGE = re.compile(
    rf"Error: the message with its card is (\d+) bytes; a Teams message holds at most {MAX_CARD_BYTES}\. "
    r"Nothing was posted\."
)


# The body of a card posted alone: the attachment tag, its id 32 hex digits.
_TAG_BYTES = len('<attachment id="' + "0" * 32 + '"></attachment>')


async def test_an_oversized_card_is_refused_before_any_request(graph):
    card = {**CARD, "body": [{"type": "TextBlock", "text": "x" * MAX_CARD_BYTES}]}

    result = await _call("send_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, card=card)

    assert graph.calls == []
    match = _TOO_LARGE.fullmatch(result.content)
    assert match is not None, result.content
    # The size counted is the body plus the serialized card, as Teams stores them.
    sent = {**card, "msteams": {"width": "Full"}}
    assert int(match.group(1)) == _TAG_BYTES + len(json.dumps(sent, ensure_ascii=False).encode())


def _quoted_card(quotes: int) -> dict:
    # Each quote is 2 bytes in the serialized card and 4 once that string is escaped again.
    return {**CARD, "body": [{"type": "TextBlock", "text": '"' * quotes}]}


async def test_a_card_is_measured_once_not_escaped_twice(graph):
    # About 20 KB as Teams stores it, about 40 KB as a JSON string inside the request.
    card = _quoted_card(10_000)
    stored = _TAG_BYTES + len(json.dumps(card, ensure_ascii=False).encode())
    assert stored < MAX_CARD_BYTES < len(json.dumps(json.dumps(card, ensure_ascii=False)).encode())

    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=card)

    assert not result.is_error, result.content
    _, _, payload = _only_write(graph)
    assert _sent_card(payload)[1] == card


async def test_a_card_just_over_the_limit_once_measured_is_refused(graph):
    overhead = _TAG_BYTES + len(json.dumps(_quoted_card(0), ensure_ascii=False).encode())
    # Each quote adds 2 bytes: one quote more than fits.
    card = _quoted_card((MAX_CARD_BYTES - overhead) // 2 + 1)

    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=card)

    assert graph.calls == []
    match = _TOO_LARGE.fullmatch(result.content)
    assert match is not None, result.content
    assert int(match.group(1)) == _TAG_BYTES + len(json.dumps(card, ensure_ascii=False).encode())
    assert int(match.group(1)) > MAX_CARD_BYTES


async def test_a_non_ascii_card_is_measured_in_utf8_bytes(graph):
    # 10,000 characters, 30,000 bytes.
    card = {**CARD, "body": [{"type": "TextBlock", "text": "€" * 10_000}]}

    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, card=card)

    assert graph.calls == []
    match = _TOO_LARGE.fullmatch(result.content)
    assert match is not None, result.content
    assert int(match.group(1)) == _TAG_BYTES + len(json.dumps(card, ensure_ascii=False).encode())


async def test_the_size_limit_counts_the_text_next_to_the_card(graph):
    # The card alone fits; the card and the text do not.
    card = {**CARD, "body": [{"type": "TextBlock", "text": "x" * (MAX_CARD_BYTES - 2048)}]}

    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, message="y" * 4096, card=card)

    assert graph.calls == []
    match = _TOO_LARGE.fullmatch(result.content)
    assert match is not None, result.content
    assert int(match.group(1)) > MAX_CARD_BYTES


@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_no_text_no_card_and_no_mention_posts_nothing(graph, blank):
    arguments: dict[str, Any] = {"chat_id": SELF_CHAT}
    if blank is not None:
        arguments["message"] = blank

    result = await _call("send_microsoft_chat_message", **arguments)

    assert graph.calls == []
    assert result.content == "Error: pass a message, a card, or both. Nothing was posted."


@pytest.mark.parametrize("empty", ["", {}, "{}", None])
async def test_an_empty_card_argument_is_no_card(graph, empty):
    result = await _call("send_microsoft_chat_message", chat_id=SELF_CHAT, message="Hi", card=empty)

    assert not result.is_error, result.content
    _, _, payload = _only_write(graph)
    assert "attachments" not in payload


# -- schemas and read-back ----------------------------------------------------


_CARD_TOOLS = {
    "send_microsoft_chat_message": ["chat_id"],
    "update_microsoft_chat_message": ["chat_id", "message_id"],
    "send_microsoft_channel_message": ["team_id", "channel_id"],
    "reply_microsoft_channel_message": ["team_id", "channel_id", "message_id"],
    "update_microsoft_channel_message": ["team_id", "channel_id", "message_id"],
}


def test_the_message_tools_take_a_card_and_an_optional_message():
    definitions = {tool.name: tool for tool in build_tools(token="dummy", executor=CallableToolExecutor())}

    for name, required in _CARD_TOOLS.items():
        schema = definitions[name].input_schema
        assert schema["properties"]["card"]["type"] == "object", name
        assert schema["required"] == required, name


def test_a_posted_card_reads_back_as_its_text():
    message = {
        "id": MESSAGE_ID,
        "createdDateTime": "2026-10-08T09:00:00Z",
        "from": {"user": {"id": JANE_ID, "displayName": "Jane Doe"}},
        "body": {"contentType": "html", "content": '<attachment id="a1"></attachment>'},
        "attachments": [{"id": "a1", "contentType": ADAPTIVE_CARD_CONTENT_TYPE, "content": json.dumps(CARD)}],
    }

    ((line,),) = _format_chat_listing([(GROUP_CHAT, [message])])

    assert "Daily digest" in line

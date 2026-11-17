"""Teams mentions on the Microsoft message tools.

A Teams mention is an ``<at id="N">`` tag in the HTML body AND the matching
entry of the Graph ``mentions`` array. The tools take ``mentions`` as data,
read the names left out from Graph, write the tags after the Markdown
conversion (the sanitizer would strip them), refuse a channel or team mention
that names another channel or team, return a Graph refusal as it is, and
report what Teams kept; ``list_microsoft_channel_messages`` reads the mentions
back. A call without ``mentions`` sends the body alone, as before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.teams_scope import (
    TEAMS_CHANNEL_SCOPE_KEY,
    teams_channel_scope_from_bindings,
)
from app.services.teams_format import MentionPlacementError, build_graph_chat_body
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
TEAM = "6c4d3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
CHANNEL = "19:1b2c3d4e5f60718293a4b5c6d7e8f9a0@thread.tacv2"
OTHER_CHANNEL = "19:General0@thread.tacv2"
CHAT = "19:1f2e3d4c5b6a79880716253443526170@thread.v2"
ONE_ON_ONE = "19:ab12cd34-0000-4000-8000-000000000001_ef1c916a-3135-4417-ba27-8eb7bd084193@unq.gbl.spaces"
JANE_ID = "ef1c916a-3135-4417-ba27-8eb7bd084193"
MESSAGES = f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/messages"


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


class _Graph:
    """A Graph stand-in: the names a mention reads, and posts echoed back."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.keep_mentions = True
        self.refuse_posts: str | None = None
        self.listing: list[dict] = []
        self.gets: dict[str, dict] = {
            f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}": {"id": CHANNEL, "displayName": "Testing XTM One"},
            f"{GRAPH}/teams/{TEAM}": {"id": TEAM, "displayName": "Executive Management"},
            f"{GRAPH}/users/jane@filigran.io": {"id": JANE_ID, "displayName": "Jane Doe"},
            f"{GRAPH}/teams/{TEAM}/tags": {"value": [{"id": "tag-1", "displayName": "Leads"}]},
            f"{GRAPH}/teams/{TEAM}/tags/tag-1": {"id": "tag-1", "displayName": "Leads"},
            f"{GRAPH}/chats/{CHAT}": {"id": CHAT, "chatType": "group"},
            f"{GRAPH}/chats/{ONE_ON_ONE}": {"id": ONE_ON_ONE, "chatType": "oneOnOne"},
        }

    def writes(self) -> list[tuple[str, str, dict]]:
        return [call for call in self.calls if call[0] != "get"]

    async def __call__(self, _client, method, url, _provider, _action, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "get":
            if url == MESSAGES:
                return _Response({"value": self.listing}), None
            if url.endswith("/replies"):
                return _Response({"value": []}), None
            if url in self.gets:
                return _Response(self.gets[url]), None
            return None, f"Error: Microsoft resource not found ({url})."
        if self.refuse_posts:
            return None, self.refuse_posts
        payload = kwargs.get("json") or {}
        echoed = payload.get("mentions", []) if self.keep_mentions else []
        return _Response({"id": "1700000000300", "createdDateTime": "2026-10-06T09:00:00Z", "mentions": echoed}), None


@pytest.fixture
def graph(monkeypatch) -> _Graph:
    fake = _Graph()
    monkeypatch.setattr(ms._common, "safe_request", fake)
    return fake


async def _call(tool: str, executor: CallableToolExecutor | None = None, **arguments):
    if executor is None:
        executor = CallableToolExecutor()
        build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


async def _post(graph: _Graph, mentions: Any, message: str = "Welcome to the EMT channel.", **extra):
    result = await _call(
        "send_microsoft_channel_message",
        team_id=TEAM,
        channel_id=CHANNEL,
        message=message,
        mentions=mentions,
        **extra,
    )
    posts = graph.writes()
    return result, (posts[0][2]["json"] if posts else None)


# -- channel and team mentions ------------------------------------------------


async def test_a_channel_mention_is_sent_with_its_tag_and_the_channel_name(graph):
    result, payload = await _post(graph, [{"type": "channel"}])

    assert not result.is_error, result.content
    assert payload["body"]["content"].startswith('<p><at id="0">Testing XTM One</at></p>')
    assert payload["mentions"] == [
        {
            "id": 0,
            "mentionText": "Testing XTM One",
            "mentioned": {
                "conversation": {
                    "id": CHANNEL,
                    "displayName": "Testing XTM One",
                    "conversationIdentityType": "channel",
                }
            },
        }
    ]
    assert 'Mentions created: channel "Testing XTM One".' in result.content


async def test_a_team_mention_names_the_team_posted_in(graph):
    result, payload = await _post(graph, [{"type": "team"}])

    assert not result.is_error, result.content
    assert payload["mentions"][0]["mentioned"] == {
        "conversation": {
            "id": TEAM,
            "displayName": "Executive Management",
            "conversationIdentityType": "team",
        }
    }
    assert '<at id="0">Executive Management</at>' in payload["body"]["content"]


async def test_a_given_display_name_skips_the_lookup(graph):
    result, payload = await _post(graph, [{"type": "channel", "display_name": "EMT"}])

    assert not result.is_error, result.content
    assert [call for call in graph.calls if call[0] == "get"] == []
    assert payload["mentions"][0]["mentionText"] == "EMT"


@pytest.mark.parametrize("kind, other", [("channel", OTHER_CHANNEL), ("team", "00000000-0000-0000-0000-000000000001")])
async def test_a_mention_of_another_channel_or_team_is_refused_before_any_request(graph, kind, other):
    result, payload = await _post(graph, [{"type": kind, "id": other}])

    assert result.is_error
    assert f"names another {kind}" in result.content
    assert graph.calls == []
    assert payload is None


# -- user and tag mentions ----------------------------------------------------


async def test_a_user_mention_by_email_is_sent_with_the_entra_object_id(graph):
    result, payload = await _post(graph, [{"type": "user", "id": "jane@filigran.io"}])

    assert not result.is_error, result.content
    assert payload["mentions"][0]["mentioned"] == {
        "user": {"id": JANE_ID, "displayName": "Jane Doe", "userIdentityType": "aadUser"}
    }


async def test_a_user_mention_with_an_object_id_and_a_name_needs_no_lookup(graph):
    result, payload = await _post(graph, [{"type": "user", "id": JANE_ID, "display_name": "Jane"}])

    assert not result.is_error, result.content
    assert [call for call in graph.calls if call[0] == "get"] == []
    assert payload["mentions"][0]["mentioned"]["user"]["id"] == JANE_ID


async def test_a_user_mention_needs_an_id(graph):
    result, _ = await _post(graph, [{"type": "user", "display_name": "Jane"}])

    assert result.is_error
    assert "needs an id" in result.content
    assert graph.calls == []


@pytest.mark.parametrize("tag", [{"type": "tag", "display_name": "leads"}, {"type": "tag", "id": "tag-1"}])
async def test_a_tag_mention_is_resolved_from_the_team_tags(graph, tag):
    result, payload = await _post(graph, [tag])

    assert not result.is_error, result.content
    assert payload["mentions"][0]["mentioned"] == {
        "tag": {"@odata.type": "#microsoft.graph.teamworkTagIdentity", "id": "tag-1", "displayName": "Leads"}
    }


async def test_an_unknown_tag_name_lists_the_team_tags(graph):
    result, _ = await _post(graph, [{"type": "tag", "display_name": "Board"}])

    assert result.is_error
    assert "no tag named 'Board'" in result.content
    assert "Leads" in result.content
    assert graph.writes() == []


async def test_a_failed_lookup_says_how_to_skip_it(graph):
    del graph.gets[f"{GRAPH}/teams/{TEAM}"]

    result, _ = await _post(graph, [{"type": "team"}])

    assert result.is_error
    assert "pass display_name to skip this lookup" in result.content
    assert graph.writes() == []


# -- placement, refusal, grounding --------------------------------------------


async def test_a_mention_goes_where_the_message_places_it(graph):
    result, payload = await _post(
        graph,
        [{"type": "user", "id": JANE_ID, "display_name": "Jane Doe"}, {"type": "channel"}],
        message='**Agenda** for <at id="1"></at>, owner <at id="0">@jane</at>.',
    )

    assert not result.is_error, result.content
    content = payload["body"]["content"]
    assert content == (
        '<p><strong>Agenda</strong> for <at id="1">Testing XTM One</at>, owner <at id="0">Jane Doe</at>.</p>'
    )


async def test_a_marker_naming_no_mention_posts_nothing(graph):
    result, payload = await _post(graph, [{"type": "channel"}], message='Hi <at id="3"></at>')

    assert result.is_error
    assert '<at id="3">' in result.content
    assert payload is None


async def test_mentions_sent_as_a_json_string_are_read(graph):
    result, payload = await _post(graph, json.dumps([{"type": "channel"}]))

    assert not result.is_error, result.content
    assert payload["mentions"][0]["mentioned"]["conversation"]["id"] == CHANNEL


@pytest.mark.parametrize("bad", ["not json", 42, [{"type": "everyone"}], ["channel"]])
async def test_malformed_mentions_post_nothing(graph, bad):
    result, payload = await _post(graph, bad)

    assert result.is_error
    assert payload is None


async def test_a_graph_refusal_is_returned_and_nothing_is_posted_as_plain_text(graph):
    graph.refuse_posts = (
        "Error: Microsoft request failed while trying to send channel message (HTTP 400). Detail: mentions are disabled"
    )

    result, _ = await _post(graph, [{"type": "team"}])

    assert result.is_error
    assert "mentions are disabled" in result.content
    assert "A team's settings can turn off channel and team mentions." in result.content
    assert len(graph.writes()) == 1


async def test_a_mention_teams_did_not_keep_is_reported(graph):
    graph.keep_mentions = False

    result, _ = await _post(graph, [{"type": "channel"}])

    assert "Warning: Teams did not keep 1 of 1 mention(s)" in result.content
    assert "notify nobody" in result.content


async def test_a_call_without_mentions_sends_the_body_alone(graph):
    message = 'Hello <at id="0">Testing XTM One</at>, see https://example.com'

    result, payload = await _post(graph, None, message=message)

    assert not result.is_error, result.content
    assert payload == {"body": build_graph_chat_body(message)}
    assert "<at" not in payload["body"]["content"]
    assert "Mentions" not in result.content


async def test_a_reply_and_an_edit_carry_their_mentions(graph):
    reply = await _call(
        "reply_microsoft_channel_message",
        team_id=TEAM,
        channel_id=CHANNEL,
        message_id="1700000000300",
        message="Recap attached.",
        mentions=[{"type": "channel"}],
    )
    edit = await _call(
        "update_microsoft_channel_message",
        team_id=TEAM,
        channel_id=CHANNEL,
        message_id="1700000000300",
        message="Agenda (updated).",
        mentions=[{"type": "team"}],
    )

    (_, reply_url, reply_kw), (edit_method, _, edit_kw) = graph.writes()
    assert reply_url.endswith("/messages/1700000000300/replies")
    assert reply_kw["json"]["mentions"][0]["mentioned"]["conversation"]["conversationIdentityType"] == "channel"
    assert 'Mentions created: channel "Testing XTM One".' in reply.content
    assert edit_method == "patch"
    assert edit_kw["json"]["mentions"][0]["mentioned"]["conversation"]["conversationIdentityType"] == "team"
    assert 'Mentions sent: team "Executive Management".' in edit.content


async def test_a_scoped_run_mentions_its_channel(graph):
    scope = teams_channel_scope_from_bindings(
        {TEAMS_CHANNEL_SCOPE_KEY: [{"id": f"{TEAM}:{CHANNEL}", "name": "Testing XTM One", "group": "EMT"}]}
    )
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor, teams_channel_scope=scope)

    result = await _call(
        "send_microsoft_channel_message",
        executor,
        team_id=TEAM,
        channel_id=CHANNEL,
        message="Hello",
        mentions=[{"type": "channel"}],
    )

    assert not result.is_error, result.content
    assert graph.writes()[0][2]["json"]["mentions"][0]["mentionText"] == "Testing XTM One"


# -- chats --------------------------------------------------------------------


async def test_a_chat_message_mentions_people(graph):
    result = await _call(
        "send_microsoft_chat_message",
        chat_id=CHAT,
        message="Can you review?",
        mentions=[{"type": "user", "id": "jane@filigran.io"}],
    )

    assert not result.is_error, result.content
    ((_, url, kwargs),) = graph.writes()
    assert url == f"{GRAPH}/me/chats/{CHAT}/messages"
    assert kwargs["json"]["mentions"][0]["mentioned"]["user"]["id"] == JANE_ID
    assert kwargs["json"]["body"]["content"].startswith('<p><at id="0">Jane Doe</at></p>')
    assert 'Mentions created: user "Jane Doe".' in result.content


async def test_a_chat_message_mentions_everyone_in_the_chat(graph):
    result = await _call(
        "send_microsoft_chat_message",
        chat_id=CHAT,
        message='<at id="0"></at> the deck is out.',
        mentions=[{"type": "chat"}],
    )

    assert not result.is_error, result.content
    ((_, url, kwargs),) = graph.writes()
    assert url == f"{GRAPH}/me/chats/{CHAT}/messages"
    assert kwargs["json"]["mentions"] == [
        {
            "id": 0,
            "mentionText": "Everyone",
            "mentioned": {"conversation": {"id": CHAT, "displayName": "Everyone", "conversationIdentityType": "chat"}},
        }
    ]
    assert '<at id="0">Everyone</at> the deck is out.' in kwargs["json"]["body"]["content"]
    assert 'Mentions created: chat "Everyone".' in result.content


async def test_a_chat_mention_takes_a_display_name_and_people_beside_it(graph):
    result = await _call(
        "send_microsoft_chat_message",
        chat_id=CHAT,
        message="Hi",
        mentions=[{"type": "chat", "display_name": "All Hands"}, {"type": "user", "id": "jane@filigran.io"}],
    )

    assert not result.is_error, result.content
    mentions = graph.writes()[0][2]["json"]["mentions"]
    assert mentions[0]["mentionText"] == "All Hands"
    assert mentions[1]["mentioned"]["user"]["id"] == JANE_ID


async def test_a_chat_mention_in_a_one_on_one_chat_is_refused(graph):
    result = await _call("send_microsoft_chat_message", chat_id=ONE_ON_ONE, message="Hi", mentions=[{"type": "chat"}])

    assert result.is_error
    assert "1:1 chat" in result.content
    assert graph.writes() == []


async def test_a_chat_mention_of_another_chat_is_refused_before_any_request(graph):
    result = await _call(
        "send_microsoft_chat_message",
        chat_id=CHAT,
        message="Hi",
        mentions=[{"type": "chat", "id": "19:other@thread.v2"}],
    )

    assert result.is_error
    assert "names another chat" in result.content
    assert graph.calls == []


async def test_a_graph_refusal_of_a_chat_mention_is_returned_as_is(graph):
    graph.refuse_posts = "Error: Microsoft returned 400 Bad Request: mention not supported."

    result = await _call("send_microsoft_chat_message", chat_id=CHAT, message="Hi", mentions=[{"type": "chat"}])

    assert result.is_error
    assert "mention not supported" in result.content


async def test_an_edit_carries_a_chat_mention(graph):
    result = await _call(
        "update_microsoft_chat_message",
        chat_id=CHAT,
        message_id="1700000000300",
        message="Updated",
        mentions=[{"type": "chat"}],
    )

    assert not result.is_error, result.content
    ((method, _, kwargs),) = graph.writes()
    assert method == "patch"
    assert kwargs["json"]["mentions"][0]["mentioned"]["conversation"]["conversationIdentityType"] == "chat"


async def test_the_chat_listing_reads_back_a_chat_mention():
    from app.integrations.providers.microsoft._teams import _format_message_mentions

    suffix = _format_message_mentions(
        {
            "mentions": [
                {
                    "id": 0,
                    "mentionText": "Everyone",
                    "mentioned": {
                        "conversation": {"id": CHAT, "displayName": "Everyone", "conversationIdentityType": "chat"}
                    },
                }
            ]
        }
    )

    assert suffix == f'\n[mentions: chat "Everyone" ({CHAT})]'


@pytest.mark.parametrize("kind", ["channel", "team", "tag"])
async def test_a_chat_message_mentions_nothing_but_people(graph, kind):
    result = await _call(
        "send_microsoft_chat_message", chat_id=CHAT, message="Hi", mentions=[{"type": kind, "id": "x"}]
    )

    assert result.is_error
    assert "this tool mentions: user" in result.content
    assert graph.calls == []


def test_every_message_tool_declares_mentions():
    executor = CallableToolExecutor()
    definitions = {definition.name: definition for definition in build_tools(token="dummy", executor=executor)}
    for name in (
        "send_microsoft_channel_message",
        "reply_microsoft_channel_message",
        "update_microsoft_channel_message",
    ):
        schema = definitions[name].input_schema["properties"]["mentions"]
        assert schema["items"]["properties"]["type"]["enum"] == ["user", "channel", "team", "tag"]
        assert "mentions" not in definitions[name].input_schema["required"]
    for name in ("send_microsoft_chat_message", "update_microsoft_chat_message"):
        schema = definitions[name].input_schema["properties"]["mentions"]
        assert schema["items"]["properties"]["type"]["enum"] == ["user", "chat"]


# -- read-back ----------------------------------------------------------------


async def test_the_listing_reads_back_each_message_mentions(graph):
    graph.listing = [
        {
            "id": "1700000000300",
            "createdDateTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "from": {"user": {"displayName": "Executive Assistant"}},
            "body": {"content": '<p><at id="0">Testing XTM One</at> <at id="1">Jane Doe</at> agenda</p>'},
            "mentions": [
                {
                    "id": 0,
                    "mentionText": "Testing XTM One",
                    "mentioned": {
                        "conversation": {
                            "id": CHANNEL,
                            "displayName": "Testing XTM One",
                            "conversationIdentityType": "channel",
                        }
                    },
                },
                {"id": 1, "mentionText": "Jane Doe", "mentioned": {"user": {"id": JANE_ID, "displayName": "Jane Doe"}}},
                {"id": 2, "mentionText": "Leads", "mentioned": {"tag": {"id": "tag-1", "displayName": "Leads"}}},
            ],
        }
    ]

    result = await _call("list_microsoft_channel_messages", team_id=TEAM, channel_id=CHANNEL)

    assert not result.is_error, result.content
    assert (
        f'[mentions: channel "Testing XTM One" ({CHANNEL}); user "Jane Doe" ({JANE_ID}); tag "Leads" (tag-1)]'
    ) in result.content
    assert "(Message ID: 1700000000300)" in result.content


# -- the body builder ---------------------------------------------------------


def test_a_mention_name_is_escaped_in_its_tag():
    content = build_graph_chat_body("Hi", mention_texts=["R&D <ops>"])["content"]

    assert content.startswith('<p><at id="0">R&amp;D &lt;ops&gt;</at></p>')


def test_an_unplaced_mention_opens_a_markdown_report_on_its_own_line():
    content = build_graph_chat_body("## Agenda\n\n- budget", mention_texts=["EMT"])["content"]

    assert content.startswith('<p><at id="0">EMT</at></p><br/><h2>Agenda</h2>')


def test_an_unplaced_mention_opens_an_html_body_in_a_paragraph():
    content = build_graph_chat_body("<p>Hello</p><ul><li>a</li></ul>", mention_texts=["EMT"])["content"]

    assert content == '<p><at id="0">EMT</at></p><p>Hello</p><ul><li>a</li></ul>'


def test_a_mention_placed_twice_or_with_no_name_is_refused():
    with pytest.raises(MentionPlacementError, match="more than once"):
        build_graph_chat_body('<at id="0"></at> and <at id="0"/>', mention_texts=["EMT"])
    with pytest.raises(MentionPlacementError, match="needs a name"):
        build_graph_chat_body("Hi", mention_texts=[" "])


def test_a_mention_in_a_place_teams_does_not_show_is_refused():
    with pytest.raises(MentionPlacementError, match="sit where Teams shows no text"):
        build_graph_chat_body('<p>Hi</p><!-- <at id="0"></at> -->', mention_texts=["EMT"])

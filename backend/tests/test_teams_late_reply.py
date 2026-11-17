"""A Teams chat reply that lands after a newer message quotes the message it answers.

Two inbound messages A then B, the turn for A still running when B arrives:
A's answer is posted with A quoted (Graph ``messageReference``), never bare
under B. A reply that is still the latest answer is posted without a quote.
Graph and Redis are fakes; nothing here reaches the network.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.services import chat_reply_guard
from app.services.agent_accounts.providers import ProviderError, ProviderSession
from app.services.agent_accounts.providers import microsoft_teams as teams
from app.services.teams_chat_posts import chat_conversation_key
from app.services.teams_late_reply import message_to_quote, quote_attachment, with_quote
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
ME = "account-id"
SAM = "sam-id"
CHAT = "19:abc@thread.v2"


def _msg(msg_id: str, author: str, minute: int, text: str = "hi", *, deleted: bool = False, kind: str = "message"):
    msg: dict[str, Any] = {
        "id": msg_id,
        "messageType": kind,
        "createdDateTime": f"2026-11-11T11:{minute:02d}:00Z",
        "from": {"user": {"id": author, "displayName": "Sam" if author == SAM else "Account"}},
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
    }
    if deleted:
        msg["deletedDateTime"] = "2026-11-11T12:00:00Z"
    return msg


A = _msg("A", SAM, 30, "Can you draft the agenda for <b>Thursday</b>?")
B = _msg("B", SAM, 36, "B")


# -- the decision -------------------------------------------------------------


def test_a_newer_message_from_the_person_makes_the_reply_quote_the_answered_one():
    assert message_to_quote([B, A], ["A"], account_id=ME) is A


def test_a_reply_that_is_still_the_latest_answer_quotes_nothing():
    assert message_to_quote([A], ["A"], account_id=ME) is None
    # the account's own "looking into it" after A is not a newer message to answer
    assert message_to_quote([_msg("ack", ME, 31), A], ["A"], account_id=ME) is None


def test_a_turn_that_answers_both_messages_quotes_nothing():
    assert message_to_quote([B, A], ["A", "B"], account_id=ME) is None


def test_deleted_posts_and_system_events_are_not_newer_messages():
    events = [_msg("x", SAM, 40, deleted=True), _msg("y", "", 41, kind="systemEventMessage"), A]
    assert message_to_quote(events, ["A"], account_id=ME) is None


def test_another_member_of_a_group_chat_counts_as_a_newer_message():
    assert message_to_quote([_msg("C", "jane-id", 33), A], ["A"], account_id=ME) is A


def test_without_the_account_id_only_the_sender_writing_again_counts():
    assert message_to_quote([_msg("C", "jane-id", 33), A], ["A"]) is None
    assert message_to_quote([B, A], ["A"]) is A


def test_an_answered_message_outside_the_latest_messages_quotes_nothing():
    assert message_to_quote([B], ["A"], account_id=ME) is None


def test_the_quote_is_a_message_reference_attachment_placed_above_the_body():
    attachment = quote_attachment(A)
    content = json.loads(attachment["content"])
    assert attachment["id"] == "A" and attachment["contentType"] == "messageReference"
    assert content["messageId"] == "A"
    assert content["messagePreview"] == "Can you draft the agenda for Thursday ?"
    assert content["messageSender"]["user"] == {"id": SAM, "displayName": "Sam", "userIdentityType": "aadUser"}
    payload = with_quote({"body": {"contentType": "html", "content": "<p>Done.</p>"}, "mentions": []}, A)
    assert payload["body"]["content"] == '<attachment id="A"></attachment><p>Done.</p>'
    assert payload["attachments"] == [attachment] and payload["mentions"] == []


def test_a_long_message_is_cut_in_the_preview():
    content = json.loads(quote_attachment(_msg("L", SAM, 1, "word " * 100))["content"])
    assert len(content["messagePreview"]) == 200 and content["messagePreview"].endswith("\u2026")


# -- the agent-account reply ------------------------------------------------------


class _FakeGraph:
    def __init__(self, recent: list[dict] | Exception):
        self.recent = recent
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, session, method, url, *, params=None, payload=None, headers=None, allow_missing=False):
        self.calls.append((method, url, payload))
        if method == "GET" and url == f"{GRAPH}/chats/c1/messages":
            if isinstance(self.recent, Exception):
                raise self.recent
            return {"value": self.recent}
        if method == "POST":
            return {"id": "new-1"}
        raise ProviderError(f"unexpected {method} {url}")


def _provider(monkeypatch, recent) -> tuple[teams.MicrosoftTeamsProvider, _FakeGraph]:
    provider = teams.MicrosoftTeamsProvider()
    fake = _FakeGraph(recent)
    monkeypatch.setattr(provider, "_call", fake)
    return provider, fake


def _session(answers: str | None = "A") -> ProviderSession:
    return ProviderSession(token="t", account_external_id=ME, answers_message_id=answers)


async def test_an_account_reply_after_a_newer_message_quotes_the_answered_one(monkeypatch):
    provider, fake = _provider(monkeypatch, [B, A])
    session = _session()
    assert await provider.send_reply(session, {"kind": "chat", "chat_id": "c1"}, "Here is the agenda.") == "new-1"
    posted = fake.calls[-1][2]
    assert posted["attachments"][0]["contentType"] == "messageReference"
    assert posted["body"]["content"].startswith('<attachment id="A"></attachment>')
    assert session.delivery["quoted_message_id"] == "A"


async def test_an_account_reply_that_is_the_latest_answer_is_posted_as_before(monkeypatch):
    provider, fake = _provider(monkeypatch, [A])
    await provider.send_reply(_session(), {"kind": "chat", "chat_id": "c1"}, "Here is the agenda.")
    assert "attachments" not in fake.calls[-1][2]


async def test_a_failed_read_posts_the_reply_without_a_quote(monkeypatch):
    provider, fake = _provider(monkeypatch, ProviderError("throttled"))
    assert await provider.send_reply(_session(), {"kind": "chat", "chat_id": "c1"}, "Hi") == "new-1"
    assert "attachments" not in fake.calls[-1][2]


async def test_a_channel_reply_or_an_unknown_answered_id_reads_nothing(monkeypatch):
    provider, fake = _provider(monkeypatch, [B, A])
    monkeypatch.setattr(provider, "_reply_url", lambda route: "https://graph.microsoft.com/v1.0/x")
    await provider.send_reply(_session(), {"kind": "channel", "team_id": "t", "channel_id": "c", "root_id": "r"}, "Hi")
    await provider.send_reply(_session(None), {"kind": "chat", "chat_id": "c1"}, "Hi")
    assert [method for method, _, _ in fake.calls] == ["POST", "POST"]


# -- the send_microsoft_chat_message tool in a chat turn ---------------------------


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, seconds):
        return key in self.values

    async def delete(self, *keys):
        return sum(1 for key in keys if self.values.pop(key, None) is not None)


@pytest.fixture
def graph(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr(chat_reply_guard, "redis_client", _client)
    sent: list[tuple[str, str, Any]] = []
    recent = {"value": [B, A]}

    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        sent.append((method, url, kwargs.get("json")))
        request = httpx.Request(method.upper(), url)
        if method == "get" and url == f"{GRAPH}/me":
            return httpx.Response(200, json={"id": ME}, request=request), None
        if method == "get":
            return httpx.Response(200, json=recent, request=request), None
        return httpx.Response(
            201, json={"id": "new-1", "createdDateTime": "2026-11-11T11:37:00Z"}, request=request
        ), None

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return sent, recent


async def _send(message_ids: list[str] | None) -> Any:
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    token = None
    if message_ids is not None:
        payload = {
            "message_id": message_ids[0],
            "message_ids": message_ids,
            "conversation_key": chat_conversation_key(CHAT),
        }
        token = chat_reply_guard.bind_turn(payload, scope=str(uuid.uuid4()), run_id=str(uuid.uuid4()))
    try:
        return await executor.execute_tool(
            ToolCall(
                id="c", name="send_microsoft_chat_message", arguments={"chat_id": CHAT, "message": "Agenda attached."}
            )
        )
    finally:
        if token is not None:
            chat_reply_guard.reset_turn(token)


@pytest.mark.asyncio
async def test_a_turn_still_answering_a_after_b_arrived_posts_a_quoted(graph):
    sent, _ = graph
    result = await _send(["A"])
    posts = [payload for method, _, payload in sent if method == "post"]
    assert len(posts) == 1
    assert posts[0]["attachments"][0]["contentType"] == "messageReference"
    assert json.loads(posts[0]["attachments"][0]["content"])["messageId"] == "A"
    assert "Quotes message A" in result.content


@pytest.mark.asyncio
async def test_a_turn_whose_message_is_still_the_latest_posts_without_a_quote(graph):
    sent, recent = graph
    recent["value"] = [A]
    result = await _send(["A"])
    (post,) = [payload for method, _, payload in sent if method == "post"]
    assert "attachments" not in post and "Quotes message" not in result.content


@pytest.mark.asyncio
async def test_a_send_outside_a_chat_turn_reads_nothing_before_posting(graph):
    sent, _ = graph
    await _send(None)
    assert [method for method, _, _ in sent] == ["post"]

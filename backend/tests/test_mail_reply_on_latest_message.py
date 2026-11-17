"""The mail reply tools answer the newest message of the conversation (#11069).

An agent writing a follow-up in a thread passed the inbound message id it was
handed: the new email replied to the counterpart's message, its quote skipped
the agent's own earlier reply and read as a fresh answer. The reply tools now
make the reply on the newest message of the conversation, sent or received,
whatever id is passed, unless ``reply_to_exact_message=true``. Made on the
mailbox's own sent reply, the follow-up goes to that reply's recipients, never
to the mailbox itself.

Graph and Gmail are answered in memory through ``safe_request``.
"""

from __future__ import annotations

import base64
from email import message_from_bytes
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers import google
from app.integrations.providers.microsoft import mail
from app.integrations.providers.microsoft._thread_reply import latest_message, own_reply_recipients
from app.tools.base import CallableToolExecutor, ToolCall

OWN = "office@example.com"
INBOUND = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdINBOUND"
OWN_REPLY = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdOWNREP"
DRAFT_OLD = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdDRAFTX"
NEW_DRAFT = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdNEWDRF"


def _addr(address: str) -> dict:
    return {"emailAddress": {"address": address}}


INBOUND_MESSAGE = {
    "id": INBOUND,
    "conversationId": "conv-1",
    "from": _addr("researcher@example.org"),
    "toRecipients": [_addr(OWN)],
    "ccRecipients": [_addr("triage@example.org")],
    "receivedDateTime": "2026-11-11T05:58:00Z",
    "subject": "Open redirect",
    "body": {"content": "<html><body>report</body></html>"},
}
OWN_REPLY_MESSAGE = {
    "id": OWN_REPLY,
    "conversationId": "conv-1",
    "from": _addr(OWN),
    "toRecipients": [_addr("researcher@example.org")],
    "ccRecipients": [_addr("triage@example.org"), _addr(OWN)],
    "receivedDateTime": "2026-11-11T08:10:00Z",
    "sentDateTime": "2026-11-11T08:10:00Z",
    "subject": "RE: Open redirect",
    "body": {"content": "<html><body>our first reply</body></html>"},
}
NEWER_DRAFT = {
    "id": DRAFT_OLD,
    "conversationId": "conv-1",
    "from": _addr(OWN),
    "isDraft": True,
    "receivedDateTime": "2026-11-11T09:00:00Z",
}


class _Resp:
    def __init__(self, payload: Any = None):
        self._payload = payload if payload is not None else {}
        self.status_code = 200
        self.content = b""

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    state = SimpleNamespace(calls=[], conversation=[INBOUND_MESSAGE, OWN_REPLY_MESSAGE, NEWER_DRAFT])
    by_id = {INBOUND: INBOUND_MESSAGE, OWN_REPLY: OWN_REPLY_MESSAGE}

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if url.endswith("/me"):
            return _Resp({"mail": OWN}), None
        if url.endswith("/me/messages") and method == "get" and "conversationId eq" in str(kwargs.get("params")):
            return _Resp({"value": state.conversation}), None
        for message_id, message in by_id.items():
            if url.endswith(f"/messages/{message_id}") and method == "get":
                return _Resp(message), None
            if url.endswith(f"/messages/{message_id}/createReply") or url.endswith(
                f"/messages/{message_id}/createReplyAll"
            ):
                # createReply on one's own sent message addresses the mailbox itself.
                quoted = message["body"]["content"]
                return _Resp(
                    {
                        "id": NEW_DRAFT,
                        "body": {"content": f'<html><body><div id="appendonsend"></div><hr>{quoted}</body></html>'},
                        "toRecipients": [message["from"]],
                        "conversationId": "conv-1",
                    }
                ), None
        return _Resp({}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    state.created = lambda: [u for m, u, _k in state.calls if m == "post" and "/createReply" in u]
    state.patches = lambda: [k["json"] for m, u, k in state.calls if m == "patch" and u.endswith(NEW_DRAFT)]
    return state


async def _run(name: str, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


def test_the_latest_message_is_the_newest_sent_or_received_one_drafts_left_out():
    assert latest_message([INBOUND_MESSAGE, OWN_REPLY_MESSAGE, NEWER_DRAFT])["id"] == OWN_REPLY
    assert latest_message([NEWER_DRAFT]) is None


def test_a_follow_up_on_the_own_reply_goes_to_its_recipients_never_to_the_mailbox():
    to, cc = own_reply_recipients(OWN_REPLY_MESSAGE, own_address=OWN.upper())
    assert to == ["researcher@example.org"]
    assert cc == ["triage@example.org"]


async def test_a_draft_reply_to_an_older_inbound_message_is_made_on_the_own_newer_reply(graph):
    answer = await _run("create_microsoft_draft_reply", message_id=INBOUND, body="A follow-up")
    (created,) = graph.created()
    assert created.endswith(f"/{OWN_REPLY}/createReply")
    patch = graph.patches()[0]
    # The quote starts with the mailbox's own reply, the follow-up goes to the counterpart.
    assert "our first reply" in patch["body"]["content"]
    assert patch["toRecipients"] == [_addr("researcher@example.org")]
    assert patch["ccRecipients"] == [_addr("triage@example.org")]
    assert f"Replied on the latest message of the thread ({OWN}, 2026-11-11T08:10:00Z) instead of {INBOUND}" in answer


async def test_a_draft_reply_to_the_latest_message_is_unchanged(graph):
    graph.conversation = [INBOUND_MESSAGE]
    answer = await _run("create_microsoft_draft_reply", message_id=INBOUND, body="An answer")
    (created,) = graph.created()
    assert created.endswith(f"/{INBOUND}/createReply")
    assert "toRecipients" not in graph.patches()[0]
    assert "Replied on the latest message" not in answer


async def test_reply_to_exact_message_answers_the_message_given(graph):
    answer = await _run(
        "create_microsoft_draft_reply", message_id=INBOUND, body="An older branch", reply_to_exact_message=True
    )
    (created,) = graph.created()
    assert created.endswith(f"/{INBOUND}/createReply")
    assert not [u for m, u, k in graph.calls if "conversationId eq" in str(k.get("params"))]
    assert "Replied on the latest message" not in answer


async def test_reply_in_thread_is_made_on_the_own_newer_reply_and_goes_to_the_counterpart(graph):
    answer = await _run("reply_microsoft_email_in_thread", message_id=INBOUND, body="A follow-up")
    (created,) = graph.created()
    assert created.endswith(f"/{OWN_REPLY}/createReply")
    patch = graph.patches()[0]
    assert patch["toRecipients"] == [_addr("researcher@example.org")]
    assert patch["ccRecipients"] == [_addr("triage@example.org")]
    assert "Replied on the latest message of the thread" in answer


async def test_reply_in_thread_opt_out_keeps_the_message_given(graph):
    await _run("reply_microsoft_email_in_thread", message_id=INBOUND, body="Hi", reply_to_exact_message=True)
    (created,) = graph.created()
    assert created.endswith(f"/{INBOUND}/createReply")


def test_the_reply_tools_state_the_rule_and_the_opt_out():
    executor = CallableToolExecutor()
    tools = {d.name: d for d in ms.build_tools(token="dummy", executor=executor)}
    for name in ("create_microsoft_draft_reply", "reply_microsoft_email_in_thread"):
        assert "newest message of the thread" in tools[name].description
        assert "reply_to_exact_message" in tools[name].input_schema["properties"]


# ── Gmail ────────────────────────────────────────────────────────────────


def _gmail(message_id: str, internal: int, sender: str, to: str, labels: list[str]) -> dict:
    return {
        "id": message_id,
        "threadId": "t-1",
        "internalDate": str(internal),
        "labelIds": labels,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Subject", "value": "Open redirect"},
                {"name": "Message-ID", "value": f"<{message_id}@mail>"},
            ]
        },
    }


@pytest.fixture
def gmail(monkeypatch):
    inbound = _gmail("m-in", 1000, "researcher@example.org", OWN, ["INBOX"])
    own = _gmail("m-own", 2000, OWN, "researcher@example.org", ["SENT"])
    draft = _gmail("m-draft", 3000, OWN, "researcher@example.org", ["DRAFT"])
    state = SimpleNamespace(calls=[], thread=[inbound, own, draft], by_id={"m-in": inbound, "m-own": own})

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if "/threads/" in url:
            return _Resp({"messages": state.thread}), None
        for message_id, message in state.by_id.items():
            if url.endswith(f"/messages/{message_id}"):
                return _Resp(message), None
        if url.endswith("/messages/send"):
            return _Resp({"id": "sent-1"}), None
        return _Resp({}), None

    monkeypatch.setattr(google, "safe_request", _fake)
    state.sent = lambda: [
        message_from_bytes(base64.urlsafe_b64decode(k["json"]["raw"]))
        for m, u, k in state.calls
        if m == "post" and u.endswith("/messages/send")
    ]
    return state


async def _run_gmail(**arguments: Any) -> str:
    executor = CallableToolExecutor()
    google.build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="reply_gmail", arguments=arguments))
    return result.content


async def test_gmail_reply_is_made_on_the_own_newer_reply_and_goes_to_the_counterpart(gmail):
    answer = await _run_gmail(message_id="m-in", body="A follow-up")
    (sent,) = gmail.sent()
    assert sent["In-Reply-To"] == "<m-own@mail>"
    assert sent["To"] == "researcher@example.org"
    assert "Replied on the latest message of the thread" in answer


async def test_gmail_reply_opt_out_answers_the_message_given(gmail):
    answer = await _run_gmail(message_id="m-in", body="Hi", reply_to_exact_message=True)
    (sent,) = gmail.sent()
    assert sent["In-Reply-To"] == "<m-in@mail>"
    assert sent["To"] == "researcher@example.org"
    assert "Replied on the latest message" not in answer
    assert not [u for _m, u, _k in gmail.calls if "/threads/" in u]

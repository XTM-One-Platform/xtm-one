"""An Outlook send answered by a 5xx or a timeout is settled from Sent Items, never retried (#11848).

Graph often delivers a message behind a failing gateway: the send tools read
the mailbox's Sent Items and answer sent (naming the copy) or NOT sent
explicitly, or state unknown when Sent Items cannot be read. Graph is replaced
by a fake answering by URL; nothing here reaches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.providers.microsoft import _send_confirm as send_confirm
from app.tools.base import CallableToolExecutor

SENT_ITEMS_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages"
DRAFT_ID = "AAMkAGI2" + "D" * 140
ERR_502 = "Error: Microsoft server error (502) while trying to send email. The service may be temporarily unavailable."
ERR_TIMEOUT = "Error: Microsoft request timed out while trying to send draft. The service may be slow or overloaded."


def _copy(subject, to, *, msg_id="sent-1", internet_id="", seconds_ago=1):
    sent_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return {
        "id": msg_id,
        "subject": subject,
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [],
        "sentDateTime": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "webLink": f"https://outlook.example/owa/{msg_id}",
        "internetMessageId": internet_id,
    }


class _Graph:
    """Sends answer *send_error*; Sent Items answers *sent_items* (or *sent_items_error*); a draft read answers *draft*."""

    def __init__(self, *, send_error, sent_items=None, sent_items_error=None, draft=None):
        self.send_error = send_error
        self.sent_items = sent_items or []
        self.sent_items_error = sent_items_error
        self.draft = draft
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, _client, method, url, *_a, **_k):
        self.calls.append((method, url))
        if method == "post" and (url.endswith("/send") or url.endswith("/sendMail")):
            return None, self.send_error
        if method == "get" and url == SENT_ITEMS_URL:
            if self.sent_items_error:
                return None, self.sent_items_error
            return SimpleNamespace(json=lambda: {"value": self.sent_items}, status_code=200), None
        if method == "get" and self.draft is not None:
            return SimpleNamespace(json=lambda: self.draft, status_code=200), None
        return SimpleNamespace(json=lambda: {}, status_code=200), None

    def sends(self) -> list[str]:
        return [u for m, u in self.calls if m == "post" and (u.endswith("/send") or u.endswith("/sendMail"))]

    def sent_items_reads(self) -> int:
        return sum(1 for m, u in self.calls if m == "get" and u == SENT_ITEMS_URL)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(send_confirm, "READ_DELAYS_SECONDS", (0.0, 0.0, 0.0))


def _handlers(monkeypatch, graph):
    from app.integrations.providers import microsoft

    monkeypatch.setattr(microsoft._common, "safe_request", graph)
    executor = CallableToolExecutor()
    microsoft.build_tools("graph-token", executor)
    return executor._handlers


# ---------------------------------------------------------------------------
# The pure parts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("err", "ambiguous"),
    [
        (ERR_502, True),
        ("Error: Microsoft server error (504) while trying to send draft MIME. x", True),
        (ERR_TIMEOUT, True),
        ("Error: Microsoft request failed while trying to send email: RemoteProtocolError", True),
        ("Error: Microsoft rejected the request to send email — bad request.", False),
        ("Error: Microsoft rate limit exceeded while trying to send email. Wait a moment and try again.", False),
        ("Error: Could not connect to Microsoft while trying to send email — the service may be unreachable.", False),
        ("Error: Microsoft server error (502) while trying to read draft MIME. x", False),
        ("", False),
    ],
)
def test_only_a_5xx_or_a_timeout_on_the_send_leaves_the_state_open(err, ambiguous):
    assert send_confirm.is_ambiguous_send_error(err) is ambiguous


def test_a_copy_is_matched_by_its_internet_id_else_by_subject_and_recipients():
    by_id = _copy("Other subject", ["z@x.example"], msg_id="by-id", internet_id="<m1@x>")
    by_fields = _copy("RE: Digest", ["A@x.example"], msg_id="by-fields")
    other = _copy("Digest", ["b@x.example"], msg_id="other")
    found = send_confirm.find_sent_copy(
        [other, by_id], subject="Digest", recipients=["a@x.example"], internet_message_id="<m1@x>"
    )
    assert found["id"] == "by-id"
    found = send_confirm.find_sent_copy([other, by_fields], subject="Digest", recipients=["a@x.example"])
    assert found["id"] == "by-fields"
    assert send_confirm.find_sent_copy([other], subject="Digest", recipients=["a@x.example"]) is None


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


async def test_a_502_after_a_successful_send_answers_sent_and_does_not_send_again(monkeypatch):
    graph = _Graph(send_error=ERR_502, sent_items=[_copy("Digest", ["a@x.example"])])
    result = await _handlers(monkeypatch, graph)["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi")
    assert result.startswith("Email sent to a@x.example"), result
    assert "is in Sent Items" in result and "sent-1" in result and "was not sent again" in result
    assert len(graph.sends()) == 1, "never retried"
    assert graph.sent_items_reads() == 1


async def test_a_502_after_a_failed_send_answers_not_sent(monkeypatch):
    graph = _Graph(send_error=ERR_502, sent_items=[_copy("Digest", ["someone-else@x.example"])])
    result = await _handlers(monkeypatch, graph)["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi")
    assert "server error (502)" in result
    assert "NOT sent: Sent Items, read after the error, holds no copy" in result
    assert "Email sent" not in result
    assert len(graph.sends()) == 1, "never retried"
    assert graph.sent_items_reads() == len(send_confirm.READ_DELAYS_SECONDS)


async def test_an_unreadable_sent_items_answers_state_unknown(monkeypatch):
    graph = _Graph(send_error=ERR_502, sent_items_error="Error: Microsoft server error (503) while trying to confirm")
    result = await _handlers(monkeypatch, graph)["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi")
    assert "Send state UNKNOWN" in result and "before sending this message again" in result
    assert len(graph.sends()) == 1


async def test_a_refusal_is_answered_as_it_is_without_reading_sent_items(monkeypatch):
    refusal = "Error: Microsoft rejected the request to send email — bad request."
    graph = _Graph(send_error=refusal)
    result = await _handlers(monkeypatch, graph)["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi")
    assert result == refusal
    assert graph.sent_items_reads() == 0


async def test_a_draft_send_timing_out_is_confirmed_by_its_internet_message_id(monkeypatch):
    draft = {
        "subject": "Re: Question",
        "toRecipients": [{"emailAddress": {"address": "a@x.example"}}],
        "ccRecipients": [],
        "conversationId": "conv-1",
        "internetMessageId": "<draft-1@x>",
        "body": {"contentType": "text", "content": "hi"},
    }
    copy = _copy("Re: Question (edited)", ["a@x.example"], msg_id="sent-9", internet_id="<draft-1@x>")
    graph = _Graph(send_error=ERR_TIMEOUT, sent_items=[copy], draft=draft)
    from app.integrations.providers import microsoft

    monkeypatch.setattr(microsoft._common, "safe_request", graph)
    executor = CallableToolExecutor()
    microsoft.build_tools("graph-token", executor, integration_id="mailbox-1")
    result = await executor._handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert f"Draft {DRAFT_ID} has been sent." in result and "sent-9" in result
    assert len(graph.sends()) == 1

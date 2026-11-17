"""The log of the messages sent through an agent account, without a database.

What produced a send is read from the run's tool context; a log that cannot be
written is a server-side warning and never the send's failure.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest

from app.integrations.providers.microsoft._common import MicrosoftContext
from app.integrations.providers.microsoft._teams import _thread_route
from app.services.agent_accounts import sent_log, state
from app.services.agent_accounts.rules import normalize_rules, preset

_ROUTE = {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "root-1"}


def _account(rules: list[dict] | None = None):
    # The default rules answer in every channel on a mention and continue the threads.
    chosen = normalize_rules(rules) if rules is not None else preset("on_behalf")["rules"]
    return SimpleNamespace(id=uuid.uuid4(), enabled=True, agent_id=uuid.uuid4(), rules=chosen)


@pytest.fixture
def followed(monkeypatch):
    calls: dict[str, list] = {"active": [], "follow": []}

    async def _active(account_id, thread_id):
        calls["active"].append((account_id, thread_id))

    async def _follow(account_id, conversation_id, thread_id, route):
        calls["follow"].append((account_id, conversation_id, thread_id, route))

    monkeypatch.setattr(state, "mark_thread_active", _active)
    monkeypatch.setattr(state, "follow_thread", _follow)
    return calls


async def test_a_tool_reply_in_a_channel_thread_makes_its_follow_ups_heard(followed):
    # The owner answered in a channel thread through a Teams tool: the thread
    # continues for the rules, and a channel heard for mentions alone reads its replies.
    account = _account()
    assert await sent_log.follow_sent_thread(account, _ROUTE) is True
    assert followed["active"] == [(str(account.id), "ch1:root-1")]
    assert followed["follow"] == [(str(account.id), "ch1", "ch1:root-1", _ROUTE)]


async def test_a_thread_is_not_read_when_its_channel_is_heard_in_full_or_no_rule_continues_it(followed):
    in_full = _account([{"conversation_types": ["channel"], "trigger": "any", "continue_threads": True}])
    no_follow = _account([{"conversation_types": ["channel"], "continue_threads": False}])
    assert await sent_log.follow_sent_thread(in_full, _ROUTE) is False
    assert await sent_log.follow_sent_thread(no_follow, _ROUTE) is False
    # The thread still counts as answered: a follow-up a subscription brings continues it.
    assert len(followed["active"]) == 2 and followed["follow"] == []


async def test_a_send_that_is_not_in_a_channel_thread_follows_nothing(followed):
    account = _account()
    assert await sent_log.follow_sent_thread(account, {"kind": "chat", "chat_id": "c1"}) is False
    assert await sent_log.follow_sent_thread(account, {**_ROUTE, "root_id": ""}) is False
    assert followed == {"active": [], "follow": []}


def test_the_thread_of_a_post_is_the_post_and_of_a_reply_its_root():
    assert _thread_route("t1", "ch1", "root-1") == _ROUTE
    assert _thread_route("t1", "ch1", "") is None
    assert _thread_route("", "ch1", "root-1") is None


def test_the_origin_keeps_what_produced_the_send_and_nothing_else():
    assignment = SimpleNamespace(id="a-1")
    origin = sent_log.origin_of(
        {
            "source": "chat",
            "agent_id": "agent-1",
            "conversation_id": "conv-1",
            "run_id": "",
            "assignment": assignment,
            "user_token": "secret",
        }
    )
    assert origin == {"source": "chat", "agent_id": "agent-1", "conversation_id": "conv-1", "assignment_id": "a-1"}
    assert sent_log.origin_of({"assignment": {"id": "a-2"}}) == {"assignment_id": "a-2"}
    assert sent_log.origin_of(None) == {}
    assert len(sent_log.origin_of({"source": "x" * 1000})["source"]) == 200


async def test_a_send_outside_an_integration_logs_nothing():
    assert await sent_log.record_proactive_send(None, provider="microsoft_teams", tool="t", conversation_id="c", text="hi") == 0
    assert (
        await sent_log.record_proactive_send("not-a-uuid", provider="microsoft_teams", tool="t", conversation_id="c", text="hi")
        == 0
    )


async def test_a_log_that_cannot_be_written_warns_and_never_raises(monkeypatch, caplog):
    def _broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(sent_log, "async_session", _broken)
    with caplog.at_level(logging.WARNING, logger=sent_log.__name__):
        written = await sent_log.record_proactive_send(
            str(uuid.uuid4()),
            provider="microsoft_teams",
            tool="send_microsoft_chat_message",
            conversation_id="chat-1",
            text="Hello",
            error="403 Forbidden",
        )
    assert written == 0
    assert any("Could not log a message sent" in record.getMessage() for record in caplog.records)


async def test_the_microsoft_tools_hand_their_sends_to_the_log_with_the_run_origin(monkeypatch):
    calls: list[tuple[str | None, dict]] = []

    async def _record(integration_id, **kwargs):
        calls.append((integration_id, kwargs))
        return 1

    monkeypatch.setattr(sent_log, "record_proactive_send", _record)
    integration_id = str(uuid.uuid4())
    ctx = MicrosoftContext("token", integration_id=integration_id, send_origin={"source": "chat", "run_id": "r-1"})
    await ctx.log_account_send(
        "microsoft_teams", "send_microsoft_chat_message", conversation_id="chat-1", text="Hi", error="boom"
    )
    ((logged_for, kwargs),) = calls
    assert logged_for == integration_id
    assert kwargs["provider"] == "microsoft_teams" and kwargs["tool"] == "send_microsoft_chat_message"
    assert kwargs["error"] == "boom" and kwargs["origin"] == {"source": "chat", "run_id": "r-1"}
    assert kwargs["thread_route"] is None
    # A session with no integration (a send outside an agent's tools) logs nothing.
    calls.clear()
    await MicrosoftContext("token").log_account_send("microsoft_teams", "t", conversation_id="c", text="Hi")
    assert calls == []

"""An agent account acts as the account it operates: the pure parts.

Two agent accounts never answer each other; a turn shows its sender, not the
owner running it, with the owner's clock; a sync of an account is never run
twice at once; the account already operating a platform account is named to
its own owner only.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.agent_accounts import listening, state
from app.services.agent_accounts.conversation import message_context
from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.rules import (
    REASONS,
    AccountSettings,
    Decision,
    EventState,
    RuleError,
    evaluate,
    normalize_rules,
    preset,
)
from app.services.builtin_tools import prompting

FRONTEND_LABELS = (
    pathlib.Path(__file__).resolve().parents[2] / "frontend/src/pages/profile/agent-accounts/agent-accounts.ts"
)


def _event(**overrides) -> AccountEvent:
    values = {
        "provider": "microsoft_teams",
        "conversation_type": "direct",
        "conversation_id": "chat-1",
        "thread_id": "chat-1",
        "message_id": "m-1",
        "sender_id": "sam-id",
        "sender_name": "Sam",
        "sender_email": "sam@example.com",
        "text": "Can you book the board room for Thursday?",
        "created_at": datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return AccountEvent(**values)


def _settings(mode: str) -> AccountSettings:
    base = preset(mode)
    return AccountSettings(
        enabled=True,
        mode=mode,
        response_mode=base["response_mode"],
        ai_label_enabled=base["ai_label_enabled"],
        grace_period_seconds=0,
        owner_takeover_minutes=base["owner_takeover_minutes"],
        max_replies_per_hour=base["max_replies_per_hour"],
        active_hours=None,
        instructions="",
        rules=base["rules"],
    )


def test_a_message_another_agent_account_posted_is_left_even_in_a_direct_chat():
    assert evaluate(_settings("ai_user"), _event()).respond
    decision = evaluate(_settings("ai_user"), _event(), EventState(agent_post=True))
    assert decision == Decision(False, "agent_author")


def test_every_reason_of_the_engine_has_a_label_in_the_editor():
    source = FRONTEND_LABELS.read_text(encoding="utf-8")
    body = re.search(r"export const DECISION_REASON_LABELS = \{(.*?)\n\} as const;", source, re.S)
    assert body
    assert set(re.findall(r"^\s*(\w+):", body.group(1), re.M)) == set(REASONS)
    outcomes = re.search(r"export const OUTCOME_LABELS = \{(.*?)\n\} as const;", source, re.S)
    assert outcomes and re.search(r"^\s*agent_author:", outcomes.group(1), re.M)


def test_the_sender_is_the_identity_and_the_owner_keeps_the_clock(monkeypatch):
    monkeypatch.setattr(prompting, "format_datetime_prompt", lambda tz: f"\nNow in {tz}.")
    context = message_context(
        _event(), Decision(True, "respond", must_reply=True), platform="teams", brief="You are the assistant."
    )
    assert context["contact_name"] == "Sam" and context["sender_email"] == "sam@example.com"
    parts = prompting.build_identity_context(None, context=context, is_anonymous=True, timezone="Europe/Paris")
    assert parts[0] == "\nNow in Europe/Paris."
    assert "platform-reported, unverified" in parts[1] and "sam@example.com" in parts[1]
    # A user record keeps its own time zone.
    owner = SimpleNamespace(full_name="Owner", email="o@example.com", title=None, timezone="Asia/Tokyo")
    assert prompting.build_identity_context(owner, timezone="Europe/Paris")[0] == "\nNow in Asia/Tokyo."


async def test_an_account_already_being_synced_is_not_synced_twice(monkeypatch):
    calls: list[str] = []

    async def _held(name, seconds):
        calls.append(name)
        return False

    async def _never(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("synced while another sync holds the lease")

    monkeypatch.setattr(state, "take_lease", _held)
    monkeypatch.setattr(listening, "_sync_account", _never)
    account = SimpleNamespace(id=uuid.uuid4())
    assert await listening.sync_account(None, account) is None
    assert calls == [f"sync:{account.id}"]


async def test_the_lease_of_a_sync_is_given_back_when_it_ends(monkeypatch):
    released: list[str] = []

    async def _taken(name, seconds):
        return True

    async def _release(name):
        released.append(name)

    async def _sync(db, account, *, poll):
        raise RuntimeError("the provider failed")

    monkeypatch.setattr(state, "take_lease", _taken)
    monkeypatch.setattr(state, "release_lease", _release)
    monkeypatch.setattr(listening, "_sync_account", _sync)
    account = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(RuntimeError):
        await listening.sync_account(None, account)
    assert released == [f"sync:{account.id}"]


def test_who_may_ask_is_the_people_named_or_a_member_of_a_group_named():
    board = str(uuid.uuid4())
    rules = normalize_rules(
        [{"id": "board", "conversation_types": ["direct"], "trigger": "any", "groups": [{"id": board, "name": "Board"}],
          "senders": [{"id": "x", "email": "assistant-of-the-ceo@example.com"}]}]
    )
    settings = AccountSettings(rules=rules, response_mode="auto")
    assert evaluate(settings, _event()).reason == "no_matching_rule"
    assert evaluate(settings, _event(), EventState(sender_group_ids=frozenset({board.upper()}))).respond
    assert evaluate(settings, _event(sender_email="Assistant-of-the-CEO@example.com")).respond
    assert evaluate(settings, _event(), EventState(sender_group_ids=frozenset({"another"}))).reason == "no_matching_rule"


def test_a_rules_access_is_normalized_and_travels_with_its_decision():
    with pytest.raises(RuleError, match="unknown access setting"):
        normalize_rules([{"access": {"databases": []}}])
    with pytest.raises(RuleError, match="list of ids"):
        normalize_rules([{"access": {"knowledge_base_ids": "kb-1"}}])
    with pytest.raises(RuleError, match="memory setting"):
        normalize_rules([{"access": {"memory": "sometimes"}}])
    # Nothing narrowed: the agent's full set, stored as no access block at all.
    (full,) = normalize_rules([{"access": {"knowledge_base_ids": None, "memory": "on"}}])
    assert full["access"] is None and full["groups"] == []

    (public,) = normalize_rules(
        [{"id": "general", "conversation_types": ["channel"], "trigger": "mention",
          "access": {"integrations": ["opencti", "opencti"], "knowledge_base_ids": [], "memory": "off"}}]
    )
    assert public["access"] == {
        "integrations": ["opencti"], "knowledge_base_ids": [], "mcp_server_ids": None,
        "builtin_tool_ids": None, "custom_tool_ids": None, "skill_ids": None, "memory": "off",
    }
    decision = evaluate(
        AccountSettings(rules=[public]), _event(conversation_type="channel", mentions_account=True)
    )
    assert decision.respond and decision.access == public["access"]
    assert decision.as_dict()["access"]["memory"] == "off"


def test_the_operating_account_is_named_to_its_own_owner_only():
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    operator = listening.Operator(account_id=uuid.uuid4(), name="Executive Assistant", user_id=owner)
    assert "'Executive Assistant'" in operator.message("Microsoft Teams", owner)
    hidden = operator.message("Microsoft Teams", stranger)
    assert "Executive Assistant" not in hidden and "Another user's agent account" in hidden

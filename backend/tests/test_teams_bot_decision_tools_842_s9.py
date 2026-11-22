"""The Teams bot card tools take a decision instead of card JSON (#842, S9, human-gates.md I.2).

An agent posting a decision passes its id (``run:<id>``, ``work_item:<id>``...):
the tool renders the platform's decision card, the one the notify paths send,
so a decision is never a hand-written card that nothing can answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.providers import teams_bot
from app.services import decision_card_delivery
from app.tools.base import CallableToolExecutor

_DECISION_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Ship it?"}],
    "actions": [{"type": "Action.Execute", "verb": "xtm.decision.answer", "title": "Yes", "data": {"o": "A"}}],
}

_CARD_TOOLS = ("send_teams_bot_channel_card", "send_teams_bot_chat_card", "send_teams_bot_group_chat_card")


def _executor_and_defs():
    executor = CallableToolExecutor()
    defs = teams_bot.build_tools("tenant-123||client-abc||secret-xyz", executor)
    return executor, {d.name: d for d in defs}


class _Recorder:
    def __init__(self, responses: list[dict]):
        self.calls: list[dict] = []
        self._responses = responses

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        payload = self._responses.pop(0) if self._responses else {}
        resp = AsyncMock()
        resp.json = lambda p=payload: p
        return resp, None


def _patched(recorder: _Recorder):
    return (
        patch.object(teams_bot, "_acquire_token", new=AsyncMock(return_value="tok")),
        patch.object(teams_bot, "_get_service_url", new=AsyncMock(return_value="https://smba.example/amer")),
        patch.object(teams_bot, "safe_request", new=recorder),
    )


@pytest.mark.parametrize("tool", _CARD_TOOLS)
def test_every_card_tool_offers_decision_and_no_longer_requires_card(tool):
    _executor, defs = _executor_and_defs()
    schema = defs[tool].input_schema
    assert "decision" in schema["properties"]
    assert "card" not in schema["required"]


@pytest.mark.asyncio
async def test_a_group_chat_decision_sends_the_rendered_card_as_a_native_card():
    executor, _defs = _executor_and_defs()
    recorder = _Recorder([{"id": "act-1"}])
    render = AsyncMock(return_value=(dict(_DECISION_CARD), "Ship it?"))
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3, patch.object(decision_card_delivery, "decision_card_for_tool", new=render):
        result = await executor._handlers["send_teams_bot_group_chat_card"](
            chat_id="19:abc@thread.v2", decision="work_item:2b7a1f3e-0000-4000-8000-000000000001"
        )
    render.assert_awaited_once_with("work_item:2b7a1f3e-0000-4000-8000-000000000001")
    assert "Adaptive Card sent" in result
    activity = recorder.calls[0]["json"]
    content = activity["attachments"][0]["content"]
    assert content["actions"][0]["verb"] == "xtm.decision.answer"
    # The question is the toast preview, never a second ``text`` (one Skype activity).
    assert activity["summary"] == "Ship it?"
    assert "text" not in activity


@pytest.mark.asyncio
async def test_a_chat_decision_keeps_the_agent_preview_when_given():
    executor, _defs = _executor_and_defs()
    recorder = _Recorder([{"id": "user-aad-1"}, {"id": "conv-1", "activityId": "act-2"}])
    render = AsyncMock(return_value=(dict(_DECISION_CARD), "Ship it?"))
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3, patch.object(decision_card_delivery, "decision_card_for_tool", new=render):
        result = await executor._handlers["send_teams_bot_chat_card"](
            user_email="someone@example.com", decision="run:2b7a1f3e-0000-4000-8000-000000000002", fallback_text="Go?"
        )
    assert "Adaptive Card sent" in result
    activity = recorder.calls[1]["json"]["activity"]
    assert activity["attachments"][0]["content"]["actions"][0]["verb"] == "xtm.decision.answer"
    assert activity["summary"] == "Go?"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _CARD_TOOLS)
async def test_an_unknown_decision_sends_nothing_and_says_so(tool):
    executor, _defs = _executor_and_defs()
    recorder = _Recorder([])
    render = AsyncMock(return_value="Error: no decision found for 'run:nope'.")
    target = {
        "send_teams_bot_channel_card": {"team_id": "t", "channel_id": "19:c@thread.tacv2"},
        "send_teams_bot_chat_card": {"user_email": "someone@example.com"},
        "send_teams_bot_group_chat_card": {"chat_id": "19:abc@thread.v2"},
    }[tool]
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3, patch.object(decision_card_delivery, "decision_card_for_tool", new=render):
        result = await executor._handlers[tool](**target, decision="run:nope")
    assert result.startswith("Error: no decision found")
    assert recorder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _CARD_TOOLS)
async def test_neither_card_nor_decision_is_refused_before_any_send(tool):
    executor, _defs = _executor_and_defs()
    recorder = _Recorder([])
    target = {
        "send_teams_bot_channel_card": {"team_id": "t", "channel_id": "19:c@thread.tacv2"},
        "send_teams_bot_chat_card": {"user_email": "someone@example.com"},
        "send_teams_bot_group_chat_card": {"chat_id": "19:abc@thread.v2"},
    }[tool]
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers[tool](**target)
    assert result == "Error: pass a card (Adaptive Card JSON) or a decision id."
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_decision_card_for_tool_names_a_bad_id(monkeypatch):
    from fastapi import HTTPException

    async def _refuse(_db, _raw):
        raise HTTPException(status_code=404, detail="Decision not found")

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    monkeypatch.setattr("app.services.decision_router.resolve", _refuse)
    result = await decision_card_delivery.decision_card_for_tool("bogus")
    assert isinstance(result, str)
    assert result.startswith("Error: no decision found for 'bogus'")

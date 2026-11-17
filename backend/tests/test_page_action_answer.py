"""The page's answer to an ``act_on_page`` action, on the approval route.

The OpenCTI chatbot proxy forwards ``/chat/messages/approve`` whole, so the
widget answers there with ``page_action`` instead of ``decisions``.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1 import platform
from app.tools.approval import DECISION_IMPERSONATED_BY


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _Db:
    """Answers the ownership lookup, then the existence lookup, from a script."""

    def __init__(self, *objs):
        self._objs = list(objs)

    async def execute(self, _stmt):
        return _Result(self._objs.pop(0) if self._objs else None)


_USER = SimpleNamespace(id=uuid.uuid4())


def _answer(action_id="a1", **extra) -> dict:
    return {"action_id": action_id, "ok": True, "url": "/dashboard", "title": "t", "snapshot": "[e1] link \"x\"", **extra}


@pytest.fixture
def delivered(monkeypatch):
    seen: list[tuple[str, dict]] = []
    state = {"accept": True}

    async def _push(conv, result):
        seen.append((conv, result))
        return state["accept"]

    async def _seen(_conv):
        return None

    monkeypatch.setattr("app.services.chat_turn_guard.push_page_action_result", _push)
    monkeypatch.setattr("app.services.chat_turn_guard.mark_client_seen", _seen)
    return seen, state


async def _call(body: dict, db: _Db):
    return await platform.approve_platform_chat_tools(
        body=platform.PlatformChatApprovalRequest(**body), db=db, user=_USER, _chat_gate=None
    )


@pytest.mark.asyncio
async def test_a_matching_answer_is_accepted(delivered):
    seen, _ = delivered
    conv = str(uuid.uuid4())
    result = await _call({"conversation_id": conv, "page_action": _answer()}, _Db(object()))
    assert result == {"status": "accepted"}
    assert seen[0][0] == conv and seen[0][1]["action_id"] == "a1"


@pytest.mark.asyncio
async def test_an_answer_nothing_waits_for_is_a_409(delivered):
    _, state = delivered
    state["accept"] = False
    with pytest.raises(HTTPException) as exc:
        await _call({"conversation_id": str(uuid.uuid4()), "page_action": _answer()}, _Db(object()))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"decisions": [{"tool_call_id": "c1", "decision": "approve"}], "page_action": _answer()},
        {},
    ],
    ids=["both", "neither"],
)
async def test_exactly_one_kind_of_answer_is_required(delivered, body):
    with pytest.raises(HTTPException) as exc:
        await _call({"conversation_id": str(uuid.uuid4()), **body}, _Db(object()))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_another_users_conversation_is_a_404_with_no_approval_record(delivered, monkeypatch):
    recorded = []

    async def _record(*a, **k):
        recorded.append(k)

    monkeypatch.setattr("app.services.chat_approval_resume.record_refused_decision", _record)
    with pytest.raises(HTTPException) as exc:
        # The ownership lookup misses; the conversation exists for someone else.
        await _call({"conversation_id": str(uuid.uuid4()), "page_action": _answer()}, _Db(None, object()))
    assert exc.value.status_code == 404
    assert recorded == []
    assert delivered[0] == []


def test_an_oversized_snapshot_is_refused_by_the_schema():
    with pytest.raises(ValidationError):
        platform.PlatformChatApprovalRequest(
            conversation_id=str(uuid.uuid4()), page_action=_answer(snapshot="x" * 20_001)
        )


def test_the_message_request_declares_page_action_support():
    assert platform.PlatformChatRequest(content="hi").supports_page_actions is False
    assert platform.PlatformChatRequest(content="hi", supports_page_actions=True).supports_page_actions


@pytest.mark.asyncio
async def test_decisions_alone_still_reach_the_approval_inbox(delivered, monkeypatch):
    pushed = []

    async def _push(conv, decisions):
        pushed.append(decisions)
        return True

    monkeypatch.setattr("app.services.chat_turn_guard.push_approval_decision", _push)
    result = await _call(
        {"conversation_id": str(uuid.uuid4()), "decisions": [{"tool_call_id": "c1", "decision": "approve"}]},
        _Db(object()),
    )
    assert result == {"status": "accepted", "decided": 1}
    assert pushed == [{"c1": {"decision": "approve", "rejection_reason": None, DECISION_IMPERSONATED_BY: None}}]


@pytest.mark.asyncio
async def test_the_answer_is_delivered_under_the_canonical_conversation_id(delivered):
    # The tool keys its wait on the canonical id: another spelling of the same
    # UUID passes the ownership check and must reach the same wait.
    seen, _ = delivered
    conv = uuid.uuid4()
    await _call({"conversation_id": "{" + str(conv).upper() + "}", "page_action": _answer()}, _Db(object()))
    assert seen[0][0] == str(conv)

"""A chat "Yes, always" names whoever answered, not whoever sent the turn (FR-017b).

The answer arrives on its own request (the web and platform approve routes),
from any of the owner's sessions, while the turn that applies it was sent by
one of them. Each pushed decision carries the approving session's
impersonation; the turn's own is only the fallback.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import platform as platform_api
from app.models.tool_approval import ToolApprovalPreference
from app.services import chat_approval_resume, chat_turn_guard
from app.services.assignment_engine import _apply_approval_decisions
from app.services.tool_approval_service import grant_chat_standing_approval
from app.tools.approval import DECISION_IMPERSONATED_BY, approver_admin_id

CONV = str(uuid.uuid4())
OWNER = uuid.uuid4()
ADMIN = uuid.uuid4()
PROPOSAL = {
    "tool_call_id": "call_1",
    "tool_name": "delete_entity",
    "arguments": {},
    "input_schema": {"type": "object", "properties": {}},
    "source": "integration:opencti",
}


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def expire(self, key, _ttl):
        return 1

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lpop(self, key):
        items = self.lists.get(key)
        return items.pop(0) if items else None


class _FakeDb:
    """The preference lookup finds nothing; the audit's user lookup finds the owner."""

    def __init__(self):
        self.added: list = []
        self._scalars = 0

    async def scalar(self, _stmt):
        self._scalars += 1
        return None if self._scalars == 1 else SimpleNamespace(id=OWNER, email="owner@example.com")

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


class _Executor:
    async def execute_tool(self, call):
        return SimpleNamespace(content="done")


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    r.kv[chat_turn_guard.processing_key(CONV)] = "turn-token"

    async def _redis():
        return r

    monkeypatch.setattr(chat_turn_guard, "_redis", _redis)
    monkeypatch.setattr(chat_turn_guard, "mark_client_seen", _noop)
    monkeypatch.setattr(platform_api, "approval_conversation", _owned)
    return r


@pytest.fixture
def audits(monkeypatch):
    records: list[dict] = []

    async def _capture(_db, **kwargs):
        records.append(kwargs)

    monkeypatch.setattr("app.services.audit.log_audit", _capture)
    return records


async def _noop(*_args, **_kwargs):
    return None


async def _owned(*_args, **_kwargs):
    return uuid.UUID(CONV)


def _approver(impersonated_by=None):
    """The approving request's user: always the owner, impersonated or not (FR-008a)."""
    user = SimpleNamespace(id=OWNER)
    if impersonated_by is not None:
        user.impersonated_by = str(impersonated_by)
    return user


async def _answer_always(approver):
    decision = platform_api.PlatformChatApprovalDecision(tool_call_id="call_1", decision="approve_always")
    await platform_api.apply_approval_decisions(None, approver, CONV, [decision], surface="web_stream", channel=None)


async def _apply_on_turn(decisions, *, turn_impersonated_by):
    """What the turn does with the decisions: the applier, with the chat sink."""
    db = _FakeDb()

    async def _sink(proposal, decision):
        await grant_chat_standing_approval(
            db,
            user_id=OWNER,
            proposal=proposal,
            decision=decision,
            turn_impersonated_by=turn_impersonated_by,
        )

    await _apply_approval_decisions(
        executor=_Executor(),
        messages=[],
        proposals=[PROPOSAL],
        decisions=decisions,
        on_approve_always=_sink,
    )
    [preference] = [o for o in db.added if isinstance(o, ToolApprovalPreference)]
    return preference


async def test_an_admin_answering_a_turn_the_owner_sent_is_recorded(fake_redis, audits):
    await _answer_always(_approver(impersonated_by=ADMIN))
    decisions = await chat_turn_guard.await_approval_decision(CONV, poll_seconds=0)

    preference = await _apply_on_turn(decisions, turn_impersonated_by=None)

    # Granted to the identity the turn runs as (FR-008c), attributed to the admin.
    assert preference.user_id == OWNER
    assert preference.granted_by_admin_id == ADMIN
    assert audits[0]["details"]["impersonated_by"] == str(ADMIN)


async def test_the_owner_answering_a_turn_an_admin_sent_records_no_admin(fake_redis, audits):
    await _answer_always(_approver())
    decisions = await chat_turn_guard.await_approval_decision(CONV, poll_seconds=0)

    preference = await _apply_on_turn(decisions, turn_impersonated_by=str(ADMIN))

    assert preference.user_id == OWNER
    assert preference.granted_by_admin_id is None
    assert audits[0]["details"]["impersonated_by"] is None


async def test_a_decision_naming_no_approver_falls_back_to_the_turn(audits):
    """Pushed by a process that predates the field (rolling deploy)."""
    preference = await _apply_on_turn({"call_1": {"decision": "approve_always"}}, turn_impersonated_by=str(ADMIN))

    assert preference.granted_by_admin_id == ADMIN
    assert audits[0]["details"]["impersonated_by"] == str(ADMIN)


def test_a_resuming_client_cannot_name_the_approver():
    """A REST resume is decided on the request that runs the turn, so the
    turn's impersonation is the approver's; a client-sent value is dropped."""
    forged = chat_approval_resume.normalize_decisions(
        [{"tool_call_id": "call_1", "decision": "approve_always", DECISION_IMPERSONATED_BY: str(ADMIN)}]
    )

    assert DECISION_IMPERSONATED_BY not in forged["call_1"]
    assert approver_admin_id(forged["call_1"], None) is None

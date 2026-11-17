"""What a chat turn records about its own gating (T114/T115).

The gate is synchronous by contract — it sits on the hot path of every tool
call — so it only fills two sinks and leaves the writing to whoever installed
it. Assignment runs drained both from the start; chat drained neither, and the
asymmetry was invisible because nothing failed: conversations simply had no
answer to "why did this run without asking me?" (FR-020), and a standing
approval whose tool had changed shape was dropped from memory while its row
went on claiming the user always allows that tool (FR-019a/b).

These tests pin the drain, not the gate — the decision itself is covered by
``test_tool_approval_gate.py``.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.tool_tracking import record_chat_gate_outcomes
from app.tools.approval import OUTCOME_HITL_OFF, OUTCOME_WHITELISTED
from app.tools.base import CompositeToolExecutor


class _Call:
    def __init__(self, call_id: str, name: str):
        self.id = call_id
        self.name = name


class _Message:
    def __init__(self, tool_calls):
        self.role = "assistant"
        self.tool_calls = tool_calls


def _messages(*calls):
    return [_Message(list(calls))]


@pytest.mark.asyncio
async def test_an_anonymous_surface_records_without_a_user(caplog):
    """The public embed has no identity to attribute an audit row to, and
    ``audit_logs.user_id`` is NOT NULL. The information still has to land
    somewhere rather than being dropped for want of a name."""
    with caplog.at_level("INFO"):
        await record_chat_gate_outcomes(
            _messages(_Call("call_1", "search_entities")),
            CompositeToolExecutor(),
            outcomes={"call_1": OUTCOME_WHITELISTED},
            user_id=None,
            hitl_enabled=True,
        )

    assert any(r.message == "chat_tool_gate_outcomes" for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, uuid.uuid4()], ids=["anonymous", "signed-in"])
async def test_a_turn_whose_gate_was_off_records_nothing(caplog, monkeypatch, user_id):
    """Every turn of an agent that does not ask - every agent of an upgraded
    install - would otherwise add an audit row (or a log line) saying only that
    the gate was off. Nothing is recorded, and no session is opened."""

    def _no_session():
        raise AssertionError("a gate-off turn must not open an audit session")

    monkeypatch.setattr("app.core.database.async_session", _no_session)
    with caplog.at_level("INFO"):
        await record_chat_gate_outcomes(
            _messages(_Call("call_1", "delete_entity")),
            CompositeToolExecutor(),
            outcomes={},
            user_id=user_id,
            hitl_enabled=False,
            conversation_id=uuid.uuid4(),
        )

    assert not any(r.message == "chat_tool_gate_outcomes" for r in caplog.records)
    assert not any(r.message == "Could not record chat gate outcomes" for r in caplog.records)


@pytest.mark.asyncio
async def test_a_gated_turn_leaves_no_outcome_blank_by_accident(caplog):
    """With the switch on, an unrecorded call means the gate genuinely said
    nothing about it — which must not be silently relabelled as "switch off"."""
    with caplog.at_level("INFO"):
        await record_chat_gate_outcomes(
            _messages(_Call("call_1", "delete_entity")),
            CompositeToolExecutor(),
            outcomes={},
            user_id=None,
            hitl_enabled=True,
        )

    record = next(r for r in caplog.records if r.message == "chat_tool_gate_outcomes")
    assert record.calls[0]["gate_outcome"] is None


@pytest.mark.asyncio
async def test_outcomes_are_keyed_by_call_id_not_tool_name(monkeypatch):
    """A turn can call the same tool twice with different verdicts — one
    approved, one rejected — so keying by name would report the wrong one."""
    recorded: list[dict] = []

    async def _capture(messages, composite, **kwargs):
        recorded.append(kwargs["outcomes"])

    monkeypatch.setattr(
        "app.services.tool_tracking.record_chat_gate_outcomes", _capture
    )

    from app.services.hitl_whitelist import drain_gate_bookkeeping

    outcomes = {"call_1": OUTCOME_WHITELISTED, "call_2": OUTCOME_HITL_OFF}
    await drain_gate_bookkeeping(
        None,
        _messages(_Call("call_1", "t"), _Call("call_2", "t")),
        CompositeToolExecutor(),
        outcomes=outcomes,
        stale=set(),
        user_id=None,
        hitl_enabled=True,
    )
    assert recorded == [outcomes]


@pytest.mark.asyncio
async def test_a_stale_approval_is_revoked_after_a_chat_turn(monkeypatch):
    """The gap this closes. The gate drops the stale preference from the run's
    in-memory map, but only a write makes the user's own list stop claiming
    they always allow a tool that just prompted them."""
    calls: list[dict] = []

    async def _fake_invalidate(db, *, user_id, tool_keys, reason):
        calls.append({"user_id": user_id, "tool_keys": set(tool_keys), "reason": reason})
        return []

    monkeypatch.setattr(
        "app.services.tool_approval_service.invalidate_stale_approvals",
        _fake_invalidate,
    )

    class _Db:
        async def commit(self):
            return None

    from app.services.hitl_whitelist import drain_gate_bookkeeping

    user_id = uuid.uuid4()
    await drain_gate_bookkeeping(
        _Db(),
        _messages(_Call("call_1", "delete_entity")),
        CompositeToolExecutor(),
        outcomes={},
        stale={"delete_entity"},
        user_id=str(user_id),
        hitl_enabled=True,
    )

    assert len(calls) == 1
    assert calls[0]["tool_keys"] == {"delete_entity"}
    assert calls[0]["user_id"] == user_id, "the string id must be normalised"
    assert calls[0]["reason"] == "tool_changed"


@pytest.mark.asyncio
async def test_bookkeeping_failure_never_costs_the_user_their_answer(monkeypatch):
    """Recording is bookkeeping. A turn that already produced an answer must
    not fail because the answer could not be filed."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(
        "app.services.tool_tracking.record_chat_gate_outcomes", _boom
    )
    monkeypatch.setattr(
        "app.services.tool_approval_service.invalidate_stale_approvals", _boom
    )

    from app.services.hitl_whitelist import drain_gate_bookkeeping

    class _Db:
        async def commit(self):
            return None

    await drain_gate_bookkeeping(
        _Db(),
        _messages(_Call("call_1", "t")),
        CompositeToolExecutor(),
        outcomes={"call_1": OUTCOME_WHITELISTED},
        stale={"t"},
        user_id=str(uuid.uuid4()),
        hitl_enabled=True,
    )


def test_every_chat_surface_fills_both_sinks():
    """The regression guard. Chat gating shipped without these two arguments,
    which is precisely why nothing complained — the gate works, only its
    bookkeeping was missing."""
    import inspect

    from app.api.v1 import public_chat
    from app.services import agent_chat, general_chat

    for module, needs_stale in (
        (agent_chat, True),
        (general_chat, True),
        # No standing approval can apply without a user, so none can go stale.
        (public_chat, False),
    ):
        source = inspect.getsource(module)
        assert "outcome_sink=" in source, f"{module.__name__} records no outcome"
        assert "drain_gate_bookkeeping" in source, f"{module.__name__} never drains"
        if needs_stale:
            assert "stale_sink=" in source, f"{module.__name__} drops stale approvals"

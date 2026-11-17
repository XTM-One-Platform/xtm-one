"""The per-tool gate reads ``approve_tool_calls``, never ``human_in_the_loop``.

Scheduled, webhook and integration-event runs all go through
``execute_assignment_run``; chat and every channel bot (Slack, Teams, …) through
``run_agent_chat``. So pinning these four entry points to the resolver covers
every surface an upgraded install could start pausing on.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services.hitl_whitelist import (
    build_approval_gate,
    gate_off_outcome,
    resolve_approve_tool_calls,
)
from app.tools.approval import OUTCOME_INSTALL_DEFAULT_OFF


class _Db:
    def __init__(self, default=None, *, fail=False):
        self.default = default
        self.fail = fail
        self.queried = False

    async def scalar(self, _stmt):
        self.queried = True
        if self.fail:
            raise RuntimeError("database unavailable")
        return self.default

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def rollback(self):
        raise AssertionError("must not roll back the caller's session")


@pytest.mark.parametrize("own", [True, False])
async def test_the_agents_own_value_wins_without_a_query(own):
    db = _Db(default=not own)
    assert await resolve_approve_tool_calls(db, SimpleNamespace(approve_tool_calls=own)) is own
    assert db.queried is False


@pytest.mark.parametrize(("default", "expected"), [(False, False), (True, True), (None, False)])
async def test_unset_follows_the_install_default(default, expected):
    """``None`` is no settings row yet, which is created FALSE: approvals are opt-in."""
    agent = SimpleNamespace(approve_tool_calls=None)
    assert await resolve_approve_tool_calls(_Db(default), agent) is expected


async def test_legacy_hitl_does_not_turn_the_gate_on():
    agent = SimpleNamespace(approve_tool_calls=None, human_in_the_loop=True)
    assert await resolve_approve_tool_calls(_Db(False), agent) is False


async def test_an_unreadable_default_fails_closed():
    assert await resolve_approve_tool_calls(_Db(fail=True), None) is True


async def test_a_disabled_gate_pauses_nothing_a_tool_that_always_asks_included():
    """Approval is opt-in: an agent not set to ask never pauses."""

    class _ApprovalsAvailable:
        async def scalar(self, _stmt):
            return True

    from app.tools.base import ToolCall, ToolDefinition

    tools = {
        "search": ToolDefinition(name="search", description=""),
        "act": ToolDefinition(name="act", description="", always_ask_argument="submit"),
    }
    composite = SimpleNamespace(get_tool_definition=tools.get)
    sink: dict[str, str] = {}
    gate = await build_approval_gate(
        _ApprovalsAvailable(), composite, user_id=None, hitl_enabled=False, outcome_sink=sink
    )

    assert gate(ToolCall(id="c1", name="search", arguments={})) is None
    assert gate(ToolCall(id="c2", name="act", arguments={"submit": False})) is None
    assert gate(ToolCall(id="c3", name="act", arguments={"submit": True})) is None
    assert sink == {}


def _entry_points():
    from app.api.v1 import public_chat
    from app.services import agent_chat, assignment_engine, general_chat

    return {
        "runs": inspect.getsource(assignment_engine.execute_assignment_run),
        "chat": inspect.getsource(agent_chat.run_agent_chat),
        "general_assistant": inspect.getsource(general_chat),
        "public_embed": inspect.getsource(public_chat),
    }


@pytest.mark.parametrize("surface", ["runs", "chat", "general_assistant", "public_embed"])
def test_every_entry_point_resolves_the_gate_from_the_new_switch(surface):
    source = _entry_points()[surface]
    assert "resolve_approve_tool_calls(" in source
    assert 'getattr(agent, "human_in_the_loop", True)' not in source


def test_an_install_default_off_is_named_as_such_in_the_trail():
    """FR-020: the administrator's install-wide choice, not "the agent is ungated"."""
    assert gate_off_outcome(SimpleNamespace(approve_tool_calls=None)) == OUTCOME_INSTALL_DEFAULT_OFF
    assert gate_off_outcome(None) == OUTCOME_INSTALL_DEFAULT_OFF
    assert gate_off_outcome(SimpleNamespace(approve_tool_calls=False)) is None

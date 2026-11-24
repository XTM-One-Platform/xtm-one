"""A fleet's approval gates pause their tool calls outside assignment runs too (#10140, slice C of #10000)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services import fleet_autonomy
from app.services.fleet_autonomy import install_fleet_autonomy_gate
from app.tools.base import HumanInputRequestedError

_TICKET_GATE = {
    "key": "ticket.create",
    "action": "Create a ticket",
    "module": "xtm_one",
    "impact": "low",
    "tools": [{"name": "create_jira_issue"}],
}
_TICKET = SimpleNamespace(id="c-ticket", name="create_jira_issue", arguments={"summary": "fix"})
_READ = SimpleNamespace(id="c-read", name="search_jira_issues", arguments={})


class _Composite:
    def __init__(self, inner=None):
        self.approval_gate = inner

    def set_approval_gate(self, gate):
        self.approval_gate = gate


def _paused(gate, call) -> bool:
    try:
        gate(call)
    except HumanInputRequestedError:
        return True
    return False


@pytest.fixture
def fleet(monkeypatch):
    state = {"autonomy": ("full_gates", [_TICKET_GATE]), "kill": None, "enforced": True}

    async def _for_run(db, fleet_id):
        return None if fleet_id is None else state["autonomy"]

    async def _enforced(db: object, fleet_id: object) -> bool:
        return bool(state["enforced"])

    monkeypatch.setattr(fleet_autonomy, "fleet_enforces_at_tool_level", _enforced)

    async def _kill(db):
        return state["kill"]

    monkeypatch.setattr(fleet_autonomy, "fleet_autonomy_for_run", _for_run)
    import app.services.hitl_whitelist as hitl

    monkeypatch.setattr(hitl, "resolve_platform_kill_switch", _kill)
    return state


async def test_a_gated_call_pauses_and_the_agents_own_gate_still_decides_the_rest(fleet):
    seen: list[str] = []
    composite = _Composite(lambda call: seen.append(call.id))
    assert await install_fleet_autonomy_gate(None, composite, "fleet-1") is True
    assert _paused(composite.approval_gate, _TICKET)
    assert seen == []
    composite.approval_gate(_READ)
    assert seen == ["c-read"]


async def test_a_call_the_person_approved_runs(fleet):
    composite = _Composite()
    await install_fleet_autonomy_gate(None, composite, "fleet-1", approved_call_ids={"c-ticket"})
    composite.approval_gate(_TICKET)


async def test_a_level_that_lets_the_action_run_installs_nothing(fleet):
    fleet["autonomy"] = ("low", [_TICKET_GATE])
    inner = object()
    composite = _Composite(inner)
    assert await install_fleet_autonomy_gate(None, composite, "fleet-1") is False
    assert composite.approval_gate is inner


async def test_a_fleet_without_gates_or_without_a_fleet_changes_nothing(fleet):
    inner = object()
    composite = _Composite(inner)
    assert await install_fleet_autonomy_gate(None, composite, None) is False
    fleet["autonomy"] = ("full_gates", [])
    assert await install_fleet_autonomy_gate(None, composite, "fleet-1") is False
    assert composite.approval_gate is inner


async def test_no_tool_level_pause_unless_an_administrator_opted_the_fleet_in(fleet: dict[str, object]) -> None:
    """#1467: a fleet's gates are instructions by default; the tool call runs."""
    fleet["enforced"] = False
    seen: list[str] = []

    def inner(call: SimpleNamespace) -> None:
        seen.append(call.id)

    composite = _Composite(inner)  # type: ignore[no-untyped-call]
    assert await install_fleet_autonomy_gate(None, composite, "fleet-1") is False
    assert composite.approval_gate is inner
    composite.approval_gate(_TICKET)
    assert seen == ["c-ticket"]


async def test_the_platform_kill_switch_wins(fleet):
    from app.tools.approval import OUTCOME_APPROVALS_DISABLED

    fleet["kill"] = OUTCOME_APPROVALS_DISABLED
    inner = object()
    composite = _Composite(inner)
    assert await install_fleet_autonomy_gate(None, composite, "fleet-1") is False
    assert composite.approval_gate is inner


def test_every_chat_turn_installs_the_fleet_gate_after_the_agents_own():
    """run_agent_chat carries chat, channel, consult, sub-agent and work-item turns."""
    from app.services import agent_chat

    source = inspect.getsource(agent_chat.run_agent_chat)
    own = source.index("composite.set_approval_gate(_chat_gate)")
    fleet_gate = source.index("await install_fleet_autonomy_gate(")
    assert own < fleet_gate
    assert source.count("await install_fleet_autonomy_gate(") == 1
    assert "if not _skips_tool_approval and getattr(_run_fleet, \"fleet_id\", None) is not None:" in source

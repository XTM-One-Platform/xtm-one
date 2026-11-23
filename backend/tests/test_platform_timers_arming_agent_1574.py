"""#1574: a timer is heard when a listener of the ARMING agent takes it, never a foreign one alone."""

from __future__ import annotations

import pathlib
import uuid
from datetime import timedelta

import pytest

from app.models.platform_timer import TIMER_FIRED
from app.services import platform_timers
from app.services.builtin_tools.timers import build_timer_tools
from app.services.platform_timers import TIMER_DUE_EVENT, timer_due_data
from tests.test_platform_timers import _NOW, _live, _others, _patch, _Session, _sub, _timer


def _foreign_only(monkeypatch, timer, *, own_in_scope=False):
    """Only another agent's (fleet-scoped) listener takes the key; the arming agent's does not."""
    other = uuid.uuid4()
    theirs = _sub(other, timer.user_id, {"correlation_key": {"starts_with": "x:"}})
    mine = _sub(timer.agent_id, timer.user_id, {"correlation_key": {"starts_with": "mine:"}})

    async def _listeners(data):
        assert data["agent_id"] == str(timer.agent_id)
        return ([mine, theirs] if own_in_scope else [theirs]), [theirs]

    async def _names(ids):
        assert ids == [str(other)]
        return ["Supervision desk"]

    monkeypatch.setattr(platform_timers, "timer_listeners", _listeners)
    monkeypatch.setattr(platform_timers, "agent_names", _names)
    _others(monkeypatch, ())
    return other


@pytest.mark.asyncio
@pytest.mark.parametrize("own_in_scope", [False, True])
async def test_a_key_only_another_agent_hears_is_warned_naming_that_agent(monkeypatch, own_in_scope):
    timer = _timer(correlation_key="x:1")
    _foreign_only(monkeypatch, timer, own_in_scope=own_in_scope)

    warning = await platform_timers.unheard_warning(platform_timers.serialize_timer(timer), user_id=str(timer.user_id))

    assert warning is not None and warning.startswith("Warning: only another agent hears `x:1`")
    assert "Supervision desk" in warning and "nothing of yours runs at due time" in warning


@pytest.mark.asyncio
async def test_an_own_listener_beside_a_foreign_one_gives_no_warning(monkeypatch):
    timer = _timer(correlation_key="x:1")
    mine = _sub(timer.agent_id, timer.user_id, {"correlation_key": {"starts_with": "x:"}})
    theirs = _sub(uuid.uuid4(), timer.user_id, {"correlation_key": {"starts_with": "x:"}})

    async def _listeners(_data):
        return [mine, theirs], [mine, theirs]

    monkeypatch.setattr(platform_timers, "timer_listeners", _listeners)
    _others(monkeypatch, ())

    serialized = platform_timers.serialize_timer(timer)
    assert await platform_timers.unheard_warning(serialized, user_id=str(timer.user_id)) is None


@pytest.mark.asyncio
async def test_a_fire_heard_only_by_another_agent_says_so_and_is_still_raised_once(monkeypatch, caplog):
    timer = _timer(correlation_key="x:1")
    session = _Session([timer])
    published: list = []
    _patch(monkeypatch, session, published)
    _foreign_only(monkeypatch, timer)

    with caplog.at_level("WARNING", logger="app.services.platform_timers"):
        assert await platform_timers.fire_due_timers(now=_NOW) == 1

    # Delivery to the other listener is unchanged: one event, the fire committed first.
    assert len(published) == 1 and published[0][3] == 1
    assert timer.state == TIMER_FIRED
    assert "heard only by other agents (Supervision desk)" in timer.outcome
    assert "nothing of the arming agent ran" in timer.outcome
    assert "fired unheard" not in caplog.text


@pytest.mark.asyncio
async def test_the_arm_tool_promises_a_run_only_from_the_agents_own_listeners(monkeypatch):
    from app.tools.base import ToolCall

    timer = _timer(due_at=_NOW + timedelta(days=1), correlation_key="x:1")

    async def _arm(**_kwargs):
        return platform_timers.serialize_timer(timer), None

    monkeypatch.setattr(platform_timers, "arm_timer", _arm)
    _foreign_only(monkeypatch, timer)
    _defs, executor = build_timer_tools(str(timer.user_id), str(timer.agent_id))
    call = ToolCall(id="1", name="arm_timer", arguments={"correlation_key": "x:1", "due_at": "2026-11-06T12:00:00Z"})

    out = str((await executor.execute_tool(call)).content)

    assert "a run of yours starts only if one of your own platform_event assignments" in out
    assert "only another agent hears `x:1`" in out


def test_the_ctem_loop_seeds_its_own_reminder_listener():
    from app.agents.builtin_agents import (
        CTEM_F1_ASSIGNMENTS,
        CTEM_LOOP_LEGACY_TIMER_PREFIXES,
        CTEM_LOOP_REMINDER,
        CTEM_LOOP_REMINDER_PREFIX,
    )
    from app.agents.gate_answered import INHERIT_SWITCH_FROM_KEY

    listener = next(a for a in CTEM_F1_ASSIGNMENTS if a["name"] == CTEM_LOOP_REMINDER)
    config = listener["trigger_config"]
    assert listener["trigger_type"] == "platform_event"
    assert config["event"] == TIMER_DUE_EVENT and config["scope"] == "own"
    prefixes = config["filter"]["correlation_key"]["starts_with"]
    assert prefixes[0] == CTEM_LOOP_REMINDER_PREFIX == "ctem-loop-reminder:"
    assert set(CTEM_LOOP_LEGACY_TIMER_PREFIXES) <= set(prefixes)
    assert listener[INHERIT_SWITCH_FROM_KEY] == "Case opened or reopened"
    persona = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app/agents/seed_data/agent_persona/ctem-threat-response-orchestrator.md"
    ).read_text(encoding="utf-8")
    assert "`ctem-loop-reminder:`" in persona and CTEM_LOOP_REMINDER in persona


@pytest.mark.asyncio
async def test_a_ctem_loop_reminder_is_taken_by_the_seeded_listener(monkeypatch):
    from app.agents.builtin_agents import CTEM_F1_ASSIGNMENTS, CTEM_LOOP_REMINDER

    listener = next(a for a in CTEM_F1_ASSIGNMENTS if a["name"] == CTEM_LOOP_REMINDER)
    timer = _timer(correlation_key="ctem-loop-reminder:case-1")
    mine = _sub(timer.agent_id, timer.user_id, listener["trigger_config"]["filter"])
    _live(monkeypatch, [mine])

    serialized = platform_timers.serialize_timer(timer)
    assert await platform_timers.unheard_warning(serialized, user_id=str(timer.user_id)) is None
    for legacy in ("ctem-gate-watch:case-1", "ctem-loop-watch:case-1"):
        old = _timer(agent_id=timer.agent_id, user_id=timer.user_id, correlation_key=legacy)
        _in_scope, taking = await platform_timers.timer_listeners(timer_due_data(old))
        assert taking == [mine], legacy

"""Tests for agent self-fan-out (spawning parallel background copies of self).

Before this feature an agent could only delegate to *other* (peer) agents:

* ``build_sub_agent_tools`` returned ``None`` when ``handover_agents`` was empty.
* the cycle guard rejected any target already in the visited chain, and the
  parent agent is always pre-seeded into that chain — so ``target == self`` was
  always refused.

Self-fan-out adds an explicit, bounded carve-out:

* ``build_sub_agent_tools(self_agent=..., allow_self_fanout=True)`` builds the
  tools even with no peers, with ``"self"`` (or the agent's own id/slug) as a
  valid target.
* ``spawn_background_task("self", ...)`` skips the peer cycle check and forwards
  ``allow_self=True`` to ``create_sub_agent_task``.
* ``create_sub_agent_task(allow_self=True)`` bypasses the cycle guard *only* for
  the direct self case (``target == parent``); every other in-chain target is
  still a real cycle and rejected.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.builtin_tools import (
    build_sub_agent_tools,
    format_self_fanout_prompt,
)
from app.services.sub_agent import HandoverCycleError, create_sub_agent_task


def _make_agent(name: str = "Solo") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower(),
        handover_types={},
    )


def _make_fake_sub_task() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status="pending")


def _build_self_spawn():
    """Build the spawn handler for an agent with NO peers but self-fan-out on."""
    agent = _make_agent()
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(agent.id),
        handover_agents=[],  # no peers — self-fan-out only
        parent_run_id=str(uuid.uuid4()),
        self_agent=agent,
        allow_self_fanout=True,
    )
    assert result is not None
    _defs, executor = result
    return agent, executor._handlers["spawn_background_task"]


# ── Tool building ────────────────────────────────────────────────────────────


def test_self_fanout_builds_tools_without_peers():
    """With self-fan-out enabled, tools are built even with an empty peer list."""
    _agent, spawn = _build_self_spawn()
    assert spawn is not None


def test_no_peers_and_no_self_fanout_returns_none():
    """The legacy guard still holds: no peers + no self-fan-out -> no tools."""
    agent = _make_agent()
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(agent.id),
        handover_agents=[],
    )
    assert result is None


def test_self_fanout_ignored_without_self_agent():
    """allow_self_fanout alone (no self_agent) does not fabricate a target."""
    agent = _make_agent()
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(agent.id),
        handover_agents=[],
        allow_self_fanout=True,
        self_agent=None,
    )
    assert result is None


def test_spawn_description_mentions_self_when_enabled():
    agent = _make_agent()
    defs, _ = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(agent.id),
        handover_agents=[],
        self_agent=agent,
        allow_self_fanout=True,
    )
    spawn_def = next(d for d in defs if d.name == "spawn_background_task")
    assert '"self"' in spawn_def.description
    agent_id_desc = spawn_def.input_schema["properties"]["agent_id"]["description"]
    assert "self" in agent_id_desc.lower()


def test_spawn_description_omits_self_when_only_peers():
    peer = _make_agent("Peer")
    agent = _make_agent("Main")
    defs, _ = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(agent.id),
        handover_agents=[peer],
    )
    spawn_def = next(d for d in defs if d.name == "spawn_background_task")
    assert '"self"' not in spawn_def.description


# ── spawn_background_task("self", ...) ──────────────────────────────────────


async def test_spawn_self_forwards_allow_self_and_skips_cycle_check():
    """Spawning "self" reaches create_sub_agent_task with allow_self=True and the
    agent's own id as target — despite the parent being in the visited chain."""
    agent, spawn = _build_self_spawn()

    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn("self", "process batch 1 of 5")

    assert "spawned successfully" in msg
    assert "yourself" in msg
    create_mock.assert_awaited_once()
    kwargs = create_mock.await_args.kwargs
    assert kwargs.get("allow_self") is True
    assert kwargs.get("target_agent_id") == str(agent.id)


async def test_spawn_self_by_own_id_is_recognized():
    """Passing the agent's own id (not the literal "self") also self-fans-out."""
    agent, spawn = _build_self_spawn()

    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(agent.id), "batch")

    assert "spawned successfully" in msg
    assert create_mock.await_args.kwargs.get("allow_self") is True


async def test_spawn_unknown_target_without_peers_is_rejected():
    """A non-self, unknown target with no peers is not silently treated as self."""
    _agent, spawn = _build_self_spawn()
    msg = await spawn(str(uuid.uuid4()), "task")
    assert "not available for background tasks" in msg


# ── create_sub_agent_task cycle-guard carve-out ─────────────────────────────


class _SentinelError(Exception):
    """Marker proving execution passed the cycle guard into the DB section."""


async def test_create_self_fanout_bypasses_cycle_guard():
    """allow_self=True with target == parent must get PAST the cycle guard
    (it then opens a DB session, which we stub to raise a sentinel)."""
    parent = str(uuid.uuid4())
    with (
        patch("app.core.database.async_session", side_effect=_SentinelError),
        pytest.raises(_SentinelError),
    ):
        await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=parent,
            target_agent_id=parent,
            task_description="self batch",
            allow_self=True,
        )


async def test_create_self_target_without_allow_self_raises():
    """Without allow_self, target == parent is still a cycle (raises early)."""
    parent = str(uuid.uuid4())
    with pytest.raises(HandoverCycleError):
        await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=parent,
            target_agent_id=parent,
            task_description="x",
            allow_self=False,
        )


async def test_create_allow_self_does_not_relax_other_cycles():
    """allow_self only excuses target == parent — a different in-chain agent is
    still rejected as a genuine cycle."""
    parent, other = str(uuid.uuid4()), str(uuid.uuid4())
    with pytest.raises(HandoverCycleError):
        await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=parent,
            target_agent_id=other,
            task_description="x",
            allow_self=True,
            visited_agents=[other],
        )


# ── format_self_fanout_prompt ───────────────────────────────────────────────


def test_self_fanout_prompt_adds_header_without_peers():
    text = format_self_fanout_prompt(has_peers=False)
    assert "## Agent Delegation" in text
    assert "Parallel self-execution" in text
    assert '"self"' in text


def test_self_fanout_prompt_omits_header_with_peers():
    text = format_self_fanout_prompt(has_peers=True)
    assert "## Agent Delegation" not in text
    assert "Parallel self-execution" in text


def test_self_fanout_prompt_is_conservative():
    """Guard against regressing to eager phrasing that made agents over-spawn:
    the guidance must steer toward doing the work inline and away from spawning
    for single/small tasks."""
    text = format_self_fanout_prompt(has_peers=False).lower()
    assert "sparingly" in text
    assert "do the work yourself" in text
    # Must explicitly discourage spawning for a single/normal/small task.
    assert "do not spawn" in text
    assert "single" in text
    assert "keep working on anything independent" in text


def test_spawn_description_is_conservative_about_self():
    """The self-fan-out tool description must tell the agent to use it sparingly
    and prefer doing small/normal tasks itself."""
    agent = _make_agent()
    defs, _ = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(agent.id),
        handover_agents=[],
        self_agent=agent,
        allow_self_fanout=True,
    )
    spawn_def = next(d for d in defs if d.name == "spawn_background_task")
    desc = spawn_def.description.lower()
    assert "sparingly" in desc
    assert "do the work yourself" in desc
    assert "keep working" in desc
    assert "do not wait unless you are blocked" in desc

"""The boot-time seed must not revert an operator's choice on a built-in.

An administrator may set ``approve_tool_calls`` on a built-in agent through the
API, audited; the docs promise the choice survives restarts. The HITL columns
are not synced either. Nothing but these tests stops a contributor "completing"
the full sync and silently reverting all of it on the next boot.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.agents.seed import _sync_agent_fields, seed_builtin_assignments
from app.models.agent import Agent

# ---------------------------------------------------------------------------
# Agent-level: _sync_agent_fields must leave the column alone
# ---------------------------------------------------------------------------


def _seed_entry() -> dict:
    """A minimal built-in agent seed entry.

    Deliberately synthetic rather than a real ``BUILTIN_AGENTS`` row: this
    asserts something about the sync function, and pinning it to a shipped
    agent would make the test fail the day that agent is renamed.
    """
    return {
        "slug": "test-builtin-agent",
        "name": "Test built-in agent",
        "description": "Synthetic entry for the HITL preservation test.",
        "persona": "inline persona text, not a slug",
        "builtin_tool_ids": ["code_interpreter"],
    }


@pytest.mark.parametrize("operator_choice", [False, True])
def test_sync_agent_fields_preserves_human_in_the_loop(operator_choice: bool):
    """A value written directly on a built-in agent survives the seed sync.

    Both directions matter. ``False`` is the operator turning gating off so an
    unattended agent keeps working; ``True`` is the compliance default they may
    have deliberately restored. Neither may be silently rewritten at boot.
    """
    agent = Agent(slug="test-builtin-agent", name="Test built-in agent")
    agent.human_in_the_loop = operator_choice
    agent.approve_tool_calls = operator_choice
    agent.human_in_the_loop_prompt = "Ask me before anything irreversible."

    _sync_agent_fields(agent, _seed_entry(), anthropic_enabled=False)

    assert agent.human_in_the_loop is operator_choice, (
        "seed sync overwrote agents.human_in_the_loop — built-in agents cannot "
        "be edited via the API in XTM One mode, so writing the column is an "
        "operator's only route to running one unattended, and this sync must "
        "not revert it"
    )
    assert agent.human_in_the_loop_prompt == "Ask me before anything irreversible.", (
        "seed sync overwrote agents.human_in_the_loop_prompt — the custom "
        "prompt is part of the same decision"
    )
    assert agent.approve_tool_calls is operator_choice, (
        "seed sync overwrote an administrator's audited approval choice"
    )
    # Guard the guard: if the entry stopped being applied at all, the
    # assertions above would pass vacuously.
    assert agent.name == "Test built-in agent"
    assert agent.builtin_tool_ids == ["code_interpreter"]


def test_no_builtin_agent_seed_entry_declares_human_in_the_loop():
    """Declaring it in seed data would be a silent no-op.

    ``_sync_agent_fields`` never reads the key, so an entry setting it would
    look authoritative in review and do nothing at runtime. If a built-in ever
    genuinely needs to ship ungated, wiring it up is a decision to make
    explicitly — not something to leave looking done.
    """
    offenders = [
        a["slug"] for a in BUILTIN_AGENTS if "human_in_the_loop" in a
    ]
    assert not offenders, (
        f"built-in agent seed entries declare human_in_the_loop: {offenders}. "
        "The sync ignores the key, so this has no effect — either wire it into "
        "_sync_agent_fields deliberately or drop it"
    )


# ---------------------------------------------------------------------------
# Assignment-level: the boot sync must leave the tri-state alone
# ---------------------------------------------------------------------------


class _Scalars:
    def __init__(self, items: list):
        self._items = items

    def all(self) -> list:
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    """Minimal stand-in for a SQLAlchemy ``Result``."""

    def __init__(self, *, scalar=None, items: list | None = None):
        self._scalar = scalar
        self._items = items if items is not None else ([scalar] if scalar else [])

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self) -> _Scalars:
        return _Scalars(self._items)


@pytest.mark.asyncio
async def test_seed_sync_preserves_assignment_human_in_the_loop(monkeypatch):
    """An explicit FALSE on a built-in agent's assignment survives boot.

    This is the narrower of the two workarounds — it un-gates one nightly
    automation rather than an agent's chat as well — and it is the one most at
    risk, because the surrounding field list re-enables a disabled assignment
    on purpose. Syncing HITL the same way would fight the operator
    instead of whatever disabled the row.
    """
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.slug = "ctem-discovery-analyst"
    agent.user_id = None
    agent.is_company_managed = True

    existing = MagicMock()
    existing.enabled = True
    existing.user_id = None
    # The operator's decision: this automation runs unattended.
    existing.human_in_the_loop = False
    existing.human_in_the_loop_prompt = None

    seed_entry = {
        "agent_slug": agent.slug,
        "name": "Weekly discovery assessment",
        "description": "Test entry",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 * * 1"},
        "prompt": "Assess the discovery stage.",
        "enabled": True,
    }
    monkeypatch.setattr(
        "app.agents.seed.BUILTIN_ASSIGNMENTS", [seed_entry], raising=True
    )

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _Result(scalar=agent),  # phase 1: agent lookup for stale sweep
            _Result(items=[]),  # phase 1: no stale assignments
            _Result(scalar=agent),  # phase 2: agent lookup
            _Result(scalar=existing, items=[existing]),  # phase 2: existing row
        ]
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()

    inserted = await seed_builtin_assignments(db, platform_mode="xtm_one")

    assert inserted == 0  # updated in place, nothing inserted
    assert existing.human_in_the_loop is False, (
        "seed sync overwrote assignments.human_in_the_loop — it is a nullable "
        "tri-state an operator sets deliberately, and assignments on built-in "
        "agents cannot be edited via the API, so the column is the only place "
        "that decision can live"
    )
    # Guard the guard: the sync must actually have run over this row.
    assert existing.prompt == "Assess the discovery stage."


def test_no_builtin_assignment_seed_entry_declares_human_in_the_loop():
    """Same silent no-op as the agent case, on the assignment field list."""
    offenders = [
        f"{a['agent_slug']}/{a['name']}"
        for a in BUILTIN_ASSIGNMENTS
        if "human_in_the_loop" in a
    ]
    assert not offenders, (
        f"built-in assignment seed entries declare human_in_the_loop: "
        f"{offenders}. The sync ignores the key, so this has no effect — "
        "either wire it in deliberately or drop it"
    )

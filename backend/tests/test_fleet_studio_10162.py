"""Fleet page "Modify with AI": the conversation's fleet binding (#10162, slice S2a).

``conversations.fleet_id`` binds a chat to the fleet it changes, the twin of the
studio bindings: each turn resolves it as the caller sees the fleet and puts
the fleet's identity in the system prompt with the platform tools guaranteed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.fleet_studio import (
    STUDIO_TOOL_IDS,
    FleetStudioContext,
    format_fleet_studio_prompt,
    load_fleet_studio_context,
)


def _ctx(*, can_edit: bool = True, builtin: bool = False, members: int = 2) -> FleetStudioContext:
    fleet = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-00000000f1ee"),
        name="CEO Office",
        autonomy_level="full_gates",
        is_builtin=builtin,
    )
    roster = tuple((f"Agent {i}", "orchestrator" if i == 0 else "member") for i in range(members))
    return FleetStudioContext(
        fleet=fleet,  # type: ignore[arg-type]
        can_edit=can_edit,
        workspace_name="Office",
        orchestrator_name="Agent 0",
        members=roster,
        member_count=members,
    )


def test_prompt_names_the_fleet_and_never_asks_which() -> None:
    text = format_fleet_studio_prompt(_ctx())
    assert '"CEO Office"' in text
    assert "fleet_id=00000000-0000-0000-0000-00000000f1ee" in text
    assert "never ask which fleet" in text
    assert "autonomy level full_gates" in text
    assert "Orchestrator: Agent 0." in text
    assert "Agent 1 (member)" in text


def test_prompt_says_read_only_when_the_user_cannot_manage() -> None:
    assert "READ-ONLY" in format_fleet_studio_prompt(_ctx(can_edit=False))
    assert "READ-ONLY" not in format_fleet_studio_prompt(_ctx(can_edit=True))


def test_prompt_offers_duplicate_on_a_builtin_fleet() -> None:
    assert "built-in fleet" in format_fleet_studio_prompt(_ctx(builtin=True))
    assert "built-in fleet" not in format_fleet_studio_prompt(_ctx(builtin=False))


def test_prompt_counts_members_past_the_roster_shown() -> None:
    ctx = _ctx(members=3)
    ctx = FleetStudioContext(
        fleet=ctx.fleet,
        can_edit=True,
        workspace_name="Office",
        orchestrator_name=None,
        members=ctx.members[:1],
        member_count=3,
    )
    assert "and 2 more" in format_fleet_studio_prompt(ctx)


def test_turn_guarantees_the_platform_tools() -> None:
    assert _ctx().tool_ids == STUDIO_TOOL_IDS == ("platform",)


@pytest.mark.asyncio
async def test_unbound_conversation_has_no_fleet_context() -> None:
    assert await load_fleet_studio_context(None, user_id=uuid.uuid4(), fleet_id=None) is None  # type: ignore[arg-type]

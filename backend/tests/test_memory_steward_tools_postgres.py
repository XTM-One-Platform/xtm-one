"""Memory Steward tools on PostgreSQL (#8258): the Concierge only, counts only.

Outside a Concierge run (any login, an administrator's included), and inside
one while ``CONCIERGE`` is off, every tool refuses and nothing is read. Inside
one, the tools list the readable lanes, run a pass and count what it applied,
and never return memory content. Who is the Concierge is level 1's real rule.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from app.services.builtin_tools.memory_steward import build_memory_steward_tools
from app.services.concierge import identity
from app.tools.base import ToolCall
from tests.memory_steward_pass_world import pass_world, seed_owner_lane

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))


async def test_the_tools_refuse_anything_but_a_concierge_run(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await seed_owner_lane(world)
        _defs, executor = build_memory_steward_tools()
        principal = identity.ConciergePrincipal(agent_id=world.agent, role="memory_steward")

        for inside, flag in ((False, True), (True, False)):
            monkeypatch.setattr(identity, "concierge_enabled", lambda flag=flag: flag)
            with identity.acting_as_concierge(principal) if inside else contextlib.nullcontext():
                for name, arguments in (
                    ("list_memory_lanes", {}),
                    ("consolidate_memory_lane", {"agent_id": str(world.agent), "user_id": str(world.owner)}),
                    ("list_memory_consolidations", {}),
                ):
                    result = await _call(executor, name, **arguments)
                    assert (result.is_error, result.refused, result.content) == (
                        True,
                        True,
                        "Error: the memory tools are reserved to the Platform Concierge.",
                    ), (name, inside, flag)

        assert world.model_calls == []
        assert world.reads == []


async def test_a_concierge_run_lists_passes_and_counts_without_content(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await seed_owner_lane(world)
        world.readable.discard((world.agent, world.other))
        _defs, executor = build_memory_steward_tools()
        principal = identity.ConciergePrincipal(agent_id=world.agent, role="memory_steward")
        lane = {"agent_id": str(world.agent), "user_id": str(world.owner)}

        with identity.acting_as_concierge(principal):
            lanes = await _call(executor, "list_memory_lanes")
            applied = await _call(executor, "consolidate_memory_lane", **lane)
            unchanged = await _call(executor, "consolidate_memory_lane", **lane)
            refused = await _call(executor, "consolidate_memory_lane", agent_id=str(world.agent), user_id=str(world.other))
            malformed = await _call(executor, "consolidate_memory_lane", agent_id="not-a-uuid")
            counts = await _call(executor, "list_memory_consolidations")

        listed = json.loads(lanes.content)["lanes"]
        assert sorted((lane["user_id"] or "", lane["active_entries"]) for lane in listed) == sorted(
            [("", 1), (str(world.owner), 5)]
        )
        assert json.loads(applied.content) == {"status": "applied", "counts": {"merge": 1, "remove": 1, "demote": 1}}
        assert (unchanged.no_op, unchanged.is_error, unchanged.structured) == (
            True,
            False,
            {"status": "unchanged", "counts": {}},
        )
        assert (refused.refused, refused.content) == (True, "Error: this memory lane may not be read by the Concierge.")
        assert malformed.content == "Error: agent_id must be a UUID."
        assert json.loads(counts.content)["counts"] == {"merge": 1, "remove": 1, "demote": 1}
        for result in (lanes, applied, unchanged, refused, counts):
            assert "French" not in result.content and "EDR" not in result.content

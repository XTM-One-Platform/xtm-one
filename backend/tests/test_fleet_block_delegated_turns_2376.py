"""The fleet autonomy block reaches every turn of a fleet member, delegated ones included (#2376).

- ``turn_fleet_autonomy_block`` answers the assignment run's text for a
  member's chat, consult, background task, work-item turn and transfer, and
  for a connected agent reached by a member's delegation (the chain's fleet).
- An agent in no fleet, reached by no member, gets no block.
- ``run_agent_chat`` appends it.
- The seeded Scenario Creator and loop orchestrators agree with the catalog
  action "create or launch" and never let creation proceed while the gate waits.
- Built-in fleets still never pause a tool call (#1675).
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_fleet_docs import CTEM_VALIDATION_ACTION, CTEM_VALIDATION_GATE
from app.services import agent_chat, fleet_autonomy, fleet_runs
from app.services.fleet_runs import NO_FLEET, FleetRunDefaults

PERSONAS = Path(fleet_autonomy.__file__).resolve().parents[1] / "agents" / "seed_data" / "agent_persona"
FLEET = uuid.uuid4()
MEMBER = uuid.uuid4()
CONNECTED = uuid.uuid4()
GATES = [{"key": CTEM_VALIDATION_GATE, "action": CTEM_VALIDATION_ACTION, "module": "ctem", "impact": "destructive"}]


@pytest.fixture
def fleet_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    homes = {MEMBER: FleetRunDefaults(fleet_id=FLEET)}

    async def _home(db: Any, agent_id: object) -> FleetRunDefaults:
        try:
            return homes.get(uuid.UUID(str(agent_id)), NO_FLEET)
        except ValueError:
            return NO_FLEET

    async def _autonomy(db: Any, fleet_id: Any) -> Any:
        return ("full_gates", GATES) if fleet_id == FLEET else None

    async def _instructions(db: Any, fleet_id: Any) -> None:
        return None

    async def _enforced(db: Any, fleet_id: Any) -> bool:
        return False

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    monkeypatch.setattr(fleet_autonomy, "fleet_autonomy_for_run", _autonomy)
    monkeypatch.setattr(fleet_autonomy, "fleet_instructions_for_run", _instructions)
    monkeypatch.setattr(fleet_autonomy, "fleet_enforces_at_tool_level", _enforced)
    return {"homes": homes}


def _agent(agent_id: uuid.UUID, slug: str = "custom-agent") -> Any:
    return SimpleNamespace(id=agent_id, slug=slug, is_builtin=False)


def _expected() -> str:
    return fleet_autonomy.fleet_autonomy_block("full_gates", GATES, None, enforced=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain",
    [
        [],  # chat or work-item turn of the member
        [str(uuid.uuid4())],  # consult or transfer from an agent outside the fleet
        [str(MEMBER)],  # background task the member spawned for itself
    ],
)
async def test_member_turn_carries_the_assignment_block(fleet_world: dict, chain: list[str]) -> None:
    block = await fleet_autonomy.turn_fleet_autonomy_block(None, _agent(MEMBER), [*chain, str(MEMBER)])
    assert block == _expected()
    assert fleet_autonomy.FLEET_AUTONOMY_HEADER in block
    assert CTEM_VALIDATION_ACTION in block and "waits for a person's approval" in block


@pytest.mark.asyncio
async def test_connected_agent_reached_by_a_member_inherits_the_fleet_block(fleet_world: dict) -> None:
    chain = [str(MEMBER), str(CONNECTED)]
    assert await fleet_autonomy.turn_fleet_autonomy_block(None, _agent(CONNECTED), chain) == _expected()
    # Sub-delegated below the connected agent: still the member's fleet.
    deeper = uuid.uuid4()
    chain = [str(MEMBER), str(CONNECTED), str(deeper)]
    assert await fleet_autonomy.turn_fleet_autonomy_block(None, _agent(deeper), chain) == _expected()


@pytest.mark.asyncio
async def test_agent_in_no_fleet_gets_no_block(fleet_world: dict) -> None:
    lone = uuid.uuid4()
    assert await fleet_autonomy.turn_fleet_autonomy_block(None, _agent(lone), [str(lone)]) == ""
    other = uuid.uuid4()
    assert await fleet_autonomy.turn_fleet_autonomy_block(None, _agent(lone), [str(other), str(lone)]) == ""


@pytest.mark.asyncio
async def test_pre_existing_member_takes_its_callers_fleet_only(fleet_world: dict) -> None:
    creator = uuid.uuid4()
    fleet_world["homes"][creator] = FleetRunDefaults(fleet_id=FLEET)
    agent = SimpleNamespace(id=creator, slug="openaev-scenario-creator", is_builtin=True)
    # Run nobody in the fleet started: as in 1.261102.0, no block.
    assert await fleet_autonomy.turn_fleet_autonomy_block(None, agent, [str(creator)]) == ""
    # Delegated by a fleet member: the fleet's gates apply.
    assert await fleet_autonomy.turn_fleet_autonomy_block(None, agent, [str(MEMBER), str(creator)]) == _expected()


def test_run_agent_chat_appends_the_block() -> None:
    source = inspect.getsource(agent_chat.run_agent_chat)
    assert "turn_fleet_autonomy_block(db, agent, _visited_agents)" in source
    assert "_call_parts.append(_fleet_block)" in source


def test_catalog_and_personas_agree_on_create_or_launch() -> None:
    assert "create or launch" in CTEM_VALIDATION_ACTION
    for name in ("ctem-threat-response-orchestrator.md", "ctem-vuln-prioritization-orchestrator.md"):
        text = (PERSONAS / name).read_text(encoding="utf-8")
        assert "Launching it is the gate" not in text, name
        assert f"Creating or launching it is the gate `{CTEM_VALIDATION_GATE}`" in text, name
        assert "UNVERIFIED" in text and "delete_openaev_scenario" in text, name


def test_scenario_creator_never_builds_while_the_gate_waits() -> None:
    text = (PERSONAS / "openaev-scenario-creator.md").read_text(encoding="utf-8")
    assert "IS the approval" not in text
    assert CTEM_VALIDATION_GATE in text and "create or launch" in text
    assert "return this plan as your answer and stop" in text


def test_builtin_fleets_still_never_pause_a_tool_call() -> None:
    block = fleet_autonomy.fleet_autonomy_block("full_gates", GATES, enforced=False)
    assert "never a pause inside a run" in block
    assert "pauses the tool calls" not in block

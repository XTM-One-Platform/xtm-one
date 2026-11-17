"""A consulted configured peer consults its own consult peers in turn.

The Orchestrator consults the Clerk, and the Clerk needs a figure only the
Analyst holds. The Clerk's consulted turn keeps its consult handover - never a
transfer nor a background task - while the delegation depth allows one, and
the visited chain every consult carries refuses an agent already in it.

These drive the real ``run_agent_chat`` of every agent in the chain; only the
model loop is scripted, per agent, and the session finds the agents by the ids
a query binds and records the runs it is handed.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.agent_chat as agent_chat_mod
from app.llm.base import ChatResponse
from app.models.agent import Agent
from app.models.agent_delegation import AgentDelegation
from app.models.assignment import AssignmentRun
from app.services.builtin_tools import consulted_peer_handover
from app.services.builtin_tools.handover import CONSULTED_PEER_HANDOVER_TYPES
from app.services.sub_agent import _MAX_DELEGATION_DEPTH
from app.tools.base import ToolCall
from tests.test_agent_chat_evaluation import _caller_trace, _EmptyDB, _EmptyResult, _loop_agent
from tests.test_agent_chat_smoke import _FakeRedis

#: A known user, so a background task would be offered to a turn that may run one.
_USER = str(uuid.uuid4())
_CONVERSATION = str(uuid.uuid4())
_HANDOVER_TOOLS = {"consult_agent", "transfer_to_agent", "spawn_background_task"}
_IDENTITY = re.compile(r'\[Agent Identity\] You are "([^"]+)"')


def _desk(name: str):
    agent = _loop_agent()
    agent.id = uuid.uuid4()
    agent.name = name
    agent.slug = name.lower()
    agent.enabled = True
    agent.description = f"The {name} desk."
    agent.handover_conditions = {}
    agent.approve_tool_calls = False
    return agent


def _wire(source, **peers_by_type) -> None:
    """Give *source* its handover peers, keyed by handover type."""
    source.handover_agent_ids = [str(p.id) for peers in peers_by_type.values() for p in peers]
    source.handover_types = {str(p.id): h_type for h_type, peers in peers_by_type.items() for p in peers}


class _Rows(_EmptyResult):
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self.first()


class _ChainDB(_EmptyDB):
    """A session over the chain's agents: a ``select(Agent)`` finds the ids it binds."""

    def __init__(self, agents: list, rows: list) -> None:
        self._agents = agents
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement, *_args, **_kwargs):
        descriptions = getattr(statement, "column_descriptions", None) or []
        if not descriptions or descriptions[0].get("entity") is not Agent:
            return _EmptyResult()
        bound: set[str] = set()
        for value in statement.compile().params.values():
            bound.update(str(v) for v in (value if isinstance(value, (list, tuple, set)) else [value]))
        return _Rows([a for a in self._agents if str(a.id) in bound])

    def add(self, row) -> None:
        if isinstance(row, (AssignmentRun, AgentDelegation)):
            row.id = row.id or uuid.uuid4()
            self._rows.append(row)


async def _run_chain(top, agents: list, plan: dict[str, list[tuple[str, str]]], answers: dict[str, str], **run_kwargs) -> dict:
    """Run *top*'s turn; each agent consults as *plan* says, then answers ``answers[name]``.

    Returns, per agent, the system prompt and tools its loop was given, the
    tool results its consults got and the ``run_agent_chat`` keyword arguments
    of its turn, plus every run and delegation the consults recorded.
    """
    turns: dict[str, dict] = {}
    rows: list = []
    real_run_agent_chat = agent_chat_mod.run_agent_chat

    async def _spy(*args, **kwargs):
        agent = args[2] if len(args) > 2 else kwargs["agent"]
        turns.setdefault(agent.name, {})["kwargs"] = kwargs
        return await real_run_agent_chat(*args, **kwargs)

    async def _loop(**kwargs):
        system = [m.content for m in kwargs["messages"] if m.role == "system"]
        name = next(match.group(1) for text in system if (match := _IDENTITY.search(text)))
        turn = turns.setdefault(name, {})
        turn["system"] = system[0]
        turn["tools"] = {t.name for t in kwargs.get("tools") or []}
        turn["results"] = []
        # A step of this agent's loop, as the live view receives it.
        if kwargs.get("on_event"):
            await kwargs["on_event"]({"status": "thinking", "agent": name})
        for i, (target, question) in enumerate(plan.get(name, [])):
            call = ToolCall(id=f"{name}-{i}", name="consult_agent", arguments={"agent_id": target, "question": question})
            result = await kwargs["executor"].execute_tool(call)
            turn["results"].append(result.content if hasattr(result, "content") else str(result))
        answer = answers.get(name, f"{name} answers.")
        if turn["results"]:
            answer += "\n" + "\n".join(turn["results"])
        return ChatResponse(content=answer, model="fake-model", stop_reason="end_turn")

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch("app.services.langfuse_tracing.trace_llm_call", new=AsyncMock(side_effect=lambda *a, **k: _caller_trace())),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.llm.router.get_llm_for_user", new=AsyncMock(return_value=(SimpleNamespace(provider_name="fake"), "fake-model"))),
        patch("app.core.database.async_session", new=lambda: _ChainDB(agents, rows)),
        patch("app.services.prompt_guards.guard_chat_message", new=AsyncMock(return_value=None)),
        patch("app.services.prompt_guards.guard_model_output", new=AsyncMock(side_effect=lambda text, **_k: text)),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
        patch.object(agent_chat_mod, "run_agent_chat", new=_spy),
    ):
        text, _ = await real_run_agent_chat(
            _ChainDB(agents, rows),  # type: ignore[arg-type]
            _USER,
            top,  # type: ignore[arg-type]
            [],
            "What is the quarter's figure?",
            conversation_id=_CONVERSATION,
            **run_kwargs,
        )
    return {
        "text": text,
        "turns": turns,
        "runs": [r for r in rows if isinstance(r, AssignmentRun)],
        "delegations": [r for r in rows if isinstance(r, AgentDelegation)],
    }


def _handover_tools(turn: dict) -> set[str]:
    return turn["tools"] & _HANDOVER_TOOLS


@pytest.mark.asyncio
async def test_a_consulted_peer_asks_its_own_consult_peer_and_answers_with_its_figure():
    orchestrator, clerk, analyst = _desk("Orchestrator"), _desk("Clerk"), _desk("Analyst")
    _wire(orchestrator, consult=[clerk])
    _wire(clerk, consult=[analyst])

    chain = await _run_chain(
        orchestrator,
        [orchestrator, clerk, analyst],
        plan={"Orchestrator": [("clerk", "Ask the Analyst for X and answer.")], "Clerk": [("analyst", "What is X?")]},
        answers={"Analyst": "X is 42."},
    )

    clerk_turn = chain["turns"]["Clerk"]
    assert "## Agent Delegation" in clerk_turn["system"]
    assert f'"Analyst" (id: {analyst.id}' in clerk_turn["system"] and "[CONSULT]" in clerk_turn["system"]
    assert _handover_tools(clerk_turn) == {"consult_agent"}
    assert clerk_turn["results"] == ["[Response from Analyst]:\nX is 42."]
    assert "X is 42." in chain["turns"]["Orchestrator"]["results"][0], "the Clerk's answer carries the Analyst's figure"

    # The nested consult is recorded under the consulted Clerk's run, in the
    # conversation the turn works for.
    by_agent = {run.agent_id: run for run in chain["runs"]}
    clerk_run, analyst_run = by_agent[clerk.id], by_agent[analyst.id]
    assert analyst_run.parent_run_id == clerk_run.id
    assert clerk_run.parent_run_id == by_agent[orchestrator.id].id
    assert analyst_run.trigger_payload["parent_agent_id"] == str(clerk.id)
    assert analyst_run.trigger_payload["conversation_id"] == _CONVERSATION
    assert chain["turns"]["Analyst"]["kwargs"]["investigations_conversation_id"] == _CONVERSATION
    [nested] = [d for d in chain["delegations"] if d.target_agent_id == analyst.id]
    assert nested.parent_agent_id == clerk.id and nested.conversation_id == uuid.UUID(_CONVERSATION)
    assert chain["turns"]["Analyst"]["kwargs"]["meter_quota"] is False


@pytest.mark.asyncio
async def test_a_consulted_peer_neither_transfers_nor_starts_a_background_task():
    orchestrator, clerk = _desk("Orchestrator"), _desk("Clerk")
    analyst, closer, worker, router = _desk("Analyst"), _desk("Closer"), _desk("Worker"), _desk("Router")
    _wire(orchestrator, consult=[clerk])
    _wire(clerk, consult=[analyst], transfer=[closer], delegate=[worker], mandatory_transfer=[router])

    chain = await _run_chain(
        orchestrator,
        [orchestrator, clerk, analyst, closer, worker, router],
        plan={"Orchestrator": [("clerk", "Close the case.")], "Clerk": [("closer", "Take it.")]},
        answers={},
    )

    clerk_turn = chain["turns"]["Clerk"]
    assert _handover_tools(clerk_turn) == {"consult_agent"}
    assert '"Analyst"' in clerk_turn["system"]
    for other in ("Closer", "Worker", "Router"):
        assert f'"{other}"' not in clerk_turn["system"], f"{other} is not reachable inside a consult"
    assert clerk_turn["results"] == ["Agent closer is not available for consultation."]
    assert "Closer" not in chain["turns"] and "Router" not in chain["turns"]
    assert chain["turns"]["Clerk"]["kwargs"]["allowed_handover_types"] == CONSULTED_PEER_HANDOVER_TYPES


@pytest.mark.asyncio
@pytest.mark.parametrize("hops", [2, 3], ids=["clerk-back-to-orchestrator", "analyst-back-to-orchestrator"])
async def test_a_consult_back_to_an_agent_in_the_chain_is_refused(hops):
    orchestrator, clerk, analyst = _desk("Orchestrator"), _desk("Clerk"), _desk("Analyst")
    _wire(orchestrator, consult=[clerk])
    looping = clerk if hops == 2 else analyst
    if hops == 2:
        _wire(clerk, consult=[analyst, orchestrator])
    else:
        _wire(clerk, consult=[analyst])
        _wire(analyst, consult=[orchestrator])
    plan = {"Orchestrator": [("clerk", "Ask around.")], "Clerk": [("analyst", "What is X?")] if hops == 3 else []}
    plan[looping.name] = [*plan.get(looping.name, []), ("orchestrator", "And you?")]

    chain = await _run_chain(orchestrator, [orchestrator, clerk, analyst], plan=plan, answers={})

    refusal = chain["turns"][looping.name]["results"][-1]
    assert refusal.startswith("Handover rejected: agent Orchestrator is already in this delegation chain (")
    assert refusal.endswith("Complete the work yourself or choose a different agent.")
    assert len([run for run in chain["runs"] if run.agent_id == orchestrator.id]) == 1, "the loop never ran"


@pytest.mark.asyncio
async def test_no_handover_tool_past_the_delegation_depth():
    orchestrator, clerk, analyst = _desk("Orchestrator"), _desk("Clerk"), _desk("Analyst")
    _wire(orchestrator, consult=[clerk])
    _wire(clerk, consult=[analyst])

    chain = await _run_chain(
        orchestrator,
        [orchestrator, clerk, analyst],
        plan={"Orchestrator": [("clerk", "What is X?")]},
        answers={},
        delegation_depth=_MAX_DELEGATION_DEPTH - 1,
    )

    clerk_turn = chain["turns"]["Clerk"]
    assert clerk_turn["kwargs"]["delegation_depth"] == _MAX_DELEGATION_DEPTH
    assert "## Agent Delegation" not in clerk_turn["system"]
    assert not _handover_tools(clerk_turn)
    assert consulted_peer_handover(_MAX_DELEGATION_DEPTH - 1) == (True, CONSULTED_PEER_HANDOVER_TYPES)
    assert consulted_peer_handover(_MAX_DELEGATION_DEPTH) == (False, CONSULTED_PEER_HANDOVER_TYPES)


@pytest.mark.asyncio
async def test_a_consulted_peer_without_peers_answers_as_before():
    orchestrator, analyst = _desk("Orchestrator"), _desk("Analyst")
    _wire(orchestrator, consult=[analyst])

    chain = await _run_chain(
        orchestrator,
        [orchestrator, analyst],
        plan={"Orchestrator": [("analyst", "What is X?")]},
        answers={"Analyst": "X is 42."},
    )

    analyst_turn = chain["turns"]["Analyst"]
    assert "## Agent Delegation" not in analyst_turn["system"]
    assert not _handover_tools(analyst_turn)
    assert chain["turns"]["Orchestrator"]["results"] == ["[Response from Analyst]:\nX is 42."]


@pytest.mark.asyncio
async def test_a_consult_withheld_from_openaev_writes_withholds_the_peer_it_consults():
    orchestrator, clerk, analyst = _desk("Orchestrator"), _desk("Clerk"), _desk("Analyst")
    _wire(orchestrator, consult=[clerk])
    _wire(clerk, consult=[analyst])

    chain = await _run_chain(
        orchestrator,
        [orchestrator, clerk, analyst],
        plan={"Orchestrator": [("clerk", "Ask the Analyst.")], "Clerk": [("analyst", "What is X?")]},
        answers={},
        advisory_consult_specialists=True,
    )

    assert chain["turns"]["Clerk"]["kwargs"]["restrict_openaev_writes"] is True
    assert chain["turns"]["Analyst"]["kwargs"]["restrict_openaev_writes"] is True


def test_an_assignment_run_consults_its_peer_one_level_below_it():
    from pathlib import Path

    source = (Path(agent_chat_mod.__file__).parent / "assignment_engine.py").read_text(encoding="utf-8")
    consult = source.split("async def _consult_agent(", 1)[1].split('_ho_exec.register("consult_agent"', 1)[0]
    assert "consulted_peer_handover(1)" in consult
    assert "allow_handover=_nested_handover," in consult
    assert "allowed_handover_types=_nested_types," in consult
    assert "delegation_depth=1," in consult


@pytest.mark.asyncio
async def test_each_consult_carries_its_own_id_on_its_frames():
    """Two consults of one agent are two consults on the live view, and a nested one is its own."""
    orchestrator, clerk, analyst = _desk("Orchestrator"), _desk("Clerk"), _desk("Analyst")
    _wire(orchestrator, consult=[clerk])
    _wire(clerk, consult=[analyst])
    frames: list[dict] = []

    async def _on_event(event: dict) -> None:
        frames.append(event)

    await _run_chain(
        orchestrator,
        [orchestrator, clerk, analyst],
        plan={
            "Orchestrator": [("clerk", "First question."), ("clerk", "Second question.")],
            "Clerk": [("analyst", "What is X?")],
        },
        answers={"Analyst": "X is 42."},
        on_event=_on_event,
    )

    consults = [f for f in frames if f.get("status") == "consult_progress"]
    assert consults and all(f["agent_id"] == str(clerk.id) for f in consults)
    # Each consult of the Clerk: its own step, and the Analyst's under one more wrapper.
    by_consult: dict[str, list[dict]] = {}
    for frame in consults:
        by_consult.setdefault(frame["consult_id"], []).append(frame["inner"])
    assert len(by_consult) == 2, "the two consults of the Clerk carry two ids"
    nested_ids = set()
    for inner_frames in by_consult.values():
        assert {"status": "thinking", "agent": "Clerk"} in inner_frames
        nested = [f for f in inner_frames if f.get("status") == "consult_progress"]
        assert {f["agent_id"] for f in nested} == {str(analyst.id)}
        assert {"status": "thinking", "agent": "Analyst"} in [f["inner"] for f in nested]
        [nested_id] = {f["consult_id"] for f in nested}
        nested_ids.add(nested_id)
    assert len(nested_ids) == 2 and not nested_ids & set(by_consult), "each nested consult has an id of its own"

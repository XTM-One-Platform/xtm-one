"""A turn lists under ``## Agent Delegation`` exactly the peers its handover tools reach.

An assignment run listed a ``mandatory_transfer`` peer, which no tool reaches
(chat routes it after the agent's answer, an assignment run never does), with
the delegate instruction: the model was told to hand over through
``spawn_background_task``, which the run registered only when another peer was
``delegate`` or self-fan-out was on - and then that tool, like ``consult_agent``
and ``transfer_to_agent``, accepted the mandatory peer. A ``delegate`` peer was
likewise told to use ``spawn_background_task`` on a turn that does not register
it (an anonymous chat user, an assignment run with no acting user), a peer
whose only tool the agent's ``tool_deny_list`` removes was told to use it, and
a mentioned agent was handed ``transfer_to_agent`` for the peers of a legacy
map although a mention never transfers.
"""

from __future__ import annotations

import inspect
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_chat, assignment_engine, general_chat
from app.services.agent_chat import filter_handover_agents_by_type
from app.services.builtin_tools import (
    build_sub_agent_tools,
    handover_peers_by_type,
    handover_tool_types,
    offered_handover_types,
    reachable_handover_peers,
)
from app.services.builtin_tools.handover import peer_handover_type, peer_tool_types
from app.services.mention_dispatch import MENTIONED_AGENT_HANDOVER_TYPES
from tests.agentic_gate_stub import stub_agentic_gate

HANDOVER_TOOLS_BY_TYPE = {
    "delegate": "spawn_background_task",
    "consult": "consult_agent",
    "transfer": "transfer_to_agent",
}
HANDOVER_TOOLS = tuple(HANDOVER_TOOLS_BY_TYPE.values())


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


# ---------------------------------------------------------------------------
# The helper every site reads
# ---------------------------------------------------------------------------


def _ids(peers) -> list[str]:
    return [str(peer.id) for peer in peers]


@pytest.mark.parametrize(
    ("stored", "runs_as"),
    [
        (None, "delegate"),
        ("delegate", "delegate"),
        ("consult", "consult"),
        ("transfer", "transfer"),
        ("custom", "delegate"),
        ("mandatory_transfer", None),
    ],
)
def test_the_type_a_peer_is_reached_through(stored, runs_as):
    type_map = {"other": "consult"} | ({"a": stored} if stored else {})
    assert peer_handover_type(type_map, "a") == runs_as


def test_a_mandatory_transfer_peer_is_never_reachable():
    mandatory, consulted = _peer("Rule Router"), _peer("Reviewer")
    types = {str(mandatory.id): "mandatory_transfer", str(consulted.id): "consult"}

    reachable = reachable_handover_peers([mandatory, consulted], types)

    assert _ids(reachable) == [str(consulted.id)]
    assert handover_tool_types(reachable, types) == {"consult"}


def test_a_delegate_peer_is_reachable_only_where_background_tasks_are_registered():
    """An anonymous chat user and an ownerless assignment run get no ``spawn_background_task``."""
    typed, untyped, consulted = _peer("Worker"), _peer("Helper"), _peer("Reviewer")
    types = {str(typed.id): "delegate", str(consulted.id): "consult"}
    peers = [typed, untyped, consulted]
    no_background_task = offered_handover_types(delegate_available=False)

    assert _ids(reachable_handover_peers(peers, types)) == _ids(peers)
    assert _ids(reachable_handover_peers(peers, types, offered=no_background_task)) == [str(consulted.id)]


@pytest.mark.parametrize(
    ("kwargs", "offered"),
    [
        ({}, {"delegate", "consult", "transfer"}),
        ({"delegate_available": False}, {"consult", "transfer"}),
        ({"allowed_types": MENTIONED_AGENT_HANDOVER_TYPES}, {"consult", "delegate"}),
        ({"allowed_types": frozenset({"transfer", "mandatory_transfer"})}, {"transfer"}),
        ({"allowed_types": frozenset({"mandatory_transfer"})}, set()),
        ({"tool_deny_list": ["consult_agent", "web_fetch"]}, {"delegate", "transfer"}),
        ({"tool_deny_list": ["spawn_background_task"], "allowed_types": frozenset({"delegate"})}, set()),
        ({"tool_deny_list": None}, {"delegate", "consult", "transfer"}),
    ],
)
def test_the_types_a_turn_offers(kwargs, offered):
    assert offered_handover_types(**kwargs) == offered


@pytest.mark.parametrize("empty", [{}, None])
@pytest.mark.parametrize("delegate_available", [True, False])
def test_a_legacy_map_keeps_every_peer_and_every_tool_the_turn_offers(empty, delegate_available):
    peers = [_peer("Worker"), _peer("Reviewer")]
    offered = offered_handover_types(delegate_available=delegate_available)

    reachable = reachable_handover_peers(peers, empty, offered=offered)

    assert _ids(reachable) == _ids(peers)
    assert handover_tool_types(reachable, empty, offered=offered) == offered


def test_the_tool_types_are_those_of_the_peers_left():
    delegated, transferred = _peer("Worker"), _peer("Closer")
    types = {str(delegated.id): "delegate", str(transferred.id): "transfer"}

    assert handover_tool_types([delegated, transferred], types) == {"delegate", "transfer"}
    assert handover_tool_types([], types) == frozenset()
    assert handover_tool_types([], {}) == frozenset()


def test_no_tool_type_comes_from_a_peer_no_tool_reaches():
    """The union is taken over each peer's own reach, so an unfiltered list adds nothing."""
    mandatory, consulted = _peer("Rule Router"), _peer("Reviewer")
    types = {str(mandatory.id): "mandatory_transfer", str(consulted.id): "consult"}

    assert peer_tool_types(types, str(mandatory.id)) == frozenset()
    assert handover_tool_types([mandatory, consulted], types) == {"consult"}
    assert handover_tool_types([mandatory], types) == frozenset()


def test_a_peer_whose_only_tool_is_denied_is_not_reachable():
    consulted, delegated = _peer("Reviewer"), _peer("Worker")
    types = {str(consulted.id): "consult", str(delegated.id): "delegate"}
    offered = offered_handover_types(tool_deny_list=["consult_agent"])

    reachable = reachable_handover_peers([consulted, delegated], types, offered=offered)

    assert _ids(reachable) == [str(delegated.id)]
    assert handover_tool_types(reachable, types, offered=offered) == {"delegate"}


# ---------------------------------------------------------------------------
# Each tool accepts its own type's peers only
# ---------------------------------------------------------------------------


def test_each_tool_accepts_only_the_peers_of_its_own_type():
    delegated, consulted, transferred = _peer("Worker"), _peer("Reviewer"), _peer("Closer")
    mandatory = _peer("Rule Router")
    types = {
        str(delegated.id): "delegate",
        str(consulted.id): "consult",
        str(transferred.id): "transfer",
        str(mandatory.id): "mandatory_transfer",
    }

    by_type = handover_peers_by_type([delegated, consulted, transferred, mandatory], types)

    assert {h_type: _ids(peers) for h_type, peers in by_type.items()} == {
        "delegate": [str(delegated.id)],
        "consult": [str(consulted.id)],
        "transfer": [str(transferred.id)],
    }


@pytest.mark.parametrize("empty", [{}, None])
def test_a_legacy_peer_is_accepted_by_every_tool_the_turn_offers(empty):
    peers = [_peer("Worker"), _peer("Reviewer")]

    assert all(_ids(listed) == _ids(peers) for listed in handover_peers_by_type(peers, empty).values())
    mention = offered_handover_types(allowed_types=MENTIONED_AGENT_HANDOVER_TYPES)
    narrowed = handover_peers_by_type(peers, empty, offered=mention)
    assert {h_type: _ids(listed) for h_type, listed in narrowed.items()} == {
        "delegate": _ids(peers),
        "consult": _ids(peers),
        "transfer": [],
    }


def test_a_custom_peer_is_accepted_by_the_background_task_tool_only():
    custom = _peer("Worker")
    by_type = handover_peers_by_type([custom], {str(custom.id): "custom"})
    assert (_ids(by_type["delegate"]), by_type["consult"], by_type["transfer"]) == ([str(custom.id)], [], [])


@pytest.mark.asyncio
async def test_the_background_task_tool_refuses_a_peer_it_was_not_given():
    """What every site hands ``build_sub_agent_tools``: the delegate peers only."""
    delegated, consulted = _peer("Worker"), _peer("Reviewer")
    by_type = handover_peers_by_type(
        [delegated, consulted], {str(delegated.id): "delegate", str(consulted.id): "consult"}
    )
    _defs, executor = build_sub_agent_tools(None, str(uuid.uuid4()), str(uuid.uuid4()), by_type["delegate"])

    from app.tools.base import ToolCall

    result = await executor.execute_tool(
        ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": consulted.slug, "task": "Review."})
    )

    # The refusal names what is missing.
    assert result.content == (
        f"Agent {consulted.slug} is not available for background tasks: it is not a "
        "delegate peer of this agent - add it to this agent's handover agents with "
        "the delegate type (update_agent handover_agent_ids + handover_types)."
    )


@pytest.mark.parametrize(
    ("module", "function"),
    [
        (agent_chat, "run_agent_chat"),
        (assignment_engine, "execute_assignment_run"),
        (general_chat, "run_general_chat"),
    ],
    ids=["agent_chat", "assignment_engine", "general_chat"],
)
def test_every_site_hands_each_tool_its_own_type_share(module, function):
    """Chat (mentions and flows included) and the General Assistant are pinned; runs are driven below."""
    source = inspect.getsource(getattr(module, function))
    resolutions = re.findall(r"resolve_handover_agent\(\s*agent_id,\s*([^)]+?)\s*\)", source)
    assert sorted(resolutions) == ['_peers_by_type["consult"]', '_peers_by_type["transfer"]']
    # The call's peer argument, the fourth positional one or ``handover_agents=``.
    call = source.index("build_sub_agent_tools(")
    build = source[call : call + 900]
    assert re.search(r'(?:,\s*|handover_agents=)_peers_by_type(?:\["delegate"\]|\.get\("delegate", \[\]\)),', build)
    assert not re.search(r"^\s*(?:handover_agents|available_agents),\s*$", build, re.MULTILINE)
    assert "handover_agents=_handover_agents" not in build
    assert "handover_peers_by_type(" in source


# ---------------------------------------------------------------------------
# A turn that allows only some types (a mention, a flow edge's label)
# ---------------------------------------------------------------------------


def test_a_type_no_tool_implements_runs_as_delegate_on_a_constrained_turn():
    custom, transferred = _peer("Worker"), _peer("Closer")
    types = {str(custom.id): "custom", str(transferred.id): "transfer"}

    kept = filter_handover_agents_by_type([custom, transferred], types, MENTIONED_AGENT_HANDOVER_TYPES)

    assert _ids(kept) == [str(custom.id)]
    offered = offered_handover_types(allowed_types=MENTIONED_AGENT_HANDOVER_TYPES)
    assert handover_tool_types(kept, types, offered=offered) == {"delegate"}


@pytest.mark.parametrize("empty", [{}, None])
def test_a_mentioned_agent_gets_no_transfer_tool_for_a_legacy_map(empty):
    """A mention consults and delegates: every mention caller drops a transfer."""
    peers = [_peer("Worker"), _peer("Reviewer")]
    offered = offered_handover_types(allowed_types=MENTIONED_AGENT_HANDOVER_TYPES)

    kept = filter_handover_agents_by_type(peers, empty, MENTIONED_AGENT_HANDOVER_TYPES)
    reachable = reachable_handover_peers(kept, empty, offered=offered)

    assert _ids(reachable) == _ids(peers)
    assert handover_tool_types(reachable, empty, offered=offered) == {"consult", "delegate"}


@pytest.mark.parametrize("label", ["transfer", "consult", "delegate"])
def test_a_legacy_peer_is_reached_through_the_one_type_a_flow_edge_allows(label):
    """A legacy peer runs every type, so a labelled edge keeps it with that type's tool only."""
    peers = [_peer("Worker"), _peer("Reviewer")]
    allowed = frozenset({label})
    offered = offered_handover_types(allowed_types=allowed)

    kept = filter_handover_agents_by_type(peers, {}, allowed)

    assert _ids(kept) == _ids(peers)
    assert handover_tool_types(reachable_handover_peers(kept, {}, offered=offered), {}, offered=offered) == {label}


def test_a_mandatory_transfer_peer_stays_for_the_routing_only_where_that_type_is_allowed():
    mandatory, delegated = _peer("Rule Router"), _peer("Worker")
    types = {str(mandatory.id): "mandatory_transfer", str(delegated.id): "delegate"}
    peers = [mandatory, delegated]

    assert filter_handover_agents_by_type(peers, types, None) == peers
    assert _ids(filter_handover_agents_by_type(peers, types, MENTIONED_AGENT_HANDOVER_TYPES)) == [
        str(delegated.id)
    ]
    assert _ids(filter_handover_agents_by_type(peers, types, frozenset({"mandatory_transfer"}))) == [
        str(mandatory.id)
    ]
    # Kept for the routing after the answer, never for a tool.
    assert reachable_handover_peers([mandatory], types) == []


def test_a_legacy_map_keeps_no_peer_for_a_mandatory_transfer_edge():
    peers = [_peer("Worker")]
    assert filter_handover_agents_by_type(peers, {}, frozenset({"mandatory_transfer"})) == []


# ---------------------------------------------------------------------------
# The sites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", [agent_chat, assignment_engine], ids=["agent_chat", "assignment_engine"])
def test_the_prompt_and_the_tools_read_the_one_peer_list(module):
    """Chat is not driven end to end here; both sites are pinned to the helper."""
    source = inspect.getsource(module.run_agent_chat if module is agent_chat else module.execute_assignment_run)
    offered = source.index("offered_handover_types(")
    reachable = source.index("reachable_handover_peers(")
    assert offered < reachable < source.index("## Agent Delegation")
    assert source.count("handover_tool_types(") == 1
    assert reachable < source.index("handover_tool_types(")
    # One offered set, read by the peer list, each tool's share of it, the tool
    # types, the tags, the consult refusal and self-fan-out.
    offered_name = "_offered_types" if module is agent_chat else "_offered_handover_types"
    assert source.count(f"offered={offered_name}") == 5
    self_fanout_gate = re.split(r"\n\s*\)", source.split("_self_fanout_allowed = (", 1)[1], maxsplit=1)[0]
    assert f'"delegate" in {offered_name}' in self_fanout_gate
    # Built from the agent's deny list, before the tools are.
    assert "tool_deny_list=agent.tool_deny_list" in source[offered:reachable]
    # Tool types are no longer derived from the raw map at the site.
    assert '.get(str(ha.id), "delegate")' not in source
    assert "_every_type" not in source


def test_chat_narrows_the_offered_types_to_those_the_caller_allows():
    source = inspect.getsource(agent_chat.run_agent_chat)
    offered = source.index("offered_handover_types(")
    assert "allowed_types=allowed_handover_types" in source[offered : source.index("reachable_handover_peers(")]


def test_chat_keeps_its_mandatory_transfer_targets_for_the_routing_after_the_answer():
    source = inspect.getsource(agent_chat.run_agent_chat)
    extraction = source.index("_mandatory_transfer_targets.append(ha)")
    assert extraction < source.index("reachable_handover_peers(")
    assert "mandatory_targets=_mandatory_transfer_targets" in source


# ---------------------------------------------------------------------------
# An assignment run, end to end
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def all(self):
        return list(self._rows)


class _Session:
    """Serves the run, its assignment and its agent, then the agent's peers."""

    def __init__(self, run, assignment, agent, peers):
        self._queue = [run, assignment, agent]
        self._peers = peers

    async def execute(self, stmt, *_args, **_kwargs):
        if self._queue:
            return _Result(scalar=self._queue.pop(0))
        from app.models.agent import Agent

        descriptions = getattr(stmt, "column_descriptions", None) or [{}]
        if descriptions[0].get("entity") is Agent:
            return _Result(rows=self._peers)
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def flush(self):
        return None

    def add(self, _obj):
        return None

    def in_transaction(self):
        return False

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _peer(name: str):
    from app.models.agent import Agent

    return Agent(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower().replace(" ", "-"),
        description=f"{name} specialist",
        enabled=True,
        is_company_managed=True,
    )


def _objects(peers, handover_types, *, owned=True, tool_deny_list=None):
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun

    owner = uuid.uuid4() if owned else None
    agent = Agent(
        id=uuid.uuid4(),
        name="Triage Lead",
        slug="triage-lead",
        enabled=True,
        user_id=owner,
        is_builtin=False,
        is_company_managed=not owned,
        integration_bindings={},
        builtin_tool_ids=[],
        handover_agent_ids=[str(p.id) for p in peers],
        handover_types=handover_types,
        tool_deny_list=tool_deny_list,
        output_format="markdown",
        # Handover reach, not the approval gate.
        approve_tool_calls=False,
    )
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=owner,
        is_company_managed=not owned,
        name="Triage the queue",
        trigger_type="manual",
        trigger_config={},
        prompt="Triage the queue.",
        enabled=True,
        dry_run=False,
        timeout_seconds=300,
        retry_on_failure=False,
        max_retries=0,
        notify_on_complete=False,
        execution_count=0,
    )
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        user_id=owner,
        is_company_managed=not owned,
        status="pending",
        trigger_payload={},
        trigger_context={"type": "manual"},
        retry_count=0,
    )
    return run, assignment, agent


async def _run_turn(
    monkeypatch, peers, handover_types, *, owned=True, self_fanout=False, tool_deny_list=None, calls=()
):
    """Run ``execute_assignment_run`` with a fake loop; return what the model was given.

    Each ``(tool, arguments)`` in *calls* is executed inside the loop through
    the run's own executor, and its result returned alongside.
    """
    from app.core.config import settings
    from app.llm.base import ChatMessage
    from app.services import assignment_engine
    from app.tools.base import ToolCall
    from app.tools.wait_policy import WaitPolicy

    run, assignment, agent = _objects(peers, handover_types, owned=owned, tool_deny_list=tool_deny_list)
    session = _Session(run, assignment, agent, peers)
    seen: dict = {}

    async def _fake_loop(**kwargs):
        seen["system"] = kwargs["messages"][0].content
        seen["tools"] = {t.name for t in kwargs["tools"] or []}
        seen["results"] = []
        for name, arguments in calls:
            result = await kwargs["executor"].execute_tool(ToolCall(id=name, name=name, arguments=arguments))
            seen["results"].append(result.content)
        kwargs["messages"].append(ChatMessage(role="assistant", content="Done."))
        return SimpleNamespace(content="Done.", usage={}, model="model-x", stop_reason="end_turn")

    llm = SimpleNamespace(provider_name="anthropic", default_model="model-x", last_usage=None)
    seen["peer_runs"] = AsyncMock(return_value=("answer", None))
    for target, value in {
        "app.services.assignment_engine.async_session": lambda: session,
        "app.services.quota.check_agentic_quota": AsyncMock(),
        "app.services.quota.increment_agentic_usage": AsyncMock(),
        "app.services.quota.refund_agentic_usage_for_failure": AsyncMock(return_value=True),
        "app.services.quota.refund_agentic_usage_for_provider_outage": AsyncMock(return_value=True),
        "app.services.assignment_engine.record_agentic_execution": AsyncMock(),
        "app.services.assignment_engine._publish_run_status": AsyncMock(),
        "app.services.assignment_engine.event_bus": SimpleNamespace(emit=AsyncMock()),
        "app.api.v1.helpers.get_user_group_ids": AsyncMock(return_value=[]),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=(llm, "model-x")),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.assignment_engine._build_trigger_information": lambda _run: "",
        "app.services.assignment_engine._load_memory_context": AsyncMock(return_value=("", "")),
        "app.services.builtin_tools.load_kb_names": AsyncMock(return_value={}),
        "app.services.builtin_tools.load_write_locked_kb_ids": AsyncMock(return_value=set()),
        "app.services.assignment_engine._build_system_message": AsyncMock(return_value="You are an agent."),
        "app.services.assignment_engine.load_authorized_trigger_files": AsyncMock(return_value=[]),
        "app.services.builtin_tools.load_mcp_tools": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_tools": AsyncMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        "app.services.assignment_engine.resolve_run_max_iterations": AsyncMock(return_value=10),
        "app.services.assignment_engine.resolve_run_wait_policy": AsyncMock(return_value=WaitPolicy()),
        "app.tools.run_budget.build_run_budget_for_run": AsyncMock(return_value=None),
        "app.tools.agent_loop.run_agent_loop": _fake_loop,
        "app.services.tool_tracking.track_tool_usage": AsyncMock(),
        "app.services.assignment_engine._track_assignment_auth_failures": AsyncMock(),
        "app.services.assignment_engine._track_stale_reference_failure": AsyncMock(),
        "app.services.integration_tools.cancel_run_partner_tasks": AsyncMock(),
        "app.services.builtin_tools.cleanup_mcp_manager": AsyncMock(),
        "app.services.assignment_engine.create_execution": AsyncMock(),
        "app.services.agent_chat.run_agent_chat": seen["peer_runs"],
        "app.services.delegation.log_delegation": AsyncMock(),
    }.items():
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(settings, "agent_self_fanout_enabled", self_fanout)

    await assignment_engine.execute_assignment_run(str(run.id))
    assert run.status == "completed", run.error
    return seen


def _delegation_section(system: str) -> str:
    if "## Agent Delegation" not in system:
        return ""
    return system.split("## Agent Delegation", 1)[1]


def _listed_peers(system: str) -> dict[str, str]:
    """Each peer id listed under ``## Agent Delegation`` -> the instruction it is given."""
    listed = {}
    for line in _delegation_section(system).splitlines():
        match = re.match(r'- ".*?" \(id: ([0-9a-f-]{36})[^)]*\).* - (.*)$', line.replace(" \u2014 ", " - "))
        if match:
            listed[match.group(1)] = match.group(2)
    return listed


def _assert_every_listed_peer_has_its_tool(seen) -> None:
    for peer_id, instruction in _listed_peers(seen["system"]).items():
        named = [tool for tool in HANDOVER_TOOLS if f"`{tool}`" in instruction]
        assert named, f"peer {peer_id} is listed with no handover tool: {instruction!r}"
        missing = [tool for tool in named if tool not in seen["tools"]]
        assert not missing, f"peer {peer_id} is told to use {missing}, which the run does not offer"


@pytest.mark.asyncio
async def test_a_mandatory_transfer_peer_is_not_listed_in_an_assignment_run(monkeypatch):
    mandatory, consulted = _peer("Rule Router"), _peer("Reviewer")
    seen = await _run_turn(
        monkeypatch,
        [mandatory, consulted],
        {str(mandatory.id): "mandatory_transfer", str(consulted.id): "consult"},
    )

    _assert_every_listed_peer_has_its_tool(seen)
    assert set(_listed_peers(seen["system"])) == {str(consulted.id)}
    assert str(mandatory.id) not in seen["system"]
    assert "consult_agent" in seen["tools"]
    assert "spawn_background_task" not in seen["tools"]


@pytest.mark.asyncio
async def test_no_handover_tool_reaches_a_mandatory_transfer_peer(monkeypatch):
    mandatory, delegated = _peer("Rule Router"), _peer("Worker")
    consulted = _peer("Reviewer")
    seen = await _run_turn(
        monkeypatch,
        [mandatory, delegated, consulted],
        {
            str(mandatory.id): "mandatory_transfer",
            str(delegated.id): "delegate",
            str(consulted.id): "consult",
        },
        self_fanout=True,
        calls=[
            ("spawn_background_task", {"agent_id": str(mandatory.id), "task": "Route it."}),
            ("consult_agent", {"agent_id": mandatory.slug, "question": "Which rule?"}),
        ],
    )

    _assert_every_listed_peer_has_its_tool(seen)
    assert set(_listed_peers(seen["system"])) == {str(delegated.id), str(consulted.id)}
    assert {"spawn_background_task", "consult_agent"} <= seen["tools"]
    assert seen["results"] == [
        f"Agent {mandatory.id} is not available for background tasks: it is a "
        "mandatory-transfer peer of this agent, routed after the answer and never "
        "by a tool call.",
        f"Agent {mandatory.slug} is not available for consultation: it is a "
        "mandatory-transfer peer of this agent, routed after the answer and never "
        "by a tool call.",
    ]
    seen["peer_runs"].assert_not_awaited()


@pytest.mark.asyncio
async def test_an_agent_whose_only_peers_are_mandatory_gets_no_delegation_section(monkeypatch):
    mandatory = _peer("Rule Router")
    seen = await _run_turn(monkeypatch, [mandatory], {str(mandatory.id): "mandatory_transfer"})

    assert "## Agent Delegation" not in seen["system"]
    assert not seen["tools"] & set(HANDOVER_TOOLS)


@pytest.mark.asyncio
async def test_a_delegate_peer_is_not_listed_in_a_run_with_no_acting_user(monkeypatch):
    delegated, consulted = _peer("Worker"), _peer("Reviewer")
    seen = await _run_turn(
        monkeypatch,
        [delegated, consulted],
        {str(delegated.id): "delegate", str(consulted.id): "consult"},
        owned=False,
        calls=[("consult_agent", {"agent_id": str(delegated.id), "question": "Anything?"})],
    )

    _assert_every_listed_peer_has_its_tool(seen)
    assert set(_listed_peers(seen["system"])) == {str(consulted.id)}
    assert "spawn_background_task" not in seen["tools"]
    assert seen["results"] == [
        f"Agent {delegated.id} is not available for consultation: it is a delegate peer "
        "of this agent, and spawn_background_task is not offered on this turn."
    ]


@pytest.mark.asyncio
async def test_a_legacy_map_lists_every_peer_with_the_tools_the_run_registers(monkeypatch):
    first, second = _peer("Worker"), _peer("Reviewer")
    seen = await _run_turn(monkeypatch, [first, second], {}, owned=False)

    _assert_every_listed_peer_has_its_tool(seen)
    assert set(_listed_peers(seen["system"])) == {str(first.id), str(second.id)}
    assert {"consult_agent", "transfer_to_agent"} <= seen["tools"]
    assert "spawn_background_task" not in seen["tools"]


@pytest.mark.asyncio
async def test_a_type_no_tool_implements_runs_as_delegate(monkeypatch):
    peer, consulted = _peer("Worker"), _peer("Reviewer")
    seen = await _run_turn(
        monkeypatch,
        [peer, consulted],
        {str(peer.id): "custom", str(consulted.id): "consult"},
    )

    _assert_every_listed_peer_has_its_tool(seen)
    assert set(_listed_peers(seen["system"])) == {str(peer.id), str(consulted.id)}
    assert "[DELEGATE]" in _delegation_section(seen["system"])
    assert "spawn_background_task" in seen["tools"]


@pytest.mark.asyncio
async def test_a_peer_whose_only_tool_is_denied_is_not_listed(monkeypatch):
    consulted, delegated = _peer("Reviewer"), _peer("Worker")
    seen = await _run_turn(
        monkeypatch,
        [consulted, delegated],
        {str(consulted.id): "consult", str(delegated.id): "delegate"},
        tool_deny_list=["consult_agent"],
    )

    _assert_every_listed_peer_has_its_tool(seen)
    assert set(_listed_peers(seen["system"])) == {str(delegated.id)}
    assert "consult_agent" not in seen["tools"]
    assert "Parallel consulting" not in seen["system"]
    assert "spawn_background_task" in seen["tools"]


@pytest.mark.asyncio
async def test_a_denied_background_task_tool_takes_the_delegate_peers_and_self_fan_out_with_it(monkeypatch):
    delegated, consulted = _peer("Worker"), _peer("Reviewer")
    seen = await _run_turn(
        monkeypatch,
        [delegated, consulted],
        {str(delegated.id): "delegate", str(consulted.id): "consult"},
        self_fanout=True,
        tool_deny_list=["spawn_background_task"],
        calls=[("consult_agent", {"agent_id": str(delegated.id), "question": "Anything?"})],
    )

    _assert_every_listed_peer_has_its_tool(seen)
    assert set(_listed_peers(seen["system"])) == {str(consulted.id)}
    assert "spawn_background_task" not in seen["system"]
    assert "spawn_background_task" not in seen["tools"]
    assert seen["results"] == [
        f"Agent {delegated.id} is not available for consultation: it is a delegate peer "
        "of this agent, and spawn_background_task is not offered on this turn."
    ]


@pytest.mark.asyncio
async def test_each_tool_of_a_run_refuses_a_peer_of_another_type(monkeypatch):
    delegated, consulted, transferred = _peer("Worker"), _peer("Reviewer"), _peer("Closer")
    seen = await _run_turn(
        monkeypatch,
        [delegated, consulted, transferred],
        {str(delegated.id): "delegate", str(consulted.id): "consult", str(transferred.id): "transfer"},
        calls=[
            ("consult_agent", {"agent_id": str(delegated.id), "question": "Anything?"}),
            ("consult_agent", {"agent_id": transferred.slug, "question": "Anything?"}),
            ("spawn_background_task", {"agent_id": str(consulted.id), "task": "Review."}),
            ("spawn_background_task", {"agent_id": transferred.slug, "task": "Close."}),
            ("transfer_to_agent", {"agent_id": str(delegated.id), "message": "Take over."}),
            ("transfer_to_agent", {"agent_id": consulted.slug, "message": "Take over."}),
        ],
    )

    _assert_every_listed_peer_has_its_tool(seen)
    assert {"consult_agent", "spawn_background_task", "transfer_to_agent"} <= seen["tools"]
    # Each refusal names the tool that reaches the peer instead.
    assert seen["results"] == [
        f"Agent {delegated.id} is not available for consultation: it is a delegate peer "
        "of this agent - use spawn_background_task, or change its handover type to consult.",
        f"Agent {transferred.slug} is not available for consultation: it is a transfer "
        "peer of this agent - use transfer_to_agent, or change its handover type to consult.",
        f"Agent {consulted.id} is not available for background tasks: it is a consult "
        "peer of this agent - use consult_agent, or change its handover type to delegate.",
        f"Agent {transferred.slug} is not available for background tasks: it is a "
        "transfer peer of this agent - use transfer_to_agent, or change its handover "
        "type to delegate.",
        f"Agent {delegated.id} is not available for transfer.",
        f"Agent {consulted.slug} is not available for transfer.",
    ]
    seen["peer_runs"].assert_not_awaited()


@pytest.mark.asyncio
async def test_each_tool_of_a_run_still_reaches_its_own_type_and_a_legacy_peer(monkeypatch):
    consulted, transferred = _peer("Reviewer"), _peer("Closer")
    typed = await _run_turn(
        monkeypatch,
        [consulted, transferred],
        {str(consulted.id): "consult", str(transferred.id): "transfer"},
        calls=[
            ("consult_agent", {"agent_id": str(consulted.id), "question": "Anything?"}),
            ("transfer_to_agent", {"agent_id": transferred.slug, "message": "Take over."}),
        ],
    )
    assert not any("not available" in result for result in typed["results"])
    assert typed["peer_runs"].await_count == 2

    legacy = _peer("Worker")
    untyped = await _run_turn(
        monkeypatch,
        [legacy],
        {},
        calls=[
            ("consult_agent", {"agent_id": str(legacy.id), "question": "Anything?"}),
            ("transfer_to_agent", {"agent_id": legacy.slug, "message": "Take over."}),
        ],
    )
    assert not any("not available" in result for result in untyped["results"])
    assert untyped["peer_runs"].await_count == 2


@pytest.mark.asyncio
async def test_self_fan_out_is_still_offered_when_nothing_denies_it(monkeypatch):
    consulted = _peer("Reviewer")
    seen = await _run_turn(monkeypatch, [consulted], {str(consulted.id): "consult"}, self_fanout=True)

    _assert_every_listed_peer_has_its_tool(seen)
    assert "spawn_background_task" in seen["tools"]
    assert "Parallel self-execution" in seen["system"]

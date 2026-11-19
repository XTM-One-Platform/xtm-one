"""Sandbox copies against a real PostgreSQL (#8255): the copy lifecycle, the
hiding, the recordings a run replays, the purge, and a sandbox flow run.

Same scratch-database recipe as ``test_flow_runner_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.helpers import get_user_agent, user_visible_query
from app.models.agent import Agent
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.llm_trace import LLMSpan, LLMTrace
from app.models.sandbox_copy import SandboxCopy, SandboxRun
from app.models.user import User
from app.services.flow_runner import FlowWalker
from app.services.health.sandbox import copies, runs, turns
from app.services.health.sandbox import tools as sandbox_tools
from app.services.health.sandbox.replay import load_recorded_calls
from app.services.tracing.read_store import trace_visible_query
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


_REAL_SUBJECT_REFUSAL = sandbox_tools.subject_refusal


@pytest.fixture(autouse=True)
def _subject_in_reach(monkeypatch):
    """Every run re-reads its subject's scope and consent (level 1's rule,
    pinned in ``test_health_consent_postgres.py``): held open here, but by
    the test that restores :data:`_REAL_SUBJECT_REFUSAL`."""

    async def _in_reach(_db, _type, _id):
        return None

    monkeypatch.setattr(sandbox_tools, "subject_refusal", _in_reach)


@pytest.fixture
async def sessions():
    async with scratch_database("sandbox", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _user(session, *, admin=False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"sandbox-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Sandbox Test",
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _agent(session, owner, **fields) -> Agent:
    agent = Agent(
        id=uuid.uuid4(), user_id=owner.id, name="Triage", slug=f"triage-{uuid.uuid4().hex[:6]}", persona="Be brief.", **fields
    )
    session.add(agent)
    await session.flush()
    return agent


async def test_an_agent_copy_is_hidden_from_everyone_and_runs_as_its_source(sessions):
    async with sessions() as session:
        owner = await _user(session)
        admin = await _user(session, admin=True)
        source = await _agent(session, owner, health_enrolled=True)
        copy = await copies.create_sandbox_copy(
            session,
            source_type="agent",
            source_id=source.id,
            created_for="finding:1",
            overrides=copies.SandboxOverrides(persona="Be thorough."),
        )
        await session.commit()

        agent = await session.get(Agent, copy.copy_agent_id)
        assert (agent.is_sandbox, agent.user_id, agent.is_company_managed) == (True, None, False)
        assert (agent.persona, agent.health_enrolled) == ("Be thorough.", False)
        assert abs(copy.expires_at - copy.created_at - copies.SANDBOX_COPY_TTL) < timedelta(seconds=5)

        for user in (owner, admin):
            listed = (await session.execute(user_visible_query(Agent, user.id, [], is_admin=user.is_admin))).scalars().all()
            assert [a.id for a in listed] == [source.id]
            with pytest.raises(HTTPException) as refused:
                await get_user_agent(session, user.id, str(agent.id), [], is_admin=user.is_admin)
            assert refused.value.status_code == 404

        assert await runs.runner_user_id(session, copy) == owner.id
        assert await copies.sandbox_copy_of(session, agent_id=agent.id) == copy


async def test_a_copys_traces_stay_out_of_an_admins_analytics(sessions):
    async with sessions() as session:
        owner = await _user(session)
        admin = await _user(session, admin=True)
        source = await _agent(session, owner)
        copy = await copies.create_sandbox_copy(session, source_type="agent", source_id=source.id, created_for="x")
        now = datetime.now(UTC)
        for agent_id in (source.id, copy.copy_agent_id, None):
            session.add(LLMTrace(agent_id=agent_id, user_id=owner.id, source="agent-chat", name="t", started_at=now))
        await session.commit()
        visible = (await session.execute(trace_visible_query(admin, []))).scalars().all()
        assert sorted(str(t.agent_id) for t in visible) == sorted([str(source.id), "None"])


async def test_recordings_skip_answered_spans_and_copies_traces(sessions):
    async with sessions() as session:
        owner = await _user(session)
        source = await _agent(session, owner)
        copy = await copies.create_sandbox_copy(session, source_type="agent", source_id=source.id, created_for="x")
        now = datetime.now(UTC)
        real = LLMTrace(id=uuid.uuid4(), agent_id=source.id, user_id=owner.id, source="agent-chat", name="t", started_at=now)
        mine = LLMTrace(id=uuid.uuid4(), agent_id=copy.copy_agent_id, user_id=owner.id, source="agent-chat", name="t", started_at=now)
        session.add_all([real, mine])
        await session.flush()

        def _span(trace, position, name, output, **meta):
            return LLMSpan(
                trace_id=trace.id,
                type="TOOL",
                name=f"tool:{name}",
                position=position,
                started_at=now,
                tool_arguments={"id": "7"},
                output=output,
                metadata_json={"tool_name": name, **meta},
            )

        session.add_all(
            [
                _span(real, 1, "get_case", {"title": "recorded"}),
                _span(real, 2, "create_case", "simulated!", sandbox_outcome="simulated"),
                _span(mine, 1, "get_case", "from a copy"),
            ]
        )
        await session.commit()
        recorded = await load_recorded_calls(session, [real.id, mine.id], excluded_agent_ids=[copy.copy_agent_id])
        assert [(c.tool, c.arguments, json.loads(c.output), c.user_id) for c in recorded] == [
            ("get_case", {"id": "7"}, {"title": "recorded"}, str(owner.id))
        ]


async def test_the_purge_keeps_what_is_referenced_or_retained(sessions):
    async with sessions() as session:
        owner = await _user(session)
        source = await _agent(session, owner)
        made = [
            await copies.create_sandbox_copy(session, source_type="agent", source_id=source.id, created_for=str(i))
            for i in range(3)
        ]
        expired, referenced, retained = made
        past = datetime.now(UTC) - timedelta(days=1)
        for row in made:
            row.expires_at = past
        session.add(LLMTrace(agent_id=expired.copy_agent_id, source="agent-chat", name="t", started_at=past))
        await copies.retain_sandbox_copy(session, retained.id, until=datetime.now(UTC) + timedelta(days=3))
        await session.commit()

        async def _open_proposals(_db, ids):
            return {referenced.id} & set(ids)

        copies.register_sandbox_reference_check(_open_proposals)
        try:
            assert await copies.purge_expired_sandbox_copies(session) == 1
            await session.commit()
        finally:
            copies._reference_checks.remove(_open_proposals)
        kept = set((await session.execute(select(SandboxCopy.id))).scalars())
        assert kept == {referenced.id, retained.id}
        # Retained, so the next pass reaches the expired copies behind it.
        await session.refresh(referenced)
        assert referenced.expires_at > datetime.now(UTC) + copies.SANDBOX_COPY_TTL - timedelta(minutes=1)
        assert await session.get(Agent, expired.copy_agent_id) is None
        assert await session.scalar(select(func.count()).select_from(LLMTrace)) == 0


def _node(node_id, kind, **config):
    return {"id": node_id, "kind": kind, "position": {"x": 0, "y": 0}, "config": config}


def _edge(source, target):
    return {"id": f"e-{source}-{target}", "source": source, "source_handle": "out", "target": target, "target_handle": "in", "kind": "data"}


async def test_a_sandbox_flow_run_never_sends_its_request_and_counts_it(sessions, monkeypatch):
    sent: list = []

    async def _send(self, *args, **kwargs):
        sent.append(args)
        raise AssertionError("a sandbox run sent a request")

    monkeypatch.setattr(FlowWalker, "_send_http_request", _send)

    async def _simulate(tool, arguments, recorded, description):
        return '{"status": 200, "body": "simulated"}', 5

    monkeypatch.setattr(turns, "model_simulator", lambda *_a, **_k: _simulate)

    async with sessions() as session:
        owner = await _user(session)
        graph = {
            "nodes": [
                _node("start", "trigger.manual"),
                _node("call", "action.http_request", url="https://example.invalid/hook", method="POST"),
            ],
            "edges": [_edge("start", "call")],
        }
        source = Flow(id=uuid.uuid4(), user_id=owner.id, name="Notify", graph=graph)
        session.add(source)
        await session.flush()
        copy = await copies.create_sandbox_copy(session, source_type="flow", source_id=source.id, created_for="x")
        run = await runs.start_sandbox_flow_run(session, copy, case_ref="case-1")
        await session.commit()
        assert run.user_id == owner.id

        await FlowWalker(session, run, owner).run_burst()
        await session.commit()

        run = await session.get(FlowRun, run.id)
        assert sent == []
        assert run.status == "completed"
        assert run.state["sandbox"]["simulated_writes"] == 1
        report = await runs.sandbox_run_report(session, flow_run_id=run.id)
        assert (report.simulated_writes, report.intercepted_writes) == (1, 1)
        row = (await session.execute(select(SandboxRun).where(SandboxRun.flow_run_id == run.id))).scalar_one()
        assert (row.status, row.simulated_writes) == ("completed", 1)
        assert await runs.simulated_write_counts(session, copy_id=copy.id) == {"case-1": 1}


async def test_a_new_flow_prototype_has_no_source(sessions):
    async with sessions() as session:
        graph = {"nodes": [_node("start", "trigger.manual")], "edges": []}
        copy = await copies.create_sandbox_copy(
            session, source_type="flow", source_id=None, created_for="x", overrides=copies.SandboxOverrides(graph=graph)
        )
        await session.commit()
        flow = await session.get(Flow, copy.copy_flow_id)
        assert (flow.is_sandbox, flow.user_id, flow.graph, copy.source_id) == (True, None, graph, None)
        with pytest.raises(runs.SandboxRunError):
            await runs.start_sandbox_flow_run(session, copy)
        owner = await _user(session)
        run = await runs.start_sandbox_flow_run(session, copy, purpose="shadow", run_as_user_id=owner.id)
        assert run.user_id == owner.id


async def _write_once(name="create_ticket"):
    from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition

    ran: list[str] = []

    async def _handler(**_kwargs):
        ran.append(name)
        return "live"

    executor = CallableToolExecutor()
    executor.register(name, _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name=name, description="d")], executor)
    result = await composite.execute_tool(ToolCall(id="c1", name=name, arguments={"title": "x"}))
    return ran, result


async def test_a_case_stores_its_exact_report_even_when_it_fails(sessions, monkeypatch):
    async def _simulate(tool, arguments, recorded, description):
        return '{"id": "T-9"}', 3

    monkeypatch.setattr(turns, "model_simulator", lambda *_a, **_k: _simulate)
    async with sessions() as session:
        owner = await _user(session)
        source = await _agent(session, owner)
        copy = await copies.create_sandbox_copy(session, source_type="agent", source_id=source.id, created_for="x")
        async with runs.sandbox_case(session, copy, case_ref="entry-1", recorded_calls=[]) as case:
            ran, result = await _write_once()
        assert (ran, result.content) == ([], '{"id": "T-9"}')
        with pytest.raises(RuntimeError):
            async with runs.sandbox_case(session, copy, case_ref="entry-2", recorded_calls=[]):
                await _write_once()
                raise RuntimeError("the turn failed")
        async with runs.sandbox_case(session, copy, case_ref="entry-3", recorded_calls=[]):
            with pytest.raises(runs.SandboxRunError):
                async with runs.sandbox_case(session, copy, case_ref="nested", recorded_calls=[]):
                    pass
        await session.commit()
        rows = (await session.execute(select(SandboxRun).order_by(SandboxRun.case_ref))).scalars().all()
        assert [(r.case_ref, r.status, r.simulated_writes) for r in rows] == [
            ("entry-1", "completed", 1),
            ("entry-2", "failed", 1),
            ("entry-3", "completed", 0),
        ]
        assert rows[0].id == case.run_id
        assert rows[0].report["simulation_tokens"] == 3
        report = await runs.sandbox_run_report(session, sandbox_run_id=case.run_id)
        assert (report.case_id, report.simulated_writes, report.intercepted_writes) == ("entry-1", 1, 1)
        assert await runs.simulated_write_counts(session, copy_id=copy.id) == {"entry-1": 1, "entry-2": 1, "entry-3": 0}


async def test_an_agent_copy_answers_a_case_as_an_evaluation_turn(sessions, monkeypatch):
    seen: dict = {}

    async def _simulate(tool, arguments, recorded, description):
        return "sent", 2

    async def _turn(db, user_id, agent, history, prompt, **kwargs):
        seen.update(user_id=user_id, agent=agent.id, prompt=prompt, **kwargs)
        await _write_once("send_mail")
        kwargs["eval_result_out"]["llm_trace_id"] = "t-1"
        return "the answer", None

    monkeypatch.setattr(turns, "model_simulator", lambda *_a, **_k: _simulate)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _turn)
    async with sessions() as session:
        owner = await _user(session)
        source = await _agent(session, owner)
        copy = await copies.create_sandbox_copy(session, source_type="agent", source_id=source.id, created_for="x")
        result = await runs.run_sandbox_agent(
            session, copy, input_prompt="hello", case_ref="c", run_overrides={"persona": "v2"}
        )
        await session.commit()
    assert (result.output, result.trace_ids, result.report.simulated_writes) == ("the answer", ["t-1"], 1)
    assert (seen["user_id"], seen["agent"], seen["prompt"]) == (str(owner.id), copy.copy_agent_id, "hello")
    assert (seen["evaluation_mode"], seen["evaluation_overrides"]) == (True, {"persona": "v2"})
    # No approval gate holds a sandbox call: nobody could answer it, and the
    # interceptor, not a person, decides what runs.
    assert seen["evaluation_tool_approval"] is False
    assert result.sandbox_run_id is not None


async def test_a_flow_called_from_a_sandbox_run_is_intercepted(sessions):
    from app.services.health.sandbox.flows import flow_burst_session

    async with sessions() as session:
        owner = await _user(session)
        production = Flow(id=uuid.uuid4(), user_id=owner.id, name="Production", graph={"nodes": [], "edges": []})
        session.add(production)
        await session.flush()
        copy = await copies.create_sandbox_copy(session, source_type="flow", source_id=production.id, created_for="x")
        parent = FlowRun(flow_id=copy.copy_flow_id, flow_version_number=1, graph={}, user_id=owner.id, status="running", trigger_type="manual")
        session.add(parent)
        await session.flush()
        child = FlowRun(flow_id=production.id, flow_version_number=1, graph={}, user_id=owner.id, status="pending", trigger_type="flow", parent_run_id=parent.id)
        alone = FlowRun(flow_id=production.id, flow_version_number=1, graph={}, user_id=owner.id, status="pending", trigger_type="manual")
        session.add_all([child, alone])
        await session.commit()
        assert (await flow_burst_session(session, child, owner)).mode == "sandbox"
        assert await flow_burst_session(session, alone, owner) is None


async def test_an_agent_node_of_a_sandbox_flow_skips_the_approval_gate(sessions, monkeypatch):
    seen: list[dict] = []

    async def _turn(db, user_id, agent, history, prompt, **kwargs):
        seen.append(kwargs)
        return "done", None

    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _turn)
    async with sessions() as session:
        owner = await _user(session)
        agent = await _agent(session, owner, approve_tool_calls=True)
        graph = {
            "nodes": [_node("start", "trigger.manual"), _node("ask", "agent", agent_id=str(agent.id), prompt="go")],
            "edges": [_edge("start", "ask")],
        }
        source = Flow(id=uuid.uuid4(), user_id=owner.id, name="Ask", graph=graph)
        session.add(source)
        await session.flush()
        copy = await copies.create_sandbox_copy(session, source_type="flow", source_id=source.id, created_for="x")
        run = await runs.start_sandbox_flow_run(session, copy)
        production = FlowRun(flow_id=source.id, flow_version_number=1, graph=graph, user_id=owner.id, status="pending", trigger_type="manual")
        session.add(production)
        await session.commit()
        await FlowWalker(session, run, owner).run_burst()
        await FlowWalker(session, production, owner).run_burst()
    assert [(k["evaluation_mode"], k["evaluation_tool_approval"]) for k in seen] == [(True, False), (False, None)]


async def test_a_copy_is_reachable_from_no_channel(sessions):
    from app.channels import router
    from app.channels.base import ChannelType, InboundMessage

    slack = {"slack": {"receive_enabled": True, "bypass_whitelist": True}}
    group = InboundMessage(channel=ChannelType.SLACK, text="hi", metadata={"channel_type": "channel"})
    async with sessions() as session:
        owner = await _user(session)
        stranger = await _user(session)
        source = await _agent(session, owner, channel_config=slack)
        copy = await copies.create_sandbox_copy(session, source_type="agent", source_id=source.id, created_for="x")
        hidden = await session.get(Agent, copy.copy_agent_id)
        assert hidden.channel_config is None
        routed = await router._resolve_channel_agent(session, stranger.id, ChannelType.SLACK, group)
        assert routed.id == source.id
        # A copy that carries a channel entry anyway is never routed to.
        hidden.channel_config = slack
        source.enabled = False
        await session.flush()
        assert await router._any_agent_bypasses_whitelist(session, ChannelType.SLACK) is False
        assert await router._resolve_channel_agent(session, stranger.id, ChannelType.SLACK, group) is None


async def test_a_flow_copy_runs_only_what_its_source_names(sessions):
    async with sessions() as session:
        owner = await _user(session)
        held, other = str(uuid.uuid4()), str(uuid.uuid4())
        graph = {
            "nodes": [
                _node("start", "trigger.manual"),
                _node("read", "integration", integration_id=held, operation="list_alerts"),
            ],
            "edges": [_edge("start", "read")],
        }
        source = Flow(id=uuid.uuid4(), user_id=owner.id, name="Alerts", graph=graph)
        session.add(source)
        await session.flush()
        same = await copies.create_sandbox_copy(session, source_type="flow", source_id=source.id, created_for="x")
        assert (await runs.start_sandbox_flow_run(session, same)).user_id == owner.id
        # Another integration of the same owner: a credential the source never held.
        widened = json.loads(json.dumps(graph).replace(held, other))
        variant = await copies.create_sandbox_copy(
            session,
            source_type="flow",
            source_id=source.id,
            created_for="x",
            overrides=copies.SandboxOverrides(graph=widened),
        )
        with pytest.raises(runs.SandboxRunError):
            await runs.start_sandbox_flow_run(session, variant)
        started = select(func.count()).select_from(SandboxRun).where(SandboxRun.copy_id == variant.id)
        assert await session.scalar(started) == 0


async def test_a_flow_called_from_a_sandbox_run_records_nothing_and_lists_nowhere(sessions, monkeypatch):
    from app.models.flow_run import FlowRunStep
    from app.services.health.sandbox.flows import SANDBOX_RUN_SOURCE, load_recorded_steps, production_runs_only

    async def _send(self, *args, **kwargs):
        raise AssertionError("a sandbox run sent a request")

    async def _simulate(tool, arguments, recorded, description):
        return '{"status": 200, "body": "simulated"}', 5

    monkeypatch.setattr(FlowWalker, "_send_http_request", _send)
    monkeypatch.setattr(turns, "model_simulator", lambda *_a, **_k: _simulate)
    async with sessions() as session:
        owner = await _user(session)
        production = Flow(
            id=uuid.uuid4(),
            user_id=owner.id,
            name="Notify",
            graph={
                "nodes": [
                    _node("start", "trigger.manual"),
                    _node("call", "action.http_request", url="https://example.invalid", method="POST"),
                ],
                "edges": [_edge("start", "call")],
            },
        )
        caller = Flow(
            id=uuid.uuid4(),
            user_id=owner.id,
            name="Caller",
            graph={
                "nodes": [
                    _node("start", "trigger.manual"),
                    _node("sub", "flow.call", flow_id=str(production.id), input="go"),
                ],
                "edges": [_edge("start", "sub")],
            },
        )
        session.add_all([production, caller])
        real = FlowRun(
            flow_id=production.id, flow_version_number=1, graph={}, user_id=owner.id, status="completed", trigger_type="manual"
        )
        session.add(real)
        await session.flush()
        session.add(
            FlowRunStep(
                flow_run_id=real.id,
                seq=0,
                node_id="call",
                node_kind="action.http_request",
                status="completed",
                input={},
                output={"status": 200},
            )
        )
        copy = await copies.create_sandbox_copy(session, source_type="flow", source_id=caller.id, created_for="x")
        run = await runs.start_sandbox_flow_run(session, copy)
        await session.commit()

        await FlowWalker(session, run, owner).run_burst()
        await session.commit()
        child = (await session.execute(select(FlowRun).where(FlowRun.parent_run_id == run.id))).scalar_one()
        assert child.flow_id == production.id
        assert child.trigger_context["source"] == SANDBOX_RUN_SOURCE
        await FlowWalker(session, child, owner).run_burst()
        await session.commit()
        called = select(FlowRunStep).where(FlowRunStep.flow_run_id == child.id, FlowRunStep.node_id == "call")
        assert [s.output.get("sandbox_outcome") for s in (await session.execute(called)).scalars()] == ["simulated"]

        # Neither the called run nor its simulated step is a recording, even named.
        recorded = await load_recorded_steps(session, [child.id, real.id], flow_id=production.id)
        assert [(r.tool, r.trace_id) for r in recorded] == [("call:action.http_request", str(real.id))]
        listed = select(FlowRun.id).where(FlowRun.flow_id == production.id, production_runs_only())
        assert (await session.execute(listed)).scalars().all() == [real.id]


async def test_a_flow_case_replays_its_entrys_recordings(sessions, monkeypatch):
    from app.services.health.sandbox.flows import ENTRY_KEY, flow_burst_session
    from app.services.health.sandbox.replay import RecordedCall

    entry_id = uuid.uuid4()
    held = {entry_id: [RecordedCall(tool="get_case", arguments={"id": "7"}, output="from the entry")]}

    async def _entry_recordings(_db, ids):
        return {i: held[i] for i in ids if i in held}

    monkeypatch.setattr("app.services.health.datasets.entry_recordings", _entry_recordings)
    async with sessions() as session:
        owner = await _user(session)
        source = Flow(id=uuid.uuid4(), user_id=owner.id, name="Triage", graph={"nodes": [_node("start", "trigger.manual")], "edges": []})
        session.add(source)
        await session.flush()
        copy = await copies.create_sandbox_copy(session, source_type="flow", source_id=source.id, created_for="x")
        run = await runs.start_sandbox_flow_run(session, copy, entry_id=entry_id)
        assert run.trigger_context[ENTRY_KEY] == str(entry_id)
        replaying = await flow_burst_session(session, run, owner)
        replaying.consent = None  # level 1's rule is not under test
        assert (await replaying.recorded("get_case", {"id": "7"})).output == "from the entry"
        # An entry with no recordings of its own falls back on the copy's (none here).
        held.clear()
        fallback = await flow_burst_session(session, run, owner)
        fallback.consent = None
        assert await fallback.recorded("get_case", {"id": "7"}) is None


@pytest.mark.parametrize("cap", [None, 1])
async def test_a_flow_case_replays_the_node_steps_of_the_run_its_entry_came_from(sessions, monkeypatch, cap):
    """With the replay index full of the entry's own tool calls (*cap*), the
    node steps are still kept: a flow node without one would read live."""
    from app.models.flow_run import FlowRunStep
    from app.services.health.sandbox import flows, replay
    from app.services.health.sandbox.replay import RecordedCall

    if cap is not None:
        monkeypatch.setattr(replay, "MAX_RECORDED_CALLS", cap)

    entry_id = uuid.uuid4()

    async def _entry_recordings(_db, ids):
        return {entry_id: [RecordedCall(tool="get_case", arguments={"id": "7"}, output="from the entry")]}

    monkeypatch.setattr("app.services.health.datasets.entry_recordings", _entry_recordings)
    async with sessions() as session:
        owner = await _user(session)
        source = Flow(
            id=uuid.uuid4(),
            user_id=owner.id,
            name="Enrich",
            graph={
                "nodes": [_node("start", "trigger.manual"), _node("fetch", "action.http_request", url="https://example.invalid")],
                "edges": [_edge("start", "fetch")],
            },
        )
        session.add(source)
        built_from = FlowRun(
            flow_id=source.id, flow_version_number=1, graph={}, user_id=owner.id, status="completed", trigger_type="manual"
        )
        session.add(built_from)
        await session.flush()
        session.add(
            FlowRunStep(
                flow_run_id=built_from.id,
                seq=0,
                node_id="fetch",
                node_kind="action.http_request",
                status="completed",
                input={},
                output={"status": 200, "body": "recorded"},
            )
        )
        await session.flush()

        async def _source_run(_db, wanted):
            return (built_from.id, source.id) if wanted == entry_id else None

        monkeypatch.setattr(flows, "_entry_source_run", _source_run)
        copy = await copies.create_sandbox_copy(session, source_type="flow", source_id=source.id, created_for="x")
        run = await runs.start_sandbox_flow_run(session, copy, entry_id=entry_id)
        replaying = await flows.flow_burst_session(session, run, owner)
        replaying.consent = None  # level 1's rule is not under test

        # The entry's own tool calls and the node steps of the run it came from, together.
        if cap is None:
            assert (await replaying.recorded("get_case", {"id": "7"})).output == "from the entry"
        step = await replaying.recorded(flows.node_recording_key("fetch", "action.http_request"), {})
        assert step is not None and json.loads(step.output)["body"] == "recorded"


async def test_a_copys_runs_stop_once_its_source_or_basis_is_out_of_reach(sessions, monkeypatch):
    """Read on the rows at every run, whoever starts it: a source not opted in,
    or a prototype's basis agent not opted in, refuses the run before anything
    is recorded; a prototype standing for nothing never runs as the
    administrator the Concierge acts for."""
    from app.models.platform_settings import PlatformSettings
    from app.services.health.flow_lab_access import BASIS_AGENT_PREFIX
    from app.services.health.identity import HealthPrincipal, acting_as_health_principal
    from app.services.health.settings import HealthSettings

    monkeypatch.setattr(sandbox_tools, "subject_refusal", _REAL_SUBJECT_REFUSAL)
    graph = {"nodes": [_node("start", "trigger.manual")], "edges": []}
    async with sessions() as session:
        session.add(PlatformSettings(health_settings=HealthSettings(enabled=True).model_dump(mode="json")))
        owner = await _user(session)
        admin = await _user(session, admin=True)
        opted_in = await _agent(session, owner, health_enrolled=True)
        opted_out = await _agent(session, owner)
        source = Flow(id=uuid.uuid4(), user_id=owner.id, name="Not opted in", graph=graph)
        session.add(source)
        await session.flush()
        flow_copy = await copies.create_sandbox_copy(session, source_type="flow", source_id=source.id, created_for="x")

        def _prototype(created_for):
            return copies.create_sandbox_copy(
                session, source_type="flow", source_id=None, created_for=created_for, overrides=copies.SandboxOverrides(graph=graph)
            )

        refused = await _prototype(f"{BASIS_AGENT_PREFIX}{opted_out.id}")
        allowed = await _prototype(f"{BASIS_AGENT_PREFIX}{opted_in.id}")
        standing_for_nothing = await _prototype("finding:1")
        agent_copy = await copies.create_sandbox_copy(session, source_type="agent", source_id=opted_out.id, created_for="x")
        await session.commit()

        principal = HealthPrincipal(agent_id=uuid.uuid4(), role="experimenter", on_behalf_of_admin_id=admin.id)
        with acting_as_health_principal(principal):
            for copy in (flow_copy, refused, standing_for_nothing):
                with pytest.raises(runs.SandboxRunError):
                    await runs.start_sandbox_flow_run(session, copy)
            with pytest.raises(runs.SandboxRunError):
                async with runs.sandbox_case(session, agent_copy, recorded_calls=[]):
                    pass
            run = await runs.start_sandbox_flow_run(session, allowed)
        # The basis agent's owner, never the administrator the Concierge acts for.
        assert run.user_id == owner.id
        rows = (await session.execute(select(SandboxRun.copy_id))).scalars().all()
        assert rows == [allowed.id]

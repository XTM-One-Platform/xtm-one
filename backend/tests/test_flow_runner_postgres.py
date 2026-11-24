"""Walker integration tests against a real PostgreSQL.

Same scratch-database recipe as ``test_trace_daily_stats_postgres.py``: a
throwaway database is created from ``DATABASE_URL`` (skipped when the user
lacks CREATEDB), the whole schema is created in it, the app's own session
factories are bound to it for the test (``tests/scratch_database.py``), and the
database is dropped afterwards. Marked ``integration``: CI's integration job
is the one with a PostgreSQL service.

What is deliberately exercised end to end here — the things a fake session
would mostly fake: the step journal (order, iterations, statuses), branch
selection, loop advancement on queue drain, merge strategies, error-port
routing, park/resume across two bursts with the cursor persisted in
``run.state``, sub-flow completion, cancellation, and the adapter contract
(terminal-status guard, outbox self-resume on park).

The agent node is monkeypatched — these tests assert the *walker's* contract
with ``run_agent_chat`` (prompt rendering, taint wrapping, allow_handover,
persona pass-through), not the LLM.
"""

import contextlib
import json
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import Base
from app.models.agent import Agent
from app.models.custom_tool import CustomTool
from app.models.flow import Flow
from app.models.flow_event import FlowEvent, FlowEventType
from app.models.flow_run import FlowRun, FlowRunStep
from app.models.group import Group, GroupMember
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.orchestration import (
    OrchestrationExecution,
    OrchestrationOutboxEvent,
)
from app.models.user import User
from app.models.variable import Variable
from app.models.workset import Workset, WorksetItem
from app.services import flow_runner
from app.services.flow_runner import FlowWalker, create_flow_run
from app.services.orchestration_adapters.base import AdapterContext
from app.services.orchestration_adapters.flow import FlowAdapter
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

# Not "not integration": the Backend CI job has no PostgreSQL, so there every
# test here skipped at `_temp_database` and the file never ran anywhere.
pytestmark = pytest.mark.integration

_TABLES = [
    User.__table__,
    # LLM-mode candidate resolution reads the caller's visible custom tools.
    CustomTool.__table__,
    # A workset's corpus lives in the KB tables; the action nodes write there,
    # and `worksets` carries FKs to `agents` (owner and processor).
    Agent.__table__,
    KnowledgeBase.__table__,
    KnowledgeDocument.__table__,
    Workset.__table__,
    WorksetItem.__table__,
    # Group visibility: the runner-command exec resolves the caller's groups.
    Group.__table__,
    GroupMember.__table__,
    # The renderer loads platform variables ({{var_id.*}}) on every burst.
    Variable.__table__,
    Flow.__table__,
    FlowEventType.__table__,
    FlowEvent.__table__,
    FlowRun.__table__,
    FlowRunStep.__table__,
    OrchestrationExecution.__table__,
    OrchestrationOutboxEvent.__table__,
]


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    # The WHOLE schema, not a hand-picked subset. The subset was cheaper, but
    # it made every new FK a dependency chain to chase (workset -> agents ->
    # file_attachments ...), and a missing table fails as an unrelated-looking
    # error in tests that never touch it. `embeddings` carries a pgvector
    # column, hence the extension. The app's session factories follow the
    # scratch database (``bind_app_sessions``, the default).
    async with scratch_database("flowrun", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.mark.asyncio
async def test_the_app_session_factory_follows_the_scratch_database(monkeypatch):
    # Code under test opens sessions of its own (the licence read behind the
    # AI gate, the work-item sweep, the trace sink). Left on the process-wide
    # factory they reach DATABASE_URL's database - missing these tables - on a
    # pool that outlives each test's event loop: the order-dependent failures.
    from app.core import database
    from app.services.tracing import local_sink

    app_factory = database.async_session
    traces_factory = local_sink._build_tracing_session_factory()
    traces_bind = traces_factory.kw.get("bind")
    with monkeypatch.context() as patch:
        async with _temp_database() as sessions, sessions() as session:
            scratch = (await session.execute(text("SELECT current_database()"))).scalar_one()
            async with database.async_session() as own:
                assert (await own.execute(text("SELECT current_database()"))).scalar_one() == scratch
            async with traces_factory() as traces:
                assert (await traces.execute(text("SELECT current_database()"))).scalar_one() == scratch
            # A test replacing the module attribute must not redirect the
            # restore: the factory OBJECT is what gets rebound back.
            patch.setattr(database, "async_session", sessions)
        assert app_factory.kw["bind"] is database.engine
        assert traces_factory.kw.get("bind") is traces_bind


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """Flow triggers and event dispatch ask the background gate first,
    and the gate reads the licence on the app's own engine, not this throwaway
    database; these tests are about the walker once agentic AI may run."""
    stub_agentic_gate(monkeypatch)


def _node(node_id, kind, **config):
    return {
        "id": node_id,
        "kind": kind,
        "position": {"x": 0, "y": 0},
        "config": config,
    }


def _edge(source, target, *, sh="out", th="in", kind="data", label=""):
    return {
        "id": f"e-{source}-{sh}-{target}",
        "source": source,
        "source_handle": sh,
        "target": target,
        "target_handle": th,
        "kind": kind,
        "label": label,
    }


async def _seed(session, *, graph, name="Test flow"):
    user = User(
        id=uuid.uuid4(),
        email=f"walker-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Walker Test",
        hashed_password="x",
    )
    session.add(user)
    # Flush before adding the flow: Flow.user_id has no relationship() to
    # User, so the unit of work sees two independent mappers and will not
    # order the inserts by the FK on its own.
    await session.flush()
    flow = Flow(id=uuid.uuid4(), user_id=user.id, name=name, graph=graph)
    session.add(flow)
    await session.commit()
    return user, flow


async def _run_to_outcome(session, user, flow, *, payload=None, context=None):
    run = await create_flow_run(
        session,
        flow,
        trigger_type="manual",
        trigger_payload=payload or {},
        trigger_context=context or {"type": "manual"},
        user_id=user.id,
    )
    await session.commit()
    outcome = await FlowWalker(session, run, user).run_burst()
    await session.commit()
    return run, outcome


async def _steps(session, run):
    rows = (
        (
            await session.execute(
                select(FlowRunStep)
                .where(FlowRunStep.flow_run_id == run.id)
                .order_by(FlowRunStep.seq)
            )
        )
        .scalars()
        .all()
    )
    return rows


# ── Linear walk, branches, merge ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_condition_routes_the_true_branch_and_merge_concats():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "c1", "condition.if", left="{{t1.payload.severity}}", operator="gt", right="5"
            ),
            _node("hi", "condition.if", left="high", operator="is_not_empty"),
            _node("lo", "condition.if", left="low", operator="is_not_empty"),
            _node("m1", "util.merge", strategy="concat"),
        ],
        "edges": [
            _edge("t1", "c1"),
            _edge("c1", "hi", sh="true", kind="condition_branch"),
            _edge("c1", "lo", sh="false", kind="condition_branch"),
            _edge("hi", "m1", sh="true", kind="condition_branch"),
            _edge("lo", "m1", sh="true", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload={"severity": "8"}
        )

        assert outcome.kind == "completed"
        assert run.status == "completed"
        steps = await _steps(session, run)
        walked = [s.node_id for s in steps]
        assert walked == ["t1", "c1", "hi", "m1"], (
            "false branch must not execute; merge fires once drained"
        )
        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.output == {"result": True}


@pytest.mark.asyncio
async def test_switch_follows_the_matching_case_edge_label():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "sw",
                "condition.switch",
                value="{{t1.payload.kind}}",
                cases={"incident": "incident", "alert": "alert"},
            ),
            _node("inc", "condition.if", left="x", operator="is_not_empty"),
            _node("al", "condition.if", left="x", operator="is_not_empty"),
            _node("df", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "sw"),
            _edge("sw", "inc", sh="case", kind="condition_branch", label="incident"),
            _edge("sw", "al", sh="case", kind="condition_branch", label="alert"),
            _edge("sw", "df", sh="default", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload={"kind": "alert"}
        )
        assert outcome.kind == "completed"
        walked = [s.node_id for s in await _steps(session, run)]
        assert "al" in walked
        assert "inc" not in walked and "df" not in walked


@pytest.mark.asyncio
async def test_merge_first_wins_fires_once():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a", "condition.if", left="1", operator="is_not_empty"),
            _node("b", "condition.if", left="1", operator="is_not_empty"),
            _node("m", "util.merge", strategy="first_wins"),
        ],
        "edges": [
            _edge("t1", "a"),
            _edge("t1", "b"),
            _edge("a", "m", sh="true", kind="condition_branch"),
            _edge("b", "m", sh="true", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        merges = [
            s
            for s in await _steps(session, run)
            if s.node_id == "m" and s.status == "completed"
        ]
        assert len(merges) == 1, "first_wins fires exactly once"


# ── Loop ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_iterates_each_item_then_fires_done():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("lp", "util.loop", items='["a", "b", "c"]'),
            _node(
                "body",
                "condition.if",
                left="{{lp.item}}",
                operator="is_not_empty",
            ),
            _node("after", "condition.if", left="fin", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "lp"),
            _edge("lp", "body", sh="each", kind="condition_branch"),
            _edge("lp", "after", sh="done", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        body_steps = [s for s in steps if s.node_id == "body"]
        assert len(body_steps) == 3, "one body pass per item"
        loop_steps = [s for s in steps if s.node_id == "lp"]
        # 3 `each` passes + 1 `done`
        assert len(loop_steps) == 4
        assert [s.iteration for s in loop_steps] == [0, 1, 2, 3]
        assert "after" in [s.node_id for s in steps]
        # {{lp.item}} resolved to the current pass's item each time.
        items = [s.output.get("item") for s in loop_steps[:3]]
        assert items == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_loop_max_iterations_caps_the_items():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("lp", "util.loop", items='["a","b","c","d"]', max_iterations=2),
            _node("body", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "lp"),
            _edge("lp", "body", sh="each", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        body_steps = [
            s for s in await _steps(session, run) if s.node_id == "body"
        ]
        assert len(body_steps) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("inner_done_back_edge", [False, True])
async def test_nested_loop_runs_its_body_on_every_outer_pass(inner_done_back_edge):
    # The roadmap-sync shape: a loop over batches with a loop over writes
    # inside. The inner loop must start afresh on each outer pass, and finish
    # before the outer one advances. The canvas often draws the inner `Done`
    # back to the outer loop: that back-edge must not change the order.
    edges = [
        _edge("t1", "outer"),
        _edge("outer", "inner", sh="each"),
        _edge("inner", "write", sh="each"),
        _edge("outer", "after", sh="done"),
    ]
    if inner_done_back_edge:
        edges.append(_edge("inner", "outer", sh="done"))
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("outer", "util.loop", items='["a", "b"]'),
            _node("inner", "util.loop", items='["1", "2"]'),
            _node(
                "write", "util.set_variable",
                name="log", mode="append", value="{{outer.item}}{{inner.item}};",
            ),
            _node("after", "util.cast", value="{{write.value}}", to="text"),
        ],
        "edges": edges,
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        assert run.state["vars"]["log"] == "a1;a2;b1;b2;"
        steps = await _steps(session, run)
        assert [s.node_id for s in steps] == [
            "t1",
            "outer", "inner", "write", "inner", "write", "inner",
            "outer", "inner", "write", "inner", "write", "inner",
            "outer", "after",
        ]
        # The inner loop's journal restarts at pass 0 on each outer pass.
        assert [s.iteration for s in steps if s.node_id == "inner"] == [
            0, 1, 2, 0, 1, 2,
        ]
        assert [s.iteration for s in steps if s.node_id == "outer"] == [0, 1, 2]
        after = [s for s in steps if s.node_id == "after"]
        assert len(after) == 1
        assert after[0].input["value"] == "a1;a2;b1;b2;"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["merge_back_edge", "done_wired_back"])
async def test_a_finished_top_level_loop_reached_again_runs_once(shape):
    # Only a loop running inside another one restarts. A top-level loop
    # reached again once finished (its back-edge joined by a merge that waits
    # for the loop, or its Done wired back into it) keeps running once.
    nodes = [
        _node("t1", "trigger.manual"),
        _node("lp", "util.loop", items='["a", "b"]'),
        _node(
            "write", "util.set_variable",
            name="log", mode="append", value="{{lp.item}};",
        ),
        _node("after", "util.cast", value="fin", to="text"),
    ]
    edges = [
        _edge("t1", "lp"),
        _edge("lp", "write", sh="each"),
        _edge("lp", "after", sh="done"),
    ]
    if shape == "merge_back_edge":
        nodes += [
            _node("side", "util.cast", value="x", to="text"),
            _node("m", "util.merge", strategy="wait_all"),
        ]
        edges += [
            _edge("lp", "side", sh="each"),
            _edge("write", "m"),
            _edge("side", "m"),
            _edge("m", "lp"),
        ]
    else:
        edges.append(_edge("after", "lp"))
    graph = {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 1}}
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        assert run.state["vars"]["log"] == "a;b;"
        steps = await _steps(session, run)
        assert [s.iteration for s in steps if s.node_id == "lp"] == [0, 1, 2]
        assert len([s for s in steps if s.node_id == "after"]) == 1


@pytest.mark.asyncio
async def test_three_nested_loops_run_every_combination():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("l1", "util.loop", items='["a", "b"]'),
            _node("l2", "util.loop", items='["1", "2"]'),
            _node("l3", "util.loop", items='["x", "y"]'),
            _node(
                "write", "util.set_variable",
                name="log", mode="append", value="{{l1.item}}{{l2.item}}{{l3.item}};",
            ),
        ],
        "edges": [
            _edge("t1", "l1"),
            _edge("l1", "l2", sh="each"),
            _edge("l2", "l3", sh="each"),
            _edge("l3", "write", sh="each"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        assert run.state["vars"]["log"] == (
            "a1x;a1y;a2x;a2y;b1x;b1y;b2x;b2y;"
        )


@pytest.mark.asyncio
async def test_while_inside_a_loop_restarts_on_every_outer_pass():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("lp", "util.loop", items='["a", "b"]'),
            _node("init", "util.set_variable", name="n", mode="set", value="0"),
            _node("get", "util.get_variable", name="n", default="0"),
            _node(
                "w1", "util.while",
                left="{{get.value}}", operator="lt", right="2",
                max_iterations=25,
            ),
            _node("inc", "util.set_variable", name="n", mode="increment", value="1"),
            _node(
                "write", "util.set_variable",
                name="log", mode="append", value="{{lp.item}}{{inc.value}};",
            ),
        ],
        "edges": [
            _edge("t1", "lp"),
            _edge("lp", "init", sh="each"),
            _edge("init", "get"),
            _edge("get", "w1"),
            _edge("w1", "inc", sh="body"),
            _edge("inc", "write"),
            # Back-edge through the read, as the existing While test draws it.
            _edge("write", "get"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        assert run.state["vars"]["log"] == "a1;a2;b1;b2;"
        steps = await _steps(session, run)
        assert [s.iteration for s in steps if s.node_id == "w1"] == [
            0, 1, 2, 0, 1, 2,
        ]
        assert [s.iteration for s in steps if s.node_id == "lp"] == [0, 1, 2]


# ── Error routing ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unresolved_node_without_error_edge_fails_the_run():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("u1", "unresolved", source_type="MysteryComponent"),
        ],
        "edges": [_edge("t1", "u1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "failed"
        assert run.status == "failed"
        assert "u1" in (run.error or "")


@pytest.mark.asyncio
async def test_error_port_reroutes_instead_of_failing():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("u1", "unresolved", source_type="MysteryComponent"),
            _node("fallback", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "u1"),
            _edge("u1", "fallback", sh="error", kind="error"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", "the error edge absorbs the failure"
        steps = await _steps(session, run)
        u1 = next(s for s in steps if s.node_id == "u1")
        assert u1.status == "failed"
        assert "fallback" in [s.node_id for s in steps]


def _reviewed_flow(review):
    """An imported node awaiting review, with an error branch wired."""
    imported = _node("lookup", "util.reroute")
    if review is not None:
        imported["source_payload"] = {"format": "n8n", "review": review}
    return {
        "nodes": [
            _node("t1", "trigger.manual"),
            imported,
            _node("after", "util.reroute"),
            _node("fallback", "util.reroute"),
        ],
        "edges": [
            _edge("t1", "lookup"),
            _edge("lookup", "after"),
            _edge("lookup", "fallback", sh="error", kind="error"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


@pytest.mark.asyncio
async def test_a_node_awaiting_review_fails_the_run_past_its_error_edge():
    """An error branch handles the node's failures, not a node that may not run."""
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=_reviewed_flow(["Check the URL."]))
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "failed"
        assert run.status == "failed"
        steps = await _steps(session, run)
        assert [(s.node_id, s.status) for s in steps] == [
            ("t1", "completed"),
            ("lookup", "failed"),
        ]
        assert steps[1].error == (
            "This imported node has not been reviewed yet: Check the URL. "
            "Clear its review notes once it is fixed."
        )


@pytest.mark.asyncio
async def test_a_reviewed_node_runs():
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=_reviewed_flow([]))
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        assert [(s.node_id, s.status) for s in steps] == [
            ("t1", "completed"),
            ("lookup", "completed"),
            ("after", "completed"),
        ]


@pytest.mark.asyncio
async def test_a_trigger_awaiting_review_never_starts_the_run():
    """A trigger never goes through the node dispatch: checked at the start."""
    graph = _reviewed_flow([])
    graph["nodes"][0]["source_payload"] = {
        "format": "n8n",
        "review": ["n8n fired every 2 weeks; this schedule fires weekly."],
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "failed"
        assert run.error == (
            "This imported node has not been reviewed yet: n8n fired every 2 weeks; this schedule "
            "fires weekly. Clear its review notes once it is fixed."
        )
        steps = await _steps(session, run)
        assert [(s.node_id, s.status) for s in steps] == [("t1", "failed")]


# ── Integration node: an operation that ANSWERS with an error ────────


_NOT_FOUND = "Error: GET /indicators/1.2.3.4 - resource not found (404)."


async def _integration_flow(session, monkeypatch, handler, **node_config):
    from types import SimpleNamespace

    from app.models.integration import Integration
    from app.services.integration_tools import _SafeIntegrationExecutor

    async def _fake_build(db, bindings, context=None):
        executor = _SafeIntegrationExecutor()
        executor.register("lookup", handler)
        return (
            [SimpleNamespace(name="lookup", input_schema={"type": "object"})],
            executor,
        )

    monkeypatch.setattr(
        "app.services.integration_tools.build_integration_tools", _fake_build
    )
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("i1", "integration", operation="lookup", **node_config),
            _node("next", "condition.if", left="{{i1.result}}", operator="is_not_empty"),
            _node("fallback", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "i1"),
            _edge("i1", "next"),
            _edge("i1", "fallback", sh="error", kind="error"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    user, flow = await _seed(session, graph=graph)
    integration = Integration(user_id=user.id, provider="github", name="gh")
    session.add(integration)
    await session.commit()
    for node in flow.graph["nodes"]:
        if node["id"] == "i1":
            node["config"]["integration_id"] = str(integration.id)
    flow.graph = json.loads(json.dumps(flow.graph))
    await session.commit()
    return user, flow


async def _answers_not_found(**_kwargs):
    return _NOT_FOUND


@pytest.mark.asyncio
async def test_integration_answered_error_completes_and_passes_the_text_on(
    monkeypatch,
):
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _integration_flow(session, monkeypatch, _answers_not_found)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        i1 = next(s for s in steps if s.node_id == "i1")
        assert i1.status == "completed"
        assert i1.output == {"result": _NOT_FOUND}
        assert i1.warning == _NOT_FOUND
        walked = [s.node_id for s in steps]
        assert "next" in walked and "fallback" not in walked


@pytest.mark.asyncio
async def test_integration_opt_in_fails_the_step_and_takes_the_error_edge(
    monkeypatch,
):
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _integration_flow(
            session, monkeypatch, _answers_not_found, fail_on_error_result=True
        )
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", "the error edge absorbs the failure"
        steps = await _steps(session, run)
        i1 = next(s for s in steps if s.node_id == "i1")
        assert i1.status == "failed"
        assert i1.error == _NOT_FOUND
        walked = [s.node_id for s in steps]
        assert "fallback" in walked and "next" not in walked


@pytest.mark.asyncio
async def test_integration_raised_failure_still_fails_the_step(monkeypatch):
    async def _raises(**_kwargs):
        raise RuntimeError("upstream exploded")

    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _integration_flow(session, monkeypatch, _raises)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", "the error edge absorbs the failure"
        steps = await _steps(session, run)
        i1 = next(s for s in steps if s.node_id == "i1")
        assert i1.status == "failed"
        assert "upstream exploded" in (i1.error or "")
        assert "fallback" in [s.node_id for s in steps]


# ── Agent node contract ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_node_renders_prompt_and_respects_allow_handover(monkeypatch):
    calls = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        calls["prompt"] = user_message
        calls["allow_handover"] = kw.get("allow_handover")
        calls["persona_override"] = kw.get("persona_override")
        return "réponse de l'agent", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "ag",
                "agent",
                agent_id="PLACEHOLDER",
                prompt="Analyse {{t1.payload.title}}",
                persona_override="Tu es un analyste.",
                allow_handover=False,
            ),
        ],
        "edges": [_edge("t1", "ag")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        # The agents table is not in _TABLES; create it for this test only.
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Analyste")
            session.add(agent)
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

            run, outcome = await _run_to_outcome(
                session, user, flow, payload={"title": "CVE-2026-0001"}
            )
            assert outcome.kind == "completed"
            assert calls["prompt"] == "Analyse CVE-2026-0001"
            assert calls["allow_handover"] is False, "per-node toggle honoured"
            assert calls["persona_override"] == "Tu es un analyste."
            ag = next(
                s for s in await _steps(session, run) if s.node_id == "ag"
            )
            assert ag.output["output"] == "réponse de l'agent"


@pytest.mark.asyncio
async def test_agent_node_execution_overrides_reach_run_agent_chat(monkeypatch):
    seen = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        seen.update(
            model=kw.get("model_id_override"),
            temperature=kw.get("temperature_override"),
            reasoning=kw.get("reasoning_effort_override"),
            max_iter=kw.get("max_iterations_override"),
            max_tokens=kw.get("max_tokens_override"),
        )
        return "ok", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)
    from app.models.agent import Agent

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "ag",
                "agent",
                agent_id="X",
                prompt="fais",
                model="gpt-4o",
                temperature=0.2,
                reasoning_effort="high",
                max_iterations=7,
                max_tokens=1024,
            ),
        ],
        "edges": [_edge("t1", "ag")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
        )
        await session.commit()
        user, flow = await _seed(session, graph=graph)
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="X")
        session.add(agent)
        await session.commit()
        flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", run.error
        assert seen == {
            "model": "gpt-4o",
            "temperature": 0.2,
            "reasoning": "high",
            "max_iter": 7,
            "max_tokens": 1024,
        }


@pytest.mark.asyncio
async def test_agent_node_blank_overrides_pass_none(monkeypatch):
    seen = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        seen.update(
            model=kw.get("model_id_override"),
            temperature=kw.get("temperature_override"),
            max_tokens=kw.get("max_tokens_override"),
        )
        return "ok", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)
    from app.models.agent import Agent

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="X", prompt="fais"),
        ],
        "edges": [_edge("t1", "ag")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
        )
        await session.commit()
        user, flow = await _seed(session, graph=graph)
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="X")
        session.add(agent)
        await session.commit()
        flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", run.error
        # Nothing set → every override is None → agent's own values apply.
        assert seen == {"model": None, "temperature": None, "max_tokens": None}


@pytest.mark.asyncio
async def test_memory_and_guardrails_nodes_reach_run_agent_chat(monkeypatch):
    seen = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        seen.update(
            memory=kw.get("memory_overrides"),
            guardrails=kw.get("guardrails_override"),
        )
        return "ok", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)
    from app.models.agent import Agent

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="X", prompt="fais"),
            _node(
                "mem",
                "capability.memory",
                short_term=True,
                medium_term=False,
                persistent=True,
                user=False,
            ),
            _node("gr", "capability.guardrails", guardrails="Ne jamais divulguer de secret."),
        ],
        "edges": [
            _edge("t1", "ag"),
            _edge("mem", "ag", sh="provides", th="memory", kind="capability"),
            _edge("gr", "ag", sh="provides", th="guardrails", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
        )
        await session.commit()
        user, flow = await _seed(session, graph=graph)
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="X")
        session.add(agent)
        await session.commit()
        flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", run.error
        assert seen["memory"] == {
            "short_term": True,
            "medium_term": False,
            "persistent": True,
            "user": False,
        }
        assert seen["guardrails"] == "Ne jamais divulguer de secret."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_format", "wired_schema", "answer", "fields"),
    [
        # The agent's own JSON output format.
        ("json", False, '{"severity": "critical", "count": 3}', {"severity": "critical", "count": 3}),
        # A wired Structured output node turns a markdown agent into a JSON one.
        ("markdown", True, '{"severity": "critical"}', {"severity": "critical"}),
        # A markdown agent's answer is text, even when it reads as JSON.
        ("markdown", False, '{"severity": "critical"}', None),
        # A JSON answer that is not an object has no fields.
        ("json", False, '["critical"]', None),
        ("json", False, "not json", None),
    ],
)
async def test_agent_node_publishes_the_fields_of_a_json_answer(
    monkeypatch, output_format, wired_schema, answer, fields
):
    """`{{node.fields.KEY}}` reads a JSON answer, as on a Script node."""

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        return answer, None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)
    from app.models.agent import Agent

    nodes = [
        _node("t1", "trigger.manual"),
        _node("ag", "agent", agent_id="X", prompt="classe"),
        _node(
            "c1",
            "condition.if",
            left="{{ag.fields.severity}}",
            operator="equals",
            right="critical",
        ),
    ]
    edges = [_edge("t1", "ag"), _edge("ag", "c1")]
    if wired_schema:
        nodes.append(
            _node(
                "so",
                "capability.structured_output",
                schema={"type": "object", "properties": {"severity": {"type": "string"}}},
            )
        )
        edges.append(_edge("so", "ag", sh="provides", th="output", kind="capability"))
    graph = {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 1}}
    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
        )
        await session.commit()
        user, flow = await _seed(session, graph=graph)
        agent = Agent(
            id=uuid.uuid4(), user_id=user.id, name="X", output_format=output_format
        )
        session.add(agent)
        await session.commit()
        flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", run.error
        steps = await _steps(session, run)
        ag = next(s for s in steps if s.node_id == "ag")
        assert ag.output["output"] == answer, "the text stays as it came"
        assert ag.output.get("fields") == fields
        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.output == {"result": fields is not None}


@pytest.mark.asyncio
async def test_no_memory_or_guardrails_nodes_pass_none(monkeypatch):
    seen = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        seen.update(
            memory=kw.get("memory_overrides"),
            guardrails=kw.get("guardrails_override"),
        )
        return "ok", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)
    from app.models.agent import Agent

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="X", prompt="fais"),
        ],
        "edges": [_edge("t1", "ag")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
        )
        await session.commit()
        user, flow = await _seed(session, graph=graph)
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="X")
        session.add(agent)
        await session.commit()
        flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", run.error
        assert seen == {"memory": None, "guardrails": None}


@pytest.mark.asyncio
async def test_one_tool_node_declares_several_tools(monkeypatch):
    """A single capability.tool node with a list contributes every id — no
    need for one node per tool."""
    seen = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        seen.update(
            builtins=kw.get("session_builtin_tool_ids"),
            customs=kw.get("session_custom_tool_ids"),
        )
        return "ok", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)
    from app.models.agent import Agent

    custom_a, custom_b = str(uuid.uuid4()), str(uuid.uuid4())
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="X", prompt="fais"),
            _node(
                "cap",
                "capability.tool",
                tool_ids=["builtin:web_fetch", "builtin:browser", custom_a, custom_b],
            ),
        ],
        "edges": [
            _edge("t1", "ag"),
            _edge("cap", "ag", sh="provides", th="tools", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
        )
        await session.commit()
        user, flow = await _seed(session, graph=graph)
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="X")
        session.add(agent)
        await session.commit()
        flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", run.error
        # builtins routed by prefix (stripped); customs kept as UUIDs.
        assert seen["builtins"] == ["web_fetch", "browser"]
        assert seen["customs"] == [custom_a, custom_b]


# ── Park / resume (delay) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delay_parks_then_resumes_where_it_stopped():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("d1", "util.delay", duration="15m"),
            _node("after", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [_edge("t1", "d1"), _edge("d1", "after")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)

        assert outcome.kind == "park"
        assert outcome.wake_at is not None
        assert run.status == "awaiting"
        assert run.state["waiting"]["kind"] == "delay"
        walked = [s.node_id for s in await _steps(session, run)]
        assert "after" not in walked, "downstream must wait for the delay"

        # Simulate the wake: rewind the stored instant, run a new burst.
        state = dict(run.state)
        state["waiting"] = {
            **state["waiting"],
            "wake_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        run.state = state
        await session.commit()

        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "completed"
        assert run.status == "completed"
        assert "after" in [s.node_id for s in await _steps(session, run)]


@pytest.mark.asyncio
async def test_delay_woken_early_parks_again_for_the_remainder():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("d1", "util.delay", duration="2h"),
        ],
        "edges": [_edge("t1", "d1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "park"

        # A premature wake (deploy, lease reclaim) must re-park, not skip
        # the remaining sleep.
        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "park"
        assert run.status == "awaiting"


# ── Sub-flow (flow.call) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flow_call_runs_the_child_and_passes_the_result_back():
    child_graph = {
        "nodes": [
            _node("ct", "trigger.manual"),
            _node(
                "cc",
                "condition.if",
                left="{{trigger.input}}",
                operator="is_not_empty",
            ),
        ],
        "edges": [_edge("ct", "cc")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, child_flow = await _seed(
            session, graph=child_graph, name="Child"
        )
        parent_graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "call",
                    "flow.call",
                    flow_id=str(child_flow.id),
                    input="depuis le parent",
                ),
            ],
            "edges": [_edge("t1", "call")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        parent_flow = Flow(
            id=uuid.uuid4(), user_id=user.id, name="Parent", graph=parent_graph
        )
        session.add(parent_flow)
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, parent_flow)
        assert outcome.kind == "park"
        assert run.state["waiting"]["kind"] == "flow_call"

        child_run = (
            await session.execute(
                select(FlowRun).where(FlowRun.parent_run_id == run.id)
            )
        ).scalar_one()
        assert child_run.depth == 1
        assert child_run.trigger_type == "flow_call"

        # Run the child to completion, then resume the parent.
        child_outcome = await FlowWalker(session, child_run, user).run_burst()
        await session.commit()
        assert child_outcome.kind == "completed"

        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "completed"
        call_step = next(
            s
            for s in await _steps(session, run)
            if s.node_id == "call" and s.status == "completed"
        )
        assert "output" in (call_step.output or {})


@pytest.mark.asyncio
async def test_handover_edge_labels_constrain_allowed_types(monkeypatch):
    """The labels on a node's handover edges reach run_agent_chat:
    labelled edges narrow the allowed types; unlabelled edges leave chat
    behaviour unconstrained (None)."""
    seen: dict = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        seen["allowed"] = kw.get("allowed_handover_types")
        return "ok", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    def graph_with(label):
        edge_h = _edge("ag", "sink", sh="handover", kind="handover")
        if label:
            edge_h["label"] = label
        return {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node("ag", "agent", agent_id="PLACEHOLDER", prompt="go"),
                _node("sink", "util.cast", value="x", to="text"),
            ],
            "edges": [_edge("t1", "ag"), edge_h],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }

    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph_with("transfer"))
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="A")
            session.add(agent)
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)
            await _run_to_outcome(session, user, flow)
            assert seen["allowed"] == frozenset({"transfer"})

            flow2 = Flow(
                id=uuid.uuid4(), user_id=user.id, name="F2",
                graph=graph_with(None),
            )
            flow2.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)
            session.add(flow2)
            await session.commit()
            await _run_to_outcome(session, user, flow2)
            assert seen["allowed"] is None


def test_filter_handover_agents_by_type():
    from types import SimpleNamespace

    from app.services.agent_chat import filter_handover_agents_by_type

    a = SimpleNamespace(id="a")
    b = SimpleNamespace(id="b")
    type_map = {"a": "transfer"}  # b defaults to delegate

    assert filter_handover_agents_by_type([a, b], type_map, None) == [a, b]
    assert filter_handover_agents_by_type(
        [a, b], type_map, frozenset({"transfer"})
    ) == [a]
    assert filter_handover_agents_by_type(
        [a, b], type_map, frozenset({"delegate"})
    ) == [b]
    # A type no tool implements runs as delegate.
    assert filter_handover_agents_by_type(
        [a, b], {"a": "custom", "b": "transfer"}, frozenset({"delegate"})
    ) == [a]
    # A peer of a legacy map runs every type, a transfer included.
    assert filter_handover_agents_by_type(
        [a, b], {}, frozenset({"transfer"})
    ) == [a, b]
    assert filter_handover_agents_by_type(
        [a, b], {}, frozenset({"mandatory_transfer"})
    ) == []


@pytest.mark.asyncio
async def test_on_delegate_attachment_emits_one_event_per_delegation(monkeypatch):
    """The "On delegate" capability: each background task the agent
    spawns emits the attached declared event — rendered with the
    {{delegation.*}} pseudo-node — and listener flows start as their own
    runs while `out` continues immediately (no park)."""

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        sink = kw.get("delegations_out")
        if sink is not None:
            sink.append(
                {
                    "task_id": "11111111-1111-1111-1111-111111111111",
                    "target_agent_id": "22222222-2222-2222-2222-222222222222",
                    "target_agent_name": "Analyste",
                    "task": "analyser les IOCs",
                }
            )
            sink.append(
                {
                    "task_id": "33333333-3333-3333-3333-333333333333",
                    "target_agent_id": "22222222-2222-2222-2222-222222222222",
                    "target_agent_name": "Analyste",
                    "task": "rédiger le résumé",
                }
            )
        return "travail délégué", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(
                session,
                graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
            )
            et = await _declare_event_type(
                session, user, name="TâcheDéléguée",
                fields=[
                    {"name": "task", "type": "string", "required": True},
                    {"name": "cible", "type": "string", "required": False},
                ],
            )
            listener = Flow(
                id=uuid.uuid4(),
                user_id=user.id,
                name="Delegation handler",
                status="active",
                graph={
                    "nodes": [
                        _node("t1", "trigger.event", event_type_id=str(et.id)),
                        _node("s1", "util.stop"),
                    ],
                    "edges": [_edge("t1", "s1")],
                    "viewport": {"x": 0, "y": 0, "zoom": 1},
                },
            )
            session.add(listener)
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Chef")
            session.add(agent)
            await session.commit()

            cap_edge = _edge("cap", "ag", sh="provides", th="on_delegate")
            cap_edge["kind"] = "capability"
            flow.graph = {
                "nodes": [
                    _node("t1", "trigger.manual"),
                    _node("ag", "agent", agent_id=str(agent.id), prompt="fais le travail"),
                    _node(
                        "cap", "capability.on_delegate",
                        event_type_id=str(et.id), scope="global",
                        mappings={
                            "task": "{{delegation.task}}",
                            "cible": "{{delegation.target_agent_name}}",
                        },
                    ),
                    _node("fin", "util.cast", value="suite", to="text"),
                ],
                "edges": [
                    _edge("t1", "ag"),
                    cap_edge,
                    _edge("ag", "fin"),
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }
            await session.commit()

            run, outcome = await _run_to_outcome(session, user, flow)
            # No park: `out` continued immediately, the signal is the event.
            assert outcome.kind == "completed"

            ag = next(s_ for s_ in await _steps(session, run) if s_.node_id == "ag")
            assert ag.output["delegation_events"] == 2
            assert [d["task"] for d in ag.output["delegations"]] == [
                "analyser les IOCs", "rédiger le résumé",
            ]

            # Two listener runs, typed payload rendered from {{delegation.*}}.
            children = (
                (
                    await session.execute(
                        select(FlowRun).where(FlowRun.flow_id == listener.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(children) == 2
            assert {
                c.trigger_payload["fields"]["task"] for c in children
            } == {"analyser les IOCs", "rédiger le résumé"}
            assert all(
                c.trigger_payload["fields"]["cible"] == "Analyste" for c in children
            )


@pytest.mark.asyncio
async def test_script_node_runs_a_custom_tool_through_its_own_runtime(monkeypatch):
    """`action.script` binds a platform CustomTool by id and rides
    the SAME runtime agents use (load_custom_tools -> execute_tool_auto):
    arguments coerce against the tool's live schema, a JSON stdout publishes
    {{node.fields.*}}, and a disabled tool is a node error, not a crash."""

    async def fake_execute_tool_auto(function_code, inputs, language="javascript",
                                     timeout=300, **kw):
        # The sandbox is faked; the assertion is on what REACHES it
        # ($files is the loader's own injection — not under test).
        assert {k: v for k, v in inputs.items() if k != "$files"} == {
            "count": 3, "label": "IOC",
        }
        assert language == "python"
        return '{"total": 3, "echo": "IOC"}'

    import app.services.custom_tool_executor as executor

    monkeypatch.setattr(executor, "execute_tool_auto", fake_execute_tool_auto)

    from app.models.agent import Agent
    from app.models.chat import Conversation
    from app.models.custom_tool import CustomTool

    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        Agent.__table__,
                        Conversation.__table__,
                        CustomTool.__table__,
                    ],
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(
                session,
                graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
            )
            tool = CustomTool(
                id=uuid.uuid4(),
                user_id=user.id,
                name="compteur",
                language="python",
                js_function="print('jamais exécuté: sandbox mocké')",
                input_schema={
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                        "label": {"type": "string"},
                    },
                    "required": ["count"],
                },
            )
            session.add(tool)
            await session.commit()

            flow.graph = {
                "nodes": [
                    _node("t1", "trigger.manual"),
                    _node(
                        "sc", "action.script",
                        custom_tool_id=str(tool.id),
                        arguments={"count": "3", "label": "IOC"},
                    ),
                    _node(
                        "fin", "util.cast",
                        value="total={{sc.fields.total}} echo={{sc.fields.echo}}",
                        to="text",
                    ),
                ],
                "edges": [_edge("t1", "sc"), _edge("sc", "fin")],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }
            await session.commit()

            run, outcome = await _run_to_outcome(session, user, flow)
            sc = next(s_ for s_ in await _steps(session, run) if s_.node_id == "sc")
            assert outcome.kind == "completed", f"step sc: {sc.error}"
            assert sc.output["fields"] == {"total": 3, "echo": "IOC"}
            fin = next(
                s_ for s_ in await _steps(session, run) if s_.node_id == "fin"
            )
            assert fin.input["value"] == "total=3 echo=IOC"

            # A disabled tool routes the error pin instead of crashing.
            tool.enabled = False
            await session.commit()
            run2, outcome2 = await _run_to_outcome(session, user, flow)
            assert outcome2.kind == "failed"
            sc2 = next(
                s_ for s_ in await _steps(session, run2) if s_.node_id == "sc"
            )
            assert "disabled" in (sc2.error or "")


@pytest.mark.asyncio
async def test_call_tool_resolves_flow_builtins_and_coerces_arguments():
    """The Call-tool node reaches the flow-callable built-ins: the
    STIX validator runs offline, and the stored arguments_schema coerces the
    form's string values back to what the tool declares."""
    bundle = (
        '{"type": "bundle", "id": "bundle--11111111-1111-4111-8111-111111111111",'
        ' "objects": []}'
    )
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "tool", "action.call_tool",
                tool_name="stix_bundle_validator",
                arguments={"bundle_json": bundle},
                arguments_schema={
                    "type": "object",
                    "properties": {"bundle_json": {"type": "string"}},
                    "required": ["bundle_json"],
                },
            ),
        ],
        "edges": [_edge("t1", "tool")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        step = next(s_ for s_ in await _steps(session, run) if s_.node_id == "tool")
        assert step.status == "completed"
        assert step.output["result"], "the validator returned a verdict"


@pytest.mark.asyncio
async def test_unified_handover_occurrences_route_by_edge_label(monkeypatch):
    """The unified contract: every handover occurrence fires the
    branch, edge labels route by type (unlabelled = every type), the
    occurrences are published as {{ag.handover}} / {{ag.handovers}}, and a
    type with no occurrence keeps its branch dark."""

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        sink = kw.get("handovers_out")
        if sink is not None:
            sink.append(
                {
                    "type": "consult",
                    "target_agent_id": "11111111-1111-1111-1111-111111111111",
                    "target_agent_name": "Analyste",
                    "question": "avis ?",
                    "answer": "favorable",
                }
            )
        dsink = kw.get("delegations_out")
        if dsink is not None:
            dsink.append(
                {
                    "task_id": "22222222-2222-2222-2222-222222222222",
                    "target_agent_id": "33333333-3333-3333-3333-333333333333",
                    "target_agent_name": "Ouvrier",
                    "task": "trier les logs",
                }
            )
        return "réponse finale", None  # no transfer

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    def h_edge(target, label):
        edge = _edge("ag", target, sh="handover", kind="handover")
        edge["id"] = f"e_h_{target}"
        if label:
            edge["label"] = label
        return edge

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="PLACEHOLDER", prompt="go"),
            _node(
                "on_consult", "util.cast",
                value="consult de {{ag.handover.target_agent_name}}: {{ag.handover.answer}}",
                to="text",
            ),
            _node("on_delegate", "util.cast", value="délégué", to="text"),
            _node("on_transfer", "util.cast", value="transféré", to="text"),
            _node("any_handover", "util.cast", value="au moins un", to="text"),
            _node("after", "util.cast", value="out", to="text"),
        ],
        "edges": [
            _edge("t1", "ag"),
            h_edge("on_consult", "consult"),
            h_edge("on_delegate", "delegate"),
            h_edge("on_transfer", "transfer"),
            h_edge("any_handover", None),
            _edge("ag", "after"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Chef")
            session.add(agent)
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

            run, outcome = await _run_to_outcome(session, user, flow)
            assert outcome.kind == "completed"
            ran = {s_.node_id for s_ in await _steps(session, run)}
            # consult + delegate occurred → their branches fired, plus the
            # wildcard; NO transfer occurred → its branch stayed dark.
            assert "on_consult" in ran
            assert "on_delegate" in ran
            assert "any_handover" in ran
            assert "on_transfer" not in ran
            assert "after" in ran

            ag = next(s_ for s_ in await _steps(session, run) if s_.node_id == "ag")
            assert len(ag.output["handovers"]) == 2
            consult = next(
                s_ for s_ in await _steps(session, run)
                if s_.node_id == "on_consult"
            )
            # {{ag.handover}} is the LAST occurrence (the delegate) — the
            # consult branch reads the list; here we just prove sub-paths
            # resolve on the published occurrence.
            assert "de Ouvrier" in consult.input["value"] or "favorable" in consult.input["value"]


@pytest.mark.asyncio
async def test_handover_last_occurrence_is_chronological_and_mandatory_routes(monkeypatch):
    """delegate-then-transfer: {{ag.handover}} is the TRANSFER (chronology
    preserved by the chat layer's tee), and a mandatory transfer routes the
    `mandatory_transfer` label while a plain `transfer` label also fires."""

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        sink = kw.get("handovers_out")
        if sink is not None:
            # Chronological order, as the real tee produces it.
            sink.append(
                {
                    "type": "delegate",
                    "target_agent_id": "33333333-3333-3333-3333-333333333333",
                    "target_agent_name": "Ouvrier",
                    "task": "trier",
                    "task_id": "22222222-2222-2222-2222-222222222222",
                }
            )
            sink.append(
                {
                    "type": "transfer",
                    "target_agent_id": "44444444-4444-4444-4444-444444444444",
                    "target_agent_name": "Chef",
                    "reason": "policy",
                    "mandatory": True,
                }
            )
        return "réponse", "44444444-4444-4444-4444-444444444444"

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    def h_edge(target, label):
        edge = _edge("ag", target, sh="handover", kind="handover")
        edge["id"] = f"e_h_{target}"
        if label:
            edge["label"] = label
        return edge

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="PLACEHOLDER", prompt="go"),
            _node(
                "last", "util.cast",
                value="last={{ag.handover.type}} vers {{ag.handover.target_agent_name}}",
                to="text",
            ),
            _node("on_mandatory", "util.cast", value="mandatory", to="text"),
            _node("on_transfer", "util.cast", value="transfer", to="text"),
        ],
        "edges": [
            _edge("t1", "ag"),
            _edge("ag", "last"),
            h_edge("on_mandatory", "mandatory_transfer"),
            h_edge("on_transfer", "transfer"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="A")
            session.add(agent)
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

            run, outcome = await _run_to_outcome(session, user, flow)
            assert outcome.kind == "completed"
            ran = {s_.node_id for s_ in await _steps(session, run)}
            assert "on_mandatory" in ran, "mandatory label routes the flag"
            assert "on_transfer" in ran, "a mandatory transfer IS a transfer"
            last = next(
                s_ for s_ in await _steps(session, run) if s_.node_id == "last"
            )
            assert last.input["value"] == "last=transfer vers Chef"


@pytest.mark.asyncio
async def test_mandatory_label_stays_dark_on_plain_transfer(monkeypatch):
    """A plain (LLM-decided) transfer must NOT route a `mandatory_transfer`
    labelled edge — the two are distinct routes now."""

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        sink = kw.get("handovers_out")
        if sink is not None:
            sink.append(
                {
                    "type": "transfer",
                    "target_agent_id": "44444444-4444-4444-4444-444444444444",
                    "target_agent_name": "Chef",
                    "reason": "choix",
                    "mandatory": False,
                }
            )
        return "réponse", "44444444-4444-4444-4444-444444444444"

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    edge_m = _edge("ag", "on_mandatory", sh="handover", kind="handover")
    edge_m["label"] = "mandatory_transfer"
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="PLACEHOLDER", prompt="go"),
            _node("on_mandatory", "util.cast", value="mandatory", to="text"),
        ],
        "edges": [_edge("t1", "ag"), edge_m],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="A")
            session.add(agent)
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

            run, outcome = await _run_to_outcome(session, user, flow)
            assert outcome.kind == "completed"
            ran = {s_.node_id for s_ in await _steps(session, run)}
            assert "on_mandatory" not in ran


@pytest.mark.asyncio
async def test_synthesized_transfer_occurrence_routes_labelled_edges(monkeypatch):
    """Compat: a chat layer that only returns handover_agent_id still yields
    a well-shaped transfer occurrence that routes `transfer` labels."""

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        return "réponse", "55555555-5555-5555-5555-555555555555"

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    edge_t = _edge("ag", "on_transfer", sh="handover", kind="handover")
    edge_t["label"] = "transfer"
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="PLACEHOLDER", prompt="go"),
            _node(
                "on_transfer", "util.cast",
                value="{{ag.handover.type}}:{{ag.handover.target_agent_id}}",
                to="text",
            ),
        ],
        "edges": [_edge("t1", "ag"), edge_t],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="A")
            session.add(agent)
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

            run, outcome = await _run_to_outcome(session, user, flow)
            assert outcome.kind == "completed"
            step = next(
                s_ for s_ in await _steps(session, run)
                if s_.node_id == "on_transfer"
            )
            assert step.input["value"] == (
                "transfer:55555555-5555-5555-5555-555555555555"
            )


@pytest.mark.asyncio
async def test_agent_handover_to_flow_fires_out_only_after_the_return(monkeypatch):
    """The handover port routed into a flow is a SYNCHRONOUS cycle.

    ag.handover → flow.call, ag.out → continuation: the out branch must NOT
    run in the launch burst (that would be an async fork at handover time) —
    it runs only once the called flow returned and the parent resumed. The
    contract is the walker's FIFO queue + the ["handover", "out"] handle
    order + flow.call parking the whole run.
    """

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        return "je transfère l'analyse", str(uuid.uuid4())

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    child_graph = {
        "nodes": [
            _node("ct", "trigger.manual"),
            _node("cc", "util.cast", value="analysé: {{trigger.input}}", to="text"),
        ],
        "edges": [_edge("ct", "cc")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, child_flow = await _seed(session, graph=child_graph, name="Child")
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Préparateur")
            session.add(agent)
            parent_graph = {
                "nodes": [
                    _node("t1", "trigger.manual"),
                    _node(
                        "ag", "agent", agent_id=str(agent.id), prompt="prépare",
                        handover_mode="wait",
                    ),
                    _node("call", "flow.call", flow_id=str(child_flow.id), input="go"),
                    _node("fin", "util.cast", value="après le out", to="text"),
                ],
                "edges": [
                    _edge("t1", "ag"),
                    _edge("ag", "call", sh="handover", kind="handover"),
                    _edge("ag", "fin", sh="out"),
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }
            parent_flow = Flow(
                id=uuid.uuid4(), user_id=user.id, name="Parent", graph=parent_graph
            )
            session.add(parent_flow)
            await session.commit()

            run, outcome = await _run_to_outcome(session, user, parent_flow)

            # Launch burst: parked on the called flow — and the out branch
            # has NOT run. This is the whole point of the test.
            assert outcome.kind == "park"
            assert run.state["waiting"]["kind"] == "flow_call"
            assert "fin" not in {s_.node_id for s_ in await _steps(session, run)}

            child_run = (
                await session.execute(
                    select(FlowRun).where(FlowRun.parent_run_id == run.id)
                )
            ).scalar_one()
            child_outcome = await FlowWalker(session, child_run, user).run_burst()
            await session.commit()
            assert child_outcome.kind == "completed"

            outcome2 = await FlowWalker(session, run, user).run_burst()
            await session.commit()
            assert outcome2.kind == "completed"

            steps = await _steps(session, run)
            fin = next(s_ for s_ in steps if s_.node_id == "fin")
            call = next(
                s_ for s_ in steps if s_.node_id == "call" and s_.status == "completed"
            )
            # The continuation started only after the handover cycle returned.
            assert fin.started_at >= call.completed_at


@pytest.mark.asyncio
async def test_handover_flow_call_is_detached_by_default(monkeypatch):
    """The transfer spirit: a flow.call fed by an agent's handover
    edge follows the agent's handover_mode, and the DEFAULT is detached —
    the relay runs as its own life, `out` fires immediately, no park.
    A plain (non-handover) call still always waits."""

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        return "je transfère", str(uuid.uuid4())

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    child_graph = {
        "nodes": [
            _node("ct", "trigger.manual"),
            _node("cc", "util.cast", value="relais", to="text"),
        ],
        "edges": [_edge("ct", "cc")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, child_flow = await _seed(session, graph=child_graph, name="Relais")
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Source")
            session.add(agent)
            parent_flow = Flow(
                id=uuid.uuid4(), user_id=user.id, name="Parent",
                graph={
                    "nodes": [
                        _node("t1", "trigger.manual"),
                        # No handover_mode set: detached is the default.
                        _node("ag", "agent", agent_id=str(agent.id), prompt="go"),
                        _node(
                            "call", "flow.call",
                            flow_id=str(child_flow.id), input="relais",
                        ),
                        _node("fin", "util.cast", value="suite", to="text"),
                    ],
                    "edges": [
                        _edge("t1", "ag"),
                        _edge("ag", "call", sh="handover", kind="handover"),
                        _edge("ag", "fin"),
                    ],
                    "viewport": {"x": 0, "y": 0, "zoom": 1},
                },
            )
            session.add(parent_flow)
            await session.commit()

            run, outcome = await _run_to_outcome(session, user, parent_flow)
            # No park: the relay was left to its own life.
            assert outcome.kind == "completed"
            steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
            assert steps["call"].output["detached"] is True
            assert "output" not in steps["call"].output
            assert steps["fin"].status == "completed"

            child_run = (
                await session.execute(
                    select(FlowRun).where(FlowRun.parent_run_id == run.id)
                )
            ).scalar_one()
            assert str(child_run.id) == steps["call"].output["child_run_id"]
            assert child_run.status == "pending"  # the reconciler's job now


def test_detached_flow_call_output_reference_warns():
    """{{call.output}} downstream of a handover-fed call warns (detached by
    default); the agent opting into Wait silences it, and a plain call never
    warns."""
    import uuid as _uuid

    from app.services.flow_graph import validate_graph as _vg

    def graph_with(agent_config):
        return {
            "nodes": [
                {"id": "t1", "kind": "trigger.manual",
                 "position": {"x": 0, "y": 0}, "config": {}},
                {"id": "ag", "kind": "agent", "position": {"x": 150, "y": 0},
                 "config": {"agent_id": str(_uuid.uuid4()), "prompt": "go",
                            **agent_config}},
                {"id": "call", "kind": "flow.call", "position": {"x": 300, "y": 0},
                 "config": {"flow_id": str(_uuid.uuid4())}},
                {"id": "fin", "kind": "util.cast", "position": {"x": 450, "y": 0},
                 "config": {"value": "{{call.output}}", "to": "text"}},
            ],
            "edges": [
                {"id": "e1", "source": "t1", "source_handle": "out",
                 "target": "ag", "target_handle": "in", "kind": "data"},
                {"id": "e2", "source": "ag", "source_handle": "handover",
                 "target": "call", "target_handle": "in", "kind": "handover",
                 "label": "transfer"},
                {"id": "e3", "source": "call", "source_handle": "out",
                 "target": "fin", "target_handle": "in", "kind": "data"},
            ],
        }

    result = _vg(graph_with({}))
    assert result.is_valid
    warn = next(i for i in result.issues if i.code == "ref.detached_output")
    assert warn.severity == "warning" and warn.node_id == "fin"

    # The agent opting into Wait restores the synchronous cycle: no warning.
    assert not any(
        i.code == "ref.detached_output"
        for i in _vg(graph_with({"handover_mode": "wait"})).issues
    )


@pytest.mark.asyncio
async def test_flow_call_depth_limit_fails_the_node():
    async with _temp_database() as sessions, sessions() as session:
        graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node("call", "flow.call", flow_id=str(uuid.uuid4())),
            ],
            "edges": [_edge("t1", "call")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        user, flow = await _seed(session, graph=graph)
        run = await create_flow_run(
            session,
            flow,
            trigger_type="flow_call",
            trigger_payload={},
            trigger_context={},
            user_id=user.id,
            depth=flow_runner.FLOW_CALL_MAX_DEPTH,
        )
        await session.commit()
        outcome = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome.kind == "failed"
        assert "depth" in (run.error or "").lower()


# ── Cancellation ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_run_stops_cooperatively():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("c1", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [_edge("t1", "c1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run = await create_flow_run(
            session,
            flow,
            trigger_type="manual",
            trigger_payload={},
            trigger_context={},
            user_id=user.id,
        )
        run.status = "cancelled"
        await session.commit()
        outcome = await FlowWalker(session, run, user).run_burst()
        assert outcome.kind == "cancelled"


# ── Adapter contract ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_completes_a_trivial_run_and_guards_terminal_reclaims():
    graph = {
        "nodes": [_node("t1", "trigger.manual")],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run = await create_flow_run(
            session,
            flow,
            trigger_type="manual",
            trigger_payload={},
            trigger_context={},
            user_id=user.id,
        )
        await session.commit()
        execution = (
            await session.execute(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.dedup_key == f"flow_run:{run.id}"
                )
            )
        ).scalar_one()

        adapter = FlowAdapter()
        ctx = AdapterContext(db=session, worker_id="test-worker")
        result = await adapter.execute(ctx, execution)
        await session.commit()
        assert result.kind == "completed"
        assert run.status == "completed"

        # A re-claim after completion must not re-run the graph.
        before = len(await _steps(session, run))
        result2 = await adapter.execute(ctx, execution)
        assert result2.kind == "completed"
        assert len(await _steps(session, run)) == before


@pytest.mark.asyncio
async def test_adapter_park_emits_a_scheduled_self_resume():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("d1", "util.delay", duration="30m"),
        ],
        "edges": [_edge("t1", "d1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run = await create_flow_run(
            session,
            flow,
            trigger_type="manual",
            trigger_payload={},
            trigger_context={},
            user_id=user.id,
        )
        await session.commit()
        execution = (
            await session.execute(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.dedup_key == f"flow_run:{run.id}"
                )
            )
        ).scalar_one()

        adapter = FlowAdapter()
        with contextlib.suppress(Exception):
            # notify/wake may touch Redis; the DB effects are what we assert.
            result = await adapter.execute(
                AdapterContext(db=session, worker_id="test-worker"), execution
            )
            await session.commit()
            assert result.kind == "await"
            assert result.deadline_at is not None

        events = (
            (
                await session.execute(
                    select(OrchestrationOutboxEvent).where(
                        OrchestrationOutboxEvent.execution_id == execution.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "resume"
        assert event.available_at is not None
        assert event.available_at > datetime.now(UTC) + timedelta(minutes=25)


# ── Capability wiring ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capability_nodes_extend_the_agent_and_are_shareable(monkeypatch):
    """One tool/skill/KB/integration node set, wired into TWO agent nodes.

    Asserts the walker's contract with run_agent_chat: wired ids arrive as
    the UNION with the agent's own (session_* kwargs REPLACE, so the walker
    merges), both agents receive the same shared capability, and capability
    nodes never appear in the step journal.
    """
    calls: list[dict] = []

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        calls.append(
            {
                "tools": kw.get("session_custom_tool_ids"),
                "builtins": kw.get("session_builtin_tool_ids"),
                "skills": kw.get("session_skill_ids"),
                "kbs": kw.get("session_knowledge_base_ids"),
                "integrations": kw.get("session_integration_ids"),
                "output_schema": kw.get("session_output_schema"),
            }
        )
        return f"ok {len(calls)}", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    tool_id, skill_id, kb_id, integ_id = (str(uuid.uuid4()) for _ in range(4))
    own_tool_id = str(uuid.uuid4())

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag1", "agent", agent_id="A1", prompt="un"),
            _node("ag2", "agent", agent_id="A2", prompt="deux"),
            _node("cap_tool", "capability.tool", tool_ids=[tool_id]),
            _node("cap_bi", "capability.tool", tool_ids=["builtin:web_fetch"]),
            _node("cap_out", "capability.structured_output",
                  schema={"type": "object", "properties": {"verdict": {"type": "string"}}}),
            _node("cap_skill", "capability.skill", skill_ids=[skill_id]),
            _node("cap_kb", "capability.knowledge_base", knowledge_base_ids=[kb_id]),
            _node(
                "cap_int", "capability.integration", integration_ids=[integ_id]
            ),
        ],
        "edges": [
            _edge("t1", "ag1"),
            _edge("ag1", "ag2"),
            # The SAME tool node feeds both agents — the sharing model.
            _edge("cap_tool", "ag1", sh="provides", th="tools", kind="capability"),
            _edge("cap_tool", "ag2", sh="provides", th="tools", kind="capability"),
            _edge("cap_bi", "ag1", sh="provides", th="tools", kind="capability"),
            _edge("cap_out", "ag1", sh="provides", th="output", kind="capability"),
            _edge("cap_skill", "ag1", sh="provides", th="skills", kind="capability"),
            _edge("cap_kb", "ag1", sh="provides", th="knowledge_bases", kind="capability"),
            _edge("cap_int", "ag1", sh="provides", th="integrations", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }

    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
        )
        await session.commit()

        user, flow = await _seed(session, graph=graph)
        # ag1 already owns a tool: the wired one must be ADDED, not replace it.
        agent1 = Agent(
            id=uuid.uuid4(), user_id=user.id, name="A1",
            custom_tool_ids=[own_tool_id],
            builtin_tool_ids=["work_items"],
        )
        agent2 = Agent(id=uuid.uuid4(), user_id=user.id, name="A2")
        session.add(agent1)
        session.add(agent2)
        await session.commit()
        flow.graph["nodes"][1]["config"]["agent_id"] = str(agent1.id)
        flow.graph["nodes"][2]["config"]["agent_id"] = str(agent2.id)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", run.error

        assert len(calls) == 2
        first, second = calls
        # Union: the agent's own tool survives, the wired one is added.
        assert first["tools"] == [own_tool_id, tool_id]
        # Built-in wired via `builtin:` prefix: routed to ITS list, prefix
        # stripped, union with the agent's own built-ins.
        assert first["builtins"] == ["work_items", "web_fetch"]
        assert first["skills"] == [skill_id]
        assert first["kbs"] == [kb_id]
        assert first["integrations"] == [integ_id]
        assert first["output_schema"] == {
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
        }
        # The shared tool node reached the second agent too; the rest is
        # None — nothing wired means the agent's behaviour is untouched.
        assert second["tools"] == [tool_id]
        assert second["builtins"] is None
        assert second["skills"] is None
        assert second["kbs"] is None
        assert second["integrations"] is None
        assert second["output_schema"] is None

        # Capability nodes are wiring, not steps.
        walked = [s.node_id for s in await _steps(session, run)]
        assert walked == ["t1", "ag1", "ag2"]


@pytest.mark.asyncio
async def test_manual_prompt_reaches_downstream_references():
    """The run API shapes {payload, prompt} as the trigger's declared
    outputs; `{{t1.prompt}}` must resolve downstream."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual", prompt="défaut du nœud"),
            _node(
                "c1",
                "condition.if",
                left="{{t1.prompt}}",
                operator="contains",
                right="incident",
            ),
        ],
        "edges": [_edge("t1", "c1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session,
            user,
            flow,
            # Shaped exactly as POST /flows/{id}/run builds it.
            payload={"payload": {}, "prompt": "analyse l'incident 42"},
        )
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        t1 = next(s for s in steps if s.node_id == "t1")
        assert t1.output == {"payload": {}, "prompt": "analyse l'incident 42"}
        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.output == {"result": True}, "le prompt a résolu en aval"


# ── Blueprint-style data utilities ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_break_exposes_json_fields_for_dotted_refs():
    """util.break parses a JSON object; `{{b1.fields.<name>}}` resolves after."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("b1", "util.break", value='{"severity": "critical", "count": 3}'),
            _node(
                "c1",
                "condition.if",
                left="{{b1.fields.severity}}",
                operator="equals",
                right="critical",
            ),
        ],
        "edges": [_edge("t1", "b1"), _edge("b1", "c1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        b1 = next(s for s in steps if s.node_id == "b1")
        assert b1.output["fields"] == {"severity": "critical", "count": 3}
        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.output == {"result": True}, "the broken-out field resolved"


@pytest.mark.asyncio
async def test_break_on_non_json_routes_the_error_port():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("b1", "util.break", value="not json at all"),
            _node("rescue", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "b1"),
            _edge("b1", "rescue", sh="error", kind="error"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", "wired error port rescues the run"
        walked = [s.node_id for s in await _steps(session, run)]
        assert "rescue" in walked


@pytest.mark.asyncio
async def test_cast_branches_ok_or_fail_without_killing_the_run():
    def cast_graph(value):
        return {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node("k1", "util.cast", value=value, to="number"),
                _node("okn", "condition.if", left="x", operator="is_not_empty"),
                _node("bad", "condition.if", left="x", operator="is_not_empty"),
            ],
            "edges": [
                _edge("t1", "k1"),
                _edge("k1", "okn", sh="ok", kind="condition_branch"),
                _edge("k1", "bad", sh="fail", kind="condition_branch"),
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }

    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=cast_graph("42.5"))
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        k1 = next(s for s in steps if s.node_id == "k1")
        assert k1.output["value"] == 42.5
        walked = [s.node_id for s in steps]
        assert "okn" in walked and "bad" not in walked

        user2, flow2 = await _seed(session, graph=cast_graph("quarante-deux"))
        run2, outcome2 = await _run_to_outcome(session, user2, flow2)
        assert outcome2.kind == "completed", "a failed cast is a branch, not a crash"
        walked2 = [s.node_id for s in await _steps(session, run2)]
        assert "bad" in walked2 and "okn" not in walked2


@pytest.mark.asyncio
async def test_select_picks_by_key_and_falls_back_to_default():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "s1",
                "util.select",
                key="{{t1.payload.level}}",
                options={"high": "page the on-call", "low": "log it"},
                default="ignore it",
            ),
        ],
        "edges": [_edge("t1", "s1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, _ = await _run_to_outcome(session, user, flow, payload={"level": "high"})
        s1 = next(s for s in await _steps(session, run) if s.node_id == "s1")
        assert s1.output == {"value": "page the on-call"}

        user2, flow2 = await _seed(session, graph=graph)
        run2, _ = await _run_to_outcome(
            session, user2, flow2, payload={"level": "weird"}
        )
        s2 = next(s for s in await _steps(session, run2) if s.node_id == "s1")
        assert s2.output == {"value": "ignore it"}


@pytest.mark.asyncio
async def test_sequence_starts_branches_in_declared_order():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("sq", "util.sequence"),
            _node("a", "condition.if", left="x", operator="is_not_empty"),
            _node("b", "condition.if", left="x", operator="is_not_empty"),
            _node("c", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "sq"),
            # Declared out of order on purpose: then_N decides, not edge order.
            _edge("sq", "c", sh="then_3"),
            _edge("sq", "a", sh="then_1"),
            _edge("sq", "b", sh="then_2"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        walked = [s.node_id for s in await _steps(session, run)]
        assert walked == ["t1", "sq", "a", "b", "c"]


@pytest.mark.asyncio
async def test_stop_completes_the_run_and_drops_the_rest_of_the_queue():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("sq", "util.sequence"),
            _node("x1", "util.stop", result="stopped on purpose"),
            _node("never", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            # then_1 stops the run; then_2 would run `never` if stop didn't halt.
            _edge("sq", "x1", sh="then_1"),
            _edge("sq", "never", sh="then_2"),
            _edge("t1", "sq"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        assert run.status == "completed"
        walked = [s.node_id for s in await _steps(session, run)]
        assert "never" not in walked, "stop drops whatever was still queued"
        assert run.result_summary == "stopped on purpose"


@pytest.mark.asyncio
async def test_switch_routes_the_per_case_handle():
    """The Blueprint shape: one output socket per case (`case:<name>`), no
    edge labels involved — the handle itself is the route."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "sw",
                "condition.switch",
                value="{{t1.payload.kind}}",
                cases={"incident": "incident", "alert": "alert"},
            ),
            _node("inc", "condition.if", left="x", operator="is_not_empty"),
            _node("al", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            _edge("t1", "sw"),
            _edge("sw", "inc", sh="case:incident", kind="condition_branch"),
            _edge("sw", "al", sh="case:alert", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload={"kind": "alert"}
        )
        assert outcome.kind == "completed"
        walked = [s.node_id for s in await _steps(session, run)]
        assert "al" in walked and "inc" not in walked


# ── Coordination capabilities: shared state + work items ─────────────


@pytest.mark.asyncio
async def test_coordination_capabilities_wire_namespace_and_peers(monkeypatch):
    """One Shared state node + one Work items node feeding two agents: both
    agents get the tools, the SAME run-scoped namespace, and each other as
    work-item peers — the wiring is the sharing contract."""
    calls: dict[str, dict] = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        calls[agent.name] = {
            "builtins": kw.get("session_builtin_tool_ids") or [],
            "ns": kw.get("shared_state_namespace"),
            "peers": kw.get("session_work_item_owner_agent_ids"),
        }
        return f"ok {agent.name}", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("agA", "agent", agent_id="A", prompt="a"),
            _node("agB", "agent", agent_id="B", prompt="b"),
            _node("ss", "capability.shared_state", scope="run"),
            _node("wi", "capability.work_items", peer_agent_ids=[]),
        ],
        "edges": [
            _edge("t1", "agA"),
            _edge("agA", "agB"),
            _edge("ss", "agA", sh="provides", th="shared_state", kind="capability"),
            _edge("ss", "agB", sh="provides", th="shared_state", kind="capability"),
            _edge("wi", "agA", sh="provides", th="work_items", kind="capability"),
            _edge("wi", "agB", sh="provides", th="work_items", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=[Agent.__table__])
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            agent_a = Agent(id=uuid.uuid4(), user_id=user.id, name="Alpha")
            agent_b = Agent(id=uuid.uuid4(), user_id=user.id, name="Beta")
            session.add_all([agent_a, agent_b])
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent_a.id)
            flow.graph["nodes"][2]["config"]["agent_id"] = str(agent_b.id)

            run, outcome = await _run_to_outcome(session, user, flow)
            assert outcome.kind == "completed"

            for name in ("Alpha", "Beta"):
                assert "shared_state" in calls[name]["builtins"]
                assert "work_items" in calls[name]["builtins"]
                assert calls[name]["ns"] == f"flow-run:{run.id}", (
                    "both agents share ONE run-scoped namespace"
                )
            assert calls["Alpha"]["peers"] == [str(agent_b.id)]
            assert calls["Beta"]["peers"] == [str(agent_a.id)]


@pytest.mark.asyncio
async def test_create_work_item_dedups_on_correlation_key(monkeypatch):
    from app.models.agent import Agent
    from app.models.work_item import WorkItem

    graph = {
        "nodes": [
            _node(
                "t1", "trigger.manual"
            ),
            _node(
                "cw",
                "action.create_work_item",
                title="Traiter le ticket",
                correlation_key="ticket-42",
            ),
        ],
        "edges": [_edge("t1", "cw")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__, WorkItem.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            run1, outcome1 = await _run_to_outcome(session, user, flow)
            assert outcome1.kind == "completed"
            first_id = next(
                s for s in await _steps(session, run1) if s.node_id == "cw"
            ).output["work_item_id"]

            # Second run, same correlation key: the open item is REUSED.
            run2, outcome2 = await _run_to_outcome(session, user, flow)
            assert outcome2.kind == "completed"
            second_id = next(
                s for s in await _steps(session, run2) if s.node_id == "cw"
            ).output["work_item_id"]
            assert second_id == first_id, "open item reused, not duplicated"

            count = (
                await session.execute(
                    select(WorkItem).where(WorkItem.correlation_key == "ticket-42")
                )
            ).scalars().all()
            assert len(count) == 1


@pytest.mark.asyncio
async def test_create_work_item_wait_parks_until_completion(monkeypatch):
    from app.models.agent import Agent
    from app.models.work_item import WorkItem
    from app.services.company_managed_writes import agent_writer
    from app.services.work_item import complete_work_item

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "cw",
                "action.create_work_item",
                title="Tâche bloquante",
                wait=True,
            ),
        ],
        "edges": [_edge("t1", "cw")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__, WorkItem.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            run, outcome = await _run_to_outcome(session, user, flow)
            assert outcome.kind == "park", "wait=true parks the run"
            assert run.status == "awaiting"
            waiting = (run.state or {}).get("waiting") or {}
            assert waiting.get("kind") == "work_item"

            # An agent completes the item; the resumed walk publishes its result.
            await complete_work_item(
                waiting["work_item_id"],
                user_id=str(user.id),
                result="fait: 3 hôtes corrigés",
                writer=agent_writer(is_company_managed=False),
            )
            outcome2 = await FlowWalker(session, run, user).run_burst()
            await session.commit()
            assert outcome2.kind == "completed"
            cw = next(s for s in await _steps(session, run) if s.node_id == "cw")
            assert cw.output["result"] == "fait: 3 hôtes corrigés"


@pytest.mark.asyncio
async def test_work_item_trigger_claims_and_settles_the_item(monkeypatch):
    from app.models.agent import Agent
    from app.models.work_item import WorkItem
    from app.services.company_managed_writes import person_writer
    from app.services.flow_triggers import process_work_item_flows
    from app.services.work_item import create_work_item, get_work_item

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        return "ticket traité", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    # The flow runs an agent node, so the trigger's AI gate holds it back on an
    # unlicensed deployment. The scratch database has no licence: the test
    # states the licensed precondition instead of inheriting whatever the
    # process-wide database happened to say.
    async def _licensed() -> bool:
        return True

    monkeypatch.setattr("app.services.agentic_gate._read_licence", _licensed)

    graph = {
        "nodes": [
            _node("t1", "trigger.work_item", owner_agent_id="OWNER"),
            _node("ag", "agent", agent_id="OWNER", prompt="Traite {{t1.title}}"),
        ],
        "edges": [_edge("t1", "ag")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__, WorkItem.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            owner = Agent(id=uuid.uuid4(), user_id=user.id, name="Owner")
            session.add(owner)
            flow.status = "active"
            await session.commit()
            flow.graph["nodes"][0]["config"]["owner_agent_id"] = str(owner.id)
            flow.graph["nodes"][1]["config"]["agent_id"] = str(owner.id)
            await session.commit()

            item = await create_work_item(
                user_id=str(user.id),
                title="Ticket 77",
                owner_agent_id=str(owner.id),
                source="manual",
                writer=person_writer(is_admin=False),
            )

            fired = await process_work_item_flows(session)
            assert fired == 1
            after_claim = await get_work_item(item["id"], user_id=str(user.id))
            assert after_claim["status"] == "claimed", "claimed before the run"

            run = (
                await session.execute(select(FlowRun).order_by(FlowRun.created_at))
            ).scalars().first()
            assert run is not None and run.trigger_type == "work_item"
            outcome = await FlowWalker(session, run, user).run_burst()
            await session.commit()
            assert outcome.kind == "completed"

            settled = await get_work_item(item["id"], user_id=str(user.id))
            assert settled["status"] == "done"
            assert "ticket traité" in (settled["result"] or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("company_managed_flow", [False, True], ids=["personal", "company"])
async def test_only_a_company_managed_flow_takes_over_a_company_managed_item(
    monkeypatch, company_managed_flow
):
    """A personal flow's ``trigger.work_item`` never lists a company-managed
    item, so nothing is claimed; a company-managed flow claims it and its run
    settles it with the run's snapshot of the flow's tier."""
    from app.models.agent import Agent
    from app.models.work_item import WorkItem
    from app.services.company_managed_writes import person_writer
    from app.services.flow_triggers import process_work_item_flows
    from app.services.work_item import create_work_item, get_work_item

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        return "fleet ticket closed", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    async def _licensed() -> bool:
        return True

    monkeypatch.setattr("app.services.agentic_gate._read_licence", _licensed)

    graph = {
        "nodes": [
            _node("t1", "trigger.work_item", owner_agent_id="OWNER"),
            _node("ag", "agent", agent_id="OWNER", prompt="Handle {{t1.title}}"),
        ],
        "edges": [_edge("t1", "ag")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__, WorkItem.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            owner = Agent(
                id=uuid.uuid4(), user_id=user.id, name="Fleet owner", is_company_managed=True
            )
            session.add(owner)
            flow.status = "active"
            flow.is_company_managed = company_managed_flow
            await session.commit()
            flow.graph["nodes"][0]["config"]["owner_agent_id"] = str(owner.id)
            flow.graph["nodes"][1]["config"]["agent_id"] = str(owner.id)
            await session.commit()

            item = await create_work_item(
                user_id=None,
                title="Fleet ticket",
                owner_agent_id=str(owner.id),
                source="manual",
                is_company_managed=True,
                writer=person_writer(is_admin=True),
            )

            fired = await process_work_item_flows(session)
            after = await get_work_item(item["id"], user_id=None)
            if not company_managed_flow:
                assert fired == 0
                assert after["status"] == "open" and after["claimed_by_agent_id"] is None
                assert (
                    await session.execute(select(func.count()).select_from(FlowRun))
                ).scalar_one() == 0
                return

            assert fired == 1 and after["status"] == "claimed"
            run = (
                await session.execute(select(FlowRun).order_by(FlowRun.created_at))
            ).scalars().first()
            assert run.is_company_managed is True, "the run keeps the flow's tier"
            outcome = await FlowWalker(session, run, user).run_burst()
            await session.commit()
            assert outcome.kind == "completed"

            settled = await get_work_item(item["id"], user_id=None)
            assert settled["status"] == "done"
            assert "fleet ticket closed" in (settled["result"] or "")


# ── The built-in example flow ────────────────────────────────────────
# The seeded demo IS the functional smoke test: the same graph a fresh
# platform ships is validated and executed here, so "the example runs" and
# "the engine works" stay one and the same fact.


@pytest.mark.asyncio
async def test_example_flow_routes_high_severity():
    from app.services.flow_examples import example_triage_graph
    from app.services.flow_graph import validate_graph

    graph = example_triage_graph()
    result = validate_graph(graph)
    assert result.is_valid
    assert result.issues == [], "the shipped example must be warning-free"

    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session,
            user,
            flow,
            payload={
                "payload": {},
                "prompt": '{"severity": "high", "title": "Intrusion X"}',
            },
        )
        assert outcome.kind == "completed"
        walked = [s.node_id for s in await _steps(session, run)]
        assert "st_high" in walked
        assert "st_med" not in walked and "st_def" not in walked
        assert run.result_summary == "URGENT — page the on-call about “Intrusion X”."


@pytest.mark.asyncio
async def test_example_flow_default_and_error_paths():
    from app.services.flow_examples import example_triage_graph

    async with _temp_database() as sessions, sessions() as session:
        # Unknown severity → the Default socket.
        user, flow = await _seed(session, graph=example_triage_graph())
        run, outcome = await _run_to_outcome(
            session,
            user,
            flow,
            payload={"payload": {}, "prompt": '{"severity": "haute"}'},
        )
        assert outcome.kind == "completed"
        assert "st_def" in [s.node_id for s in await _steps(session, run)]

        # Non-JSON input → Break's error port → the Bad input stop.
        user2, flow2 = await _seed(session, graph=example_triage_graph())
        run2, outcome2 = await _run_to_outcome(
            session,
            user2,
            flow2,
            payload={"payload": {}, "prompt": "pas du json"},
        )
        assert outcome2.kind == "completed", "the error path rescues the run"
        walked = [s.node_id for s in await _steps(session, run2)]
        assert "stop_bad" in walked and "sw" not in walked


@pytest.mark.asyncio
async def test_flow_builder_tool_authors_and_runs_a_flow():
    """The flow_builder builtin tool: an agent composes a graph
    WITHOUT positions, validation refuses a broken graph with the issues,
    create auto-layouts + saves, run executes, and the step journal comes
    back readable — the full prompt-to-running-flow loop, headless."""
    from app.models.flow import FlowVersion
    from app.models.group import Group, GroupMember
    from app.services.builtin_tools import build_flow_builder_tool

    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        FlowVersion.__table__,
                        Group.__table__,
                        GroupMember.__table__,
                    ],
                )
            )
            await session.commit()
        async with sessions() as session:
            user, _ = await _seed(
                session,
                graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
            )
            _defs, executor = build_flow_builder_tool(session, str(user.id))

            async def call(op, **kwargs):
                from app.tools.base import ToolCall

                result = await executor.execute_tool(
                    ToolCall(id="t", name=op, arguments=kwargs)
                )
                return result.content if isinstance(result.content, str) else str(
                    result.content
                )

            # Catalog is discoverable and carries the authoring primer.
            catalog = await call("get_node_catalog")
            assert "trigger.manual" in catalog and "output_variables" in catalog
            assert "auto-layout" in catalog
            # The agent learns which outputs are text, so it reads a JSON
            # answer through `fields` rather than `output.KEY`.
            agent_kind = next(
                nk for nk in json.loads(catalog)["node_kinds"] if nk["kind"] == "agent"
            )
            outputs = {v["name"]: v.get("type") for v in agent_kind["output_variables"]}
            assert (outputs["output"], outputs["fields"]) == ("text", "object")

            # A broken graph is refused with the validator's own issues.
            bad = await call(
                "validate_flow_graph",
                graph=json.dumps({
                    "nodes": [{"id": "x", "kind": "no.such.kind", "config": {}}],
                    "edges": [],
                }),
            )
            assert "INVALID" in bad and "node.unknown_kind" in bad

            # A good graph, NO positions anywhere: create auto-layouts and saves.
            graph = {
                "nodes": [
                    {"id": "t1", "kind": "trigger.manual", "config": {
                        "parameters": [
                            {"name": "who", "type": "string", "required": True}
                        ]
                    }},
                    {"id": "greet", "kind": "util.cast", "config": {
                        "value": "hello {{t1.payload.who}}", "to": "text"}},
                ],
                "edges": [
                    {"id": "e1", "source": "t1", "source_handle": "out",
                     "target": "greet", "target_handle": "in", "kind": "data"},
                ],
            }
            created = await call(
                "create_flow", name="Tool-built greeting", graph=json.dumps(graph)
            )
            assert "created" in created
            flow_id = created.split("`")[1]

            stored = json.loads(await call("get_flow", flow_id=flow_id))
            positions = [n["position"] for n in stored["graph"]["nodes"]]
            assert all(
                isinstance(p, dict) and "x" in p and "y" in p for p in positions
            ), "auto-layout must have placed every node"

            # Run with typed parameters and read the journal.
            started = await call(
                "run_flow", flow_id=flow_id,
                parameters=json.dumps({"who": "monde"}),
            )
            assert "started" in started
            run_id = started.split("`")[1]

            from app.models.flow_run import FlowRun

            run = (
                await session.execute(
                    select(FlowRun).where(FlowRun.id == uuid.UUID(run_id))
                )
            ).scalar_one()
            outcome = await FlowWalker(session, run, user).run_burst()
            await session.commit()
            assert outcome.kind == "completed"

            journal = await call("get_flow_run", run_id=run_id)
            assert "completed" in journal and "hello monde" in journal

            # Bad parameters are refused with the coercion errors.
            refused = await call("run_flow", flow_id=flow_id, parameters="{}")
            assert "do not match" in refused

            # The webhook use case: a trigger, then an HTTP POST with a header
            # and a JSON body. (The catalog's relay / runner filters are pinned
            # in test_flow_builder_catalog.py.)
            catalog_json = json.loads(await call("get_node_catalog"))
            assert "relay_groups" in catalog_json["sources"]

            hook = {
                "nodes": [
                    {"id": "go", "kind": "trigger.manual", "config": {}},
                    {
                        "id": "notify",
                        "kind": "action.http_request",
                        "config": {
                            "method": "POST",
                            "url": "http://127.0.0.1:8790/hook",
                            "headers": {"X-GLaDOS-Token": "t0k3n"},
                            "body": '{"title": "Copilot", "message": "done"}',
                        },
                    },
                ],
                "edges": [
                    {"id": "e1", "source": "go", "source_handle": "out",
                     "target": "notify", "target_handle": "in", "kind": "data"},
                ],
            }
            created_hook = await call("create_flow", name="Notify GLaDOS", graph=json.dumps(hook))
            assert "created" in created_hook
            hook_id = created_hook.split("`")[1]
            stored_hook = json.loads(await call("get_flow", flow_id=hook_id))
            http_node = next(n for n in stored_hook["graph"]["nodes"] if n["id"] == "notify")
            assert http_node["config"]["headers"] == {"X-GLaDOS-Token": "t0k3n"}
            assert "now active" in await call("activate_flow", flow_id=hook_id)

            # Activation keeps the REST route's gates: a trigger is needed, and
            # a webhook's delivery secret is never put in a tool result.
            lone = {"nodes": [{"id": "u", "kind": "util.cast", "config": {"value": "x", "to": "text"}}], "edges": []}
            lone_id = (await call("create_flow", name="No trigger", graph=json.dumps(lone))).split("`")[1]
            assert "needs a way to start" in await call("activate_flow", flow_id=lone_id)
            wh = {"nodes": [{"id": "w", "kind": "trigger.webhook", "config": {"path": "in"}}], "edges": []}
            wh_id = (await call("create_flow", name="Inbound", graph=json.dumps(wh))).split("`")[1]
            refused_wh = await call("activate_flow", flow_id=wh_id)
            assert "delivery secret" in refused_wh and "now active" not in refused_wh

            # Every write left the audit entry the REST routes leave, naming
            # the tool as the way in and the caller as the actor.
            from app.models.audit_log import AuditLog

            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.entity_type == "flow")
                    )
                )
                .scalars()
                .all()
            )
            by_action = {}
            for row in rows:
                by_action.setdefault(row.action, []).append(row)
            assert len(by_action["create"]) == 4
            assert len(by_action["flow.run"]) == 1
            assert len(by_action["flow.activate"]) == 1
            assert str(by_action["flow.activate"][0].entity_id) == hook_id
            assert all(r.details["via"] == "flow_builder" for r in rows)
            assert {r.user_id for r in rows} == {user.id}

            # An update and a deactivation are audited too.
            assert "updated" in await call("update_flow_graph", flow_id=hook_id, graph=json.dumps(hook))
            assert "draft" in await call("deactivate_flow", flow_id=hook_id)
            await session.commit()
            actions = (
                (
                    await session.execute(
                        select(AuditLog.action).where(
                            AuditLog.entity_id == uuid.UUID(hook_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert {"update", "flow.deactivate"} <= set(actions)


@pytest.mark.asyncio
async def test_flow_builder_tool_writes_follow_the_manage_rule_of_the_rest_routes():
    """A ``config`` grantee edits and activates a shared flow through the tool
    as they can in the UI; a ``read_only`` grantee and a stranger cannot."""
    from app.models.flow import FlowVersion
    from app.models.group import Group, GroupMember
    from app.services.builtin_tools import build_flow_builder_tool
    from app.tools.base import ToolCall

    graph = {
        "nodes": [{"id": "go", "kind": "trigger.manual", "config": {}}],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[FlowVersion.__table__, Group.__table__, GroupMember.__table__],
                )
            )
            await session.commit()
        async with sessions() as session:
            owner, flow = await _seed(session, graph=graph)
            editor = await _seed_user(session)
            reader = await _seed_user(session)
            stranger = await _seed_user(session)
            flow.shared_user_ids = [str(editor.id), str(reader.id)]
            flow.shared_user_permissions = {
                str(editor.id): "config",
                str(reader.id): "read_only",
            }
            await session.commit()

            async def call(user, op, **kwargs):
                _defs, executor = build_flow_builder_tool(session, str(user.id))
                result = await executor.execute_tool(ToolCall(id="t", name=op, arguments=kwargs))
                return str(result.content)

            fid = str(flow.id)
            payload = json.dumps(graph)
            assert "updated" in await call(editor, "update_flow_graph", flow_id=fid, graph=payload)
            assert "now active" in await call(editor, "activate_flow", flow_id=fid)
            assert "config-tier collaborator" in await call(reader, "deactivate_flow", flow_id=fid)
            assert "config-tier collaborator" in await call(
                reader, "update_flow_graph", flow_id=fid, graph=payload
            )
            assert "not found" in await call(stranger, "deactivate_flow", flow_id=fid)
            assert "now draft" in await call(owner, "deactivate_flow", flow_id=fid)


@pytest.mark.asyncio
async def test_example_flow_seeding_is_idempotent():
    from app.models.flow import Flow, FlowVersion
    from app.services.flow_examples import EXAMPLE_FLOW_NAME, seed_example_flow

    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[FlowVersion.__table__]
                )
            )
            await session.commit()
        async with sessions() as session:
            # The full educational pack: 16 flows + 4 declared event types.
            assert await seed_example_flow(session) == 16
            assert await seed_example_flow(session) == 0, "second boot seeds nothing"
            rows = (
                (
                    await session.execute(
                        select(Flow).where(Flow.name == EXAMPLE_FLOW_NAME)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].is_company_managed and rows[0].user_id is None

            all_examples = (
                (
                    await session.execute(
                        select(Flow).where(Flow.is_company_managed.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            assert len(all_examples) == 16
            assert all(f.tags == ["example"] for f in all_examples)
            # The two event listeners ship ACTIVE so emissions reach them.
            active = {f.name for f in all_examples if f.status == "active"}
            assert active == {
                "Example: Typed events — listener",
                "Example: Delegation handler",
            }

            from app.models.flow_event import FlowEventType

            types = (
                (await session.execute(select(FlowEventType))).scalars().all()
            )
            assert {t.name for t in types} == {
                "Example: Ticket escalated",
                "Example: Task delegated",
                "Example: CTI analysis completed",
                "Example: TheHive alert received",
            }
            assert all(t.user_id is None for t in types)


@pytest.mark.asyncio
async def test_run_attachments_reach_the_agent_as_file_refs(monkeypatch):
    """Files attached to a run (trigger_payload["files"]) arrive at every
    agent node as FileRefs + a FileContext — the same shape chat uses."""
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.chat import Conversation, Message
    from app.models.file_attachment import FileAttachment
    from app.models.knowledge_base import KnowledgeBase
    from app.models.template_container import TemplateContainer

    seen: dict = {}

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        seen["files"] = kw.get("files")
        seen["file_context"] = kw.get("file_context")
        return "lu", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("ag", "agent", agent_id="X", prompt="Analyse {{t1.files}}"),
        ],
        "edges": [_edge("t1", "ag")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                # FileAttachment carries FKs into half the platform — create
                # the whole dependency chain for this test only.
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        Agent.__table__,
                        KnowledgeBase.__table__,
                        Conversation.__table__,
                        Message.__table__,
                        Assignment.__table__,
                        AssignmentRun.__table__,
                        TemplateContainer.__table__,
                        FileAttachment.__table__,
                    ],
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(session, graph=graph)
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Lecteur")
            attachment = FileAttachment(
                id=uuid.uuid4(),
                user_id=user.id,
                filename="rapport.txt",
                content_type="text/plain",
                size=42,
                s3_object_name=f"flows/test/{uuid.uuid4().hex}.txt",
                source="flow",
            )
            session.add_all([agent, attachment])
            await session.commit()
            flow.graph["nodes"][1]["config"]["agent_id"] = str(agent.id)

            run, outcome = await _run_to_outcome(
                session,
                user,
                flow,
                payload={
                    "payload": {},
                    "prompt": "analyse le fichier",
                    "files": [
                        {
                            "file_id": str(attachment.id),
                            "filename": "rapport.txt",
                            "content_type": "text/plain",
                            "size": 42,
                        }
                    ],
                },
            )
            assert outcome.kind == "completed"
            files = seen["files"]
            assert files is not None and len(files) == 1
            assert files[0].file_id == str(attachment.id)
            assert files[0].filename == "rapport.txt"
            ctx = seen["file_context"]
            assert ctx is not None and not ctx.is_empty
            # The trigger step published the metadata for `{{t1.files}}`.
            t1 = next(s for s in await _steps(session, run) if s.node_id == "t1")
            assert t1.output["files"][0]["filename"] == "rapport.txt"


# ── Typed events (declare → emit → listen), the UE dispatcher pattern ────────


async def _declare_event_type(session, user, *, name="TicketEscalated", fields=None):
    et = FlowEventType(
        user_id=user.id,
        name=name,
        fields=fields
        if fields is not None
        else [
            {"name": "title", "type": "string", "required": True},
            {"name": "severity", "type": "number", "required": False},
        ],
    )
    session.add(et)
    await session.commit()
    return et


@pytest.mark.asyncio
async def test_company_listener_hears_global_events():
    """A company-managed ACTIVE listener (user_id NULL) must hear a user's
    global emission — and its run is attributed to the EMITTER."""
    async with _temp_database() as sessions, sessions() as session:
        user, emitter = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(session, user)
        listener = Flow(
            id=uuid.uuid4(),
            user_id=None,
            is_company_managed=True,
            name="Company listener",
            status="active",
            graph={
                "nodes": [
                    _node("t1", "trigger.event", event_type_id=str(et.id)),
                    _node("s1", "util.stop"),
                ],
                "edges": [_edge("t1", "s1")],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )
        session.add(listener)
        emitter.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "e1", "action.emit_event",
                    event_type_id=str(et.id), scope="global",
                    mappings={"title": "Disque plein"},
                ),
            ],
            "edges": [_edge("t1", "e1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, emitter)
        assert outcome.kind == "completed"
        e1 = next(s_ for s_ in await _steps(session, run) if s_.node_id == "e1")
        assert e1.output["listeners_started"] == 1

        child = (
            await session.execute(
                select(FlowRun).where(FlowRun.flow_id == listener.id)
            )
        ).scalar_one()
        assert child.user_id == user.id, "attributed to the emitting user"


@pytest.mark.asyncio
async def test_object_primitive_coerces_against_its_declared_type():
    """The UE struct pin: a Value of type Object conforms to a
    declared type — fields coerced, sub-paths readable, a deleted type
    yields {} instead of a crash."""
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(
            session, user, name="Ticket",
            fields=[
                {"name": "title", "type": "string", "required": True},
                {"name": "severity", "type": "number", "required": False},
            ],
        )
        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "p1", "util.primitive",
                    value_type="object",
                    object_type_id=str(et.id),
                    object_fields={"title": "Disque plein", "severity": "3"},
                ),
                _node(
                    "p2", "util.primitive",
                    value_type="object",
                    object_type_id=str(uuid.uuid4()),  # deleted/unknown type
                ),
                _node(
                    "fin", "util.cast",
                    value="{{p1.value.title}} sev={{p1.value.severity}} vide={{p2.value}}",
                    to="text",
                ),
            ],
            "edges": [_edge("t1", "fin")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        fin = next(s_ for s_ in await _steps(session, run) if s_.node_id == "fin")
        # Coerced: severity became a NUMBER (3, not "3"); the unknown type {}.
        assert fin.output["value"] == "Disque plein sev=3 vide={}"


@pytest.mark.asyncio
async def test_emit_event_starts_bound_listener_run_with_typed_fields():
    async with _temp_database() as sessions, sessions() as session:
        user, emitter = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(session, user)

        # The listener: an ACTIVE flow bound to the declaration.
        listener = Flow(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Listener",
            status="active",
            graph={
                "nodes": [
                    _node("t1", "trigger.event", event_type_id=str(et.id)),
                    _node("s1", "util.stop"),
                ],
                "edges": [_edge("t1", "s1")],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )
        session.add(listener)

        emitter.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "e1",
                    "action.emit_event",
                    event_type_id=str(et.id),
                    scope="global",
                    mappings={"title": "{{t1.payload.subject}}", "severity": "3"},
                ),
            ],
            "edges": [_edge("t1", "e1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(
            session, user, emitter, payload={"subject": "Disk full"}
        )
        assert outcome.kind == "completed"
        e1 = next(s for s in await _steps(session, run) if s.node_id == "e1")
        assert e1.output["listeners_started"] == 1

        child = (
            await session.execute(
                select(FlowRun).where(FlowRun.flow_id == listener.id)
            )
        ).scalar_one()
        assert child.trigger_type == "event"
        # Typed and coerced: the template rendered, the number became one.
        assert child.trigger_payload["fields"] == {
            "title": "Disk full",
            "severity": 3,
        }
        assert child.trigger_payload["event"]["name"] == "TicketEscalated"

        # The listener's trigger step publishes {{t1.fields.title}}.
        outcome2 = await FlowWalker(session, child, user).run_burst()
        await session.commit()
        assert outcome2.kind == "completed"
        t1 = next(s for s in await _steps(session, child) if s.node_id == "t1")
        assert t1.output["fields"]["title"] == "Disk full"


@pytest.mark.asyncio
async def test_emit_scope_flow_stays_inside_the_emitting_flow():
    async with _temp_database() as sessions, sessions() as session:
        user, emitter = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(session, user, fields=[])

        other = Flow(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Other flow",
            status="active",
            graph={
                "nodes": [_node("t1", "trigger.event", event_type_id=str(et.id))],
                "edges": [],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )
        session.add(other)

        # The emitter is ALSO active and listens to its own event type — a
        # `flow`-scoped emit reaches it and nothing else.
        emitter.status = "active"
        emitter.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "e1",
                    "action.emit_event",
                    event_type_id=str(et.id),
                    scope="flow",
                ),
                _node("t2", "trigger.event", event_type_id=str(et.id)),
            ],
            "edges": [_edge("t1", "e1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, emitter)
        assert outcome.kind == "completed"
        e1 = next(s for s in await _steps(session, run) if s.node_id == "e1")
        assert e1.output["listeners_started"] == 1

        started = (
            (await session.execute(select(FlowRun).where(FlowRun.id != run.id)))
            .scalars()
            .all()
        )
        assert [r.flow_id for r in started] == [emitter.id]
        assert started[0].trigger_context["trigger_node_id"] == "t2"


@pytest.mark.asyncio
async def test_emit_missing_required_field_fails_the_node():
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(session, user)
        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "e1",
                    "action.emit_event",
                    event_type_id=str(et.id),
                    scope="flow",
                    mappings={"severity": "2"},  # `title` (required) missing
                ),
            ],
            "edges": [_edge("t1", "e1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "failed"
        assert "title" in (run.error or "")
        # Nothing was multicast.
        events = (await session.execute(select(FlowEvent))).scalars().all()
        assert events == []


@pytest.mark.asyncio
async def test_on_delegate_completion_join_carries_the_result(monkeypatch):
    """The completion mode of "On delegate": the walker stamps an
    emission spec on the delegated task instead of emitting at spawn; when
    the background task reaches a terminal state, the sub-agent completion
    path emits the declared event carrying {{delegation.result}} and
    {{delegation.status}} — and a downstream Wait for event JOINS it."""
    task_uuid = "11111111-1111-1111-1111-111111111111"

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        sink = kw.get("delegations_out")
        if sink is not None:
            sink.append(
                {
                    "task_id": task_uuid,
                    "target_agent_id": "22222222-2222-2222-2222-222222222222",
                    "target_agent_name": "Analyste",
                    "task": "analyser les IOCs",
                }
            )
        return "délégué, je poursuis", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    from app.models.agent import Agent
    from app.models.chat import Conversation
    from app.models.sub_agent_task import SubAgentTask

    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        Agent.__table__,
                        Conversation.__table__,
                        SubAgentTask.__table__,
                    ],
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(
                session,
                graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
            )
            et = await _declare_event_type(
                session, user, name="AnalyseTerminée",
                fields=[
                    {"name": "task", "type": "string", "required": True},
                    {"name": "result", "type": "string", "required": False},
                    {"name": "status", "type": "string", "required": False},
                ],
            )
            agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Chef")
            session.add(agent)
            task = SubAgentTask(
                id=uuid.UUID(task_uuid),
                user_id=user.id,
                parent_agent_id=agent.id,
                target_agent_id=agent.id,
                task_description="analyser les IOCs",
                status="running",
            )
            session.add(task)
            await session.commit()

            cap_edge = _edge("cap", "ag", sh="provides", th="on_delegate")
            cap_edge["kind"] = "capability"
            flow.graph = {
                "nodes": [
                    _node("t1", "trigger.manual"),
                    _node("ag", "agent", agent_id=str(agent.id), prompt="délègue"),
                    _node(
                        "cap", "capability.on_delegate",
                        event_type_id=str(et.id), emit_on="completion",
                        scope="run",
                        mappings={
                            "task": "{{delegation.task}}",
                            "result": "{{delegation.result}}",
                            "status": "{{delegation.status}}",
                        },
                    ),
                    _node("w1", "util.wait_event", event_type_id=str(et.id)),
                    _node(
                        "fin", "util.cast",
                        value="{{w1.fields.status}}: {{w1.fields.result}}",
                        to="text",
                    ),
                ],
                "edges": [
                    _edge("t1", "ag"),
                    cap_edge,
                    _edge("ag", "w1"),
                    _edge("w1", "fin"),
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }
            await session.commit()

            run, outcome = await _run_to_outcome(session, user, flow)
            # Completion mode: nothing emitted at spawn, the run parks on
            # the wait — and the spec is stamped on the delegated task.
            assert outcome.kind == "park"
            assert run.state["waiting"]["kind"] == "event"
            ag = next(s_ for s_ in await _steps(session, run) if s_.node_id == "ag")
            assert ag.output["delegation_events"] == 0
            from app.models.flow_event import FlowEvent

            assert (
                await session.scalar(
                    select(func.count()).select_from(FlowEvent)
                )
            ) == 0
            await session.refresh(task)
            specs = (task.metadata_json or {}).get("flow_completion_emissions")
            assert specs and specs[0]["scope"] == "run"
            assert specs[0]["run_id"] == str(run.id)

            # The background task ends: the terminal transition fires the
            # completion event with the RESULT.
            from app.services.flow_events import (
                emit_delegation_completion_events,
            )

            task.status = "completed"
            task.result = "RAPPORT: 3 IOCs actifs, campagne CMSTAR."
            assert (
                await emit_delegation_completion_events(
                    session, task, "completed"
                )
            ) == 1
            await session.commit()

            outcome2 = await FlowWalker(session, run, user).run_burst()
            await session.commit()
            assert outcome2.kind == "completed"
            w1 = next(s_ for s_ in await _steps(session, run) if s_.node_id == "w1")
            assert w1.output["fields"]["status"] == "completed"
            assert w1.output["fields"]["result"] == (
                "RAPPORT: 3 IOCs actifs, campagne CMSTAR."
            )
            assert w1.output["fields"]["task"] == "analyser les IOCs"
            fin = next(
                s_ for s_ in await _steps(session, run) if s_.node_id == "fin"
            )
            assert fin.input["value"] == (
                "completed: RAPPORT: 3 IOCs actifs, campagne CMSTAR."
            )


@pytest.mark.asyncio
async def test_wait_event_parks_then_resumes_on_a_matching_emission():
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(
            session, user, fields=[{"name": "status", "type": "string"}]
        )
        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node("w1", "util.wait_event", event_type_id=str(et.id)),
            ],
            "edges": [_edge("t1", "w1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "park"
        assert run.state["waiting"]["kind"] == "event"

        # Nothing emitted yet: a wake re-parks.
        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "park"

        # A `flow`-scoped emission from another run of the same flow.
        from app.services.flow_events import emit_flow_event

        await emit_flow_event(
            session,
            event_type=et,
            user_id=user.id,
            flow_id=flow.id,
            run_id=uuid.uuid4(),
            scope="flow",
            payload={"status": "ready"},
            chain_depth=1,
        )
        await session.commit()

        outcome3 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome3.kind == "completed"
        w1 = next(s for s in await _steps(session, run) if s.node_id == "w1")
        assert w1.output["fields"] == {"status": "ready"}
        assert w1.output["event"]["name"] == "TicketEscalated"


@pytest.mark.asyncio
async def test_emit_chain_depth_cap_stops_starting_runs():
    from app.models.flow_event import EVENT_CHAIN_MAX_DEPTH

    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(session, user, fields=[])
        flow.status = "active"
        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "e1",
                    "action.emit_event",
                    event_type_id=str(et.id),
                    scope="flow",
                ),
                _node("t2", "trigger.event", event_type_id=str(et.id)),
            ],
            "edges": [_edge("t1", "e1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        # An emit whose chain is already at the cap keeps the emission (audit,
        # waiters) but starts no further runs — the UE recursion guard.
        run, outcome = await _run_to_outcome(
            session,
            user,
            flow,
            context={"type": "event", "chain_depth": EVENT_CHAIN_MAX_DEPTH},
        )
        assert outcome.kind == "completed"
        e1 = next(s for s in await _steps(session, run) if s.node_id == "e1")
        assert e1.output["listeners_started"] == 0
        assert (await session.execute(select(FlowEvent))).scalars().all() != []


def test_coerce_event_fields_types_and_errors():
    from app.services.flow_events import coerce_event_fields

    declared = [
        {"name": "count", "type": "number", "required": True},
        {"name": "ok", "type": "boolean"},
        {"name": "meta", "type": "json"},
        {"name": "note", "type": "string"},
    ]
    payload, errors = coerce_event_fields(
        declared,
        {"count": "42", "ok": "true", "meta": '{"a": 1}', "note": "hi"},
    )
    assert errors == []
    assert payload == {"count": 42, "ok": True, "meta": {"a": 1}, "note": "hi"}

    _, errors = coerce_event_fields(declared, {"count": "not-a-number"})
    assert any("count" in e for e in errors)

    _, errors = coerce_event_fields(declared, {})
    assert any("count" in e for e in errors), "missing required field"


# ── Declared input parameters (the UE function signature) ────────────────────


def test_declared_parameters_normalises_both_formats():
    from app.services.flow_runner import declared_parameters

    # Legacy KEY_VALUE dict → typed list of strings.
    legacy = declared_parameters({"parameters": {"ticket": "T-1", "note": ""}})
    assert [(p["name"], p["type"]) for p in legacy] == [
        ("ticket", "string"),
        ("note", "string"),
    ]
    assert legacy[0]["default"] == "T-1"

    # Typed list passes through, junk rows dropped.
    typed = declared_parameters(
        {
            "parameters": [
                {"name": "count", "type": "number", "required": True},
                {"name": "  "},
                "junk",
            ]
        }
    )
    assert [(p["name"], p["type"], p["required"]) for p in typed] == [
        ("count", "number", True)
    ]
    assert declared_parameters({}) == []


def test_coerce_parameters_defaults_types_and_errors():
    from app.services.flow_runner import coerce_parameters

    declared = [
        {"name": "count", "type": "number", "required": True, "default": 5},
        {"name": "dry_run", "type": "boolean", "required": False},
    ]
    # Defaults fill the gaps; extras pass through untyped.
    payload, errors = coerce_parameters(declared, {"extra": "kept"})
    assert errors == []
    assert payload == {"count": 5, "extra": "kept"}

    # Caller values win over defaults and coerce.
    payload, errors = coerce_parameters(
        declared, {"count": "12", "dry_run": True}
    )
    assert errors == []
    assert payload == {"count": 12, "dry_run": True}

    # A type mismatch is an error, not a silent string.
    _, errors = coerce_parameters(declared, {"count": "douze"})
    assert any("count" in e for e in errors)


@pytest.mark.asyncio
async def test_flow_call_passes_typed_arguments_to_the_child():
    async with _temp_database() as sessions, sessions() as session:
        user, parent = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        child = Flow(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Child with signature",
            graph={
                "nodes": [
                    _node(
                        "t1",
                        "trigger.manual",
                        parameters=[
                            {"name": "ticket", "type": "string", "required": True},
                            {"name": "severity", "type": "number", "default": 1},
                        ],
                    ),
                ],
                "edges": [],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )
        session.add(child)
        parent.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "c1",
                    "flow.call",
                    flow_id=str(child.id),
                    input="contexte",
                    arguments={"ticket": "{{t1.payload.ref}}"},
                ),
            ],
            "edges": [_edge("t1", "c1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        _run, outcome = await _run_to_outcome(
            session, user, parent, payload={"ref": "T-42"}
        )
        # The parent parks on the sub-flow; the child got a TYPED payload.
        assert outcome.kind == "park"
        child_run = (
            await session.execute(
                select(FlowRun).where(FlowRun.flow_id == child.id)
            )
        ).scalar_one()
        assert child_run.trigger_payload["payload"] == {
            "ticket": "T-42",
            "severity": 1,
        }
        assert child_run.trigger_payload["prompt"] == "contexte"


@pytest.mark.asyncio
async def test_flow_call_argument_mismatch_fails_the_node():
    async with _temp_database() as sessions, sessions() as session:
        user, parent = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        child = Flow(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Child strict",
            graph={
                "nodes": [
                    _node(
                        "t1",
                        "trigger.manual",
                        parameters=[
                            {"name": "ticket", "type": "string", "required": True},
                        ],
                    ),
                ],
                "edges": [],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )
        session.add(child)
        parent.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node("c1", "flow.call", flow_id=str(child.id), input="x"),
            ],
            "edges": [_edge("t1", "c1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, parent)
        assert outcome.kind == "failed"
        assert "ticket" in (run.error or "")
        # No child run started with a malformed payload.
        started = (
            (
                await session.execute(
                    select(FlowRun).where(FlowRun.flow_id == child.id)
                )
            )
            .scalars()
            .all()
        )
        assert started == []


# ── Step journal: rendered inputs for troubleshooting ────────────────


@pytest.mark.asyncio
async def test_step_journal_records_rendered_inputs_even_on_failure():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "c1",
                "condition.if",
                left="{{t1.payload.ref}}",
                operator="equals",
                right="GO",
            ),
            # Break on prose fails — its journal row must still carry the
            # RENDERED input, that is the whole point of troubleshooting.
            _node("b1", "util.break", value="{{t1.payload.ref}}"),
        ],
        "edges": [
            _edge("t1", "c1"),
            _edge("c1", "b1", sh="true", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload={"ref": "GO"}
        )
        assert outcome.kind == "failed"
        steps = {s.node_id: s for s in await _steps(session, run)}
        # The condition computed with the RESOLVED value, and the journal
        # shows it — not the raw template.
        assert steps["c1"].input["left"] == "GO"
        assert steps["c1"].input["right"] == "GO"
        # The failed node keeps its rendered input alongside the error.
        assert steps["b1"].status == "failed"
        assert steps["b1"].input == {"value": "GO"}
        # The trigger step records no input (its payload IS its output).
        assert steps["t1"].input is None


def test_truncate_for_journal_bounds_strings_and_lists():
    from app.services.flow_runner import _truncate_for_journal

    out = _truncate_for_journal({"big": "x" * 5000, "list": list(range(100))})
    assert out["big"].endswith("… [truncated]") and len(out["big"]) < 2000
    assert len(out["list"]) == 50


# ── Value nodes (util.primitive): pre-evaluated constants ────────────────────


@pytest.mark.asyncio
async def test_primitive_values_resolve_without_wiring():
    from app.models.variable import Variable

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            # Wired to NOTHING — the whole point of a constant.
            _node("seuil", "util.primitive", value_type="number", value="3"),
            _node("nom", "util.primitive", value_type="text", variable_id=""),
            _node(
                "c1",
                "condition.if",
                left="{{seuil.value}}",
                operator="equals",
                right="3",
            ),
            _node("b1", "util.cast", value="{{nom.value}}", to="text"),
        ],
        "edges": [
            _edge("t1", "c1"),
            _edge("c1", "b1", sh="true", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        # A platform variable for the second primitive.
        variable = Variable(user_id=user.id, name="pays", value="France")
        session.add(variable)
        await session.commit()
        graph["nodes"][2]["config"]["variable_id"] = str(variable.id)
        flow.graph = graph
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = {s.node_id: s for s in await _steps(session, run)}
        # The number literal was coerced and compared as its rendered text.
        assert steps["c1"].input["left"] == "3"
        # The variable-backed primitive resolved to the platform value.
        assert steps["b1"].input["value"] == "France"
        # Constants never execute: no journal rows for them.
        assert "seuil" not in steps and "nom" not in steps


def test_primitive_value_coercion():
    from app.services.flow_runner import primitive_value

    assert primitive_value({"value_type": "number", "value": "42"}, {}) == 42
    assert primitive_value({"value_type": "number", "value": "0.5"}, {}) == 0.5
    assert primitive_value({"value_type": "boolean", "value": "true"}, {}) is True
    assert primitive_value({"value_type": "json", "value": '{"a": 1}'}, {}) == {"a": 1}
    assert primitive_value({"value_type": "number", "value": "pas un nombre"}, {}) == "pas un nombre"
    assert (
        primitive_value({"variable_id": "abc", "value": "ignoré"}, {"abc": "V"})
        == "V"
    )


# ── Notify node: the platform bell ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_node_persists_a_bell_activity():
    from app.models.activity import Activity
    from app.models.agent import Agent

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "n1",
                "action.notify",
                title="Run fini : {{t1.payload.sujet}}",
                message="Détail du run",
            ),
        ],
        "edges": [_edge("t1", "n1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        # Activity FKs agents; neither table is in _TABLES.
        async with session.bind.begin() as conn:  # type: ignore[union-attr]
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Agent.__table__, Activity.__table__]
                )
            )
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload={"sujet": "phishing"}
        )
        assert outcome.kind == "completed"
        steps = {s.node_id: s for s in await _steps(session, run)}
        assert steps["n1"].output["delivered"] is True
        # The rendered title landed in the durable activity row.
        activity = (
            await session.execute(select(Activity).order_by(Activity.created_at))
        ).scalars().first()
        assert activity is not None
        assert activity.activity_type == "notification"
        assert activity.title == "Run fini : phishing"
        assert (activity.extra_data or {}).get("notification_source") == "flow"
        assert activity.agent_name == "Test flow"


# ── Flow variables + stateful control (the UE state nodes) ──────────────────


@pytest.mark.asyncio
async def test_while_converges_with_set_variable_counter():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("init", "util.set_variable", name="n", mode="set", value="0"),
            _node(
                "w1", "util.while",
                left="{{get.value}}", operator="lt", right="3",
                max_iterations=25,
            ),
            # Body: increment then loop back visually (drain rule re-runs w1).
            _node("inc", "util.set_variable", name="n", mode="increment", value="1"),
            _node("fin", "util.cast", value="fini:{{get.value}}", to="text"),
            _node("get", "util.get_variable", name="n", default="0"),
        ],
        "edges": [
            _edge("t1", "init"),
            _edge("init", "get"),
            _edge("get", "w1"),
            _edge("w1", "inc", sh="body"),
            # Refresh the read each pass so the condition sees the new value.
            _edge("inc", "get"),
            _edge("w1", "fin", sh="done"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        assert run.state["vars"]["n"] == 3
        fin = [s_ for s_ in await _steps(session, run) if s_.node_id == "fin"]
        assert fin and fin[-1].input["value"] == "fini:3"


@pytest.mark.asyncio
async def test_do_once_flip_flop_and_gate():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("lp", "util.loop", items='["a", "b", "c"]'),
            _node("once", "util.do_once"),
            _node("ff", "util.flip_flop"),
            _node("open_it", "util.set_variable", name="porte", mode="set", value="true"),
            _node("g1", "util.gate", variable="porte"),
            _node("after_gate", "util.cast", value="passé", to="text"),
        ],
        "edges": [
            _edge("t1", "lp"),
            _edge("lp", "once", sh="each"),
            _edge("once", "ff", sh="first"),
            _edge("once", "ff", sh="again"),
            # Gate: closed by default (variable unset) on early passes, the
            # loop's Done opens it and re-enters.
            _edge("lp", "open_it", sh="done"),
            _edge("open_it", "g1"),
            _edge("ff", "g1", sh="a"),
            _edge("ff", "g1", sh="b"),
            _edge("g1", "after_gate", sh="out"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        by_node: dict[str, list] = {}
        for s_ in steps:
            by_node.setdefault(s_.node_id, []).append(s_)
        # DoOnce: 3 loop passes → first once, again twice.
        assert [x.output["count"] for x in by_node["once"]] == [1, 2, 3]
        # FlipFlop alternates A/B/A.
        assert [x.output["is_a"] for x in by_node["ff"]] == [True, False, True]
        # The gate blocked while the variable was unset, passed once opened.
        gate_opens = [x.output["open"] for x in by_node["g1"]]
        assert False in gate_opens and True in gate_opens
        assert "after_gate" in by_node


# ── Expression node (sandboxed pure functions) ───────────────────────────────


def test_expression_evaluator_helpers_and_sandbox():
    import pytest as _pytest

    from app.services.flow_expression import ExpressionError, evaluate_expression

    assert evaluate_expression("round(2.678, 1)") == 2.7
    assert evaluate_expression('join(", ", sort(["b", "a"]))') == "a, b"
    assert evaluate_expression('get(json(\'{"a": {"b": 5}}\'), "a")["b"]') == 5
    assert evaluate_expression('upper("ok") if 3 > 2 else "no"') == "OK"
    assert evaluate_expression('append([1, 2], 3)') == [1, 2, 3]
    assert evaluate_expression("len(split(\'a b c\'))") == 3
    # The sandbox refuses everything outside the whitelist.
    for hostile in (
        "__import__(\'os\')",
        "open(\'/etc/passwd\')",
        "().__class__",
        "[x for x in [1]]",
        "2 ** 99999",
        "\'a\' * 99999999",
        "lambda: 1",
    ):
        with _pytest.raises(ExpressionError):
            evaluate_expression(hostile)
    # kwargs reach the helpers: `ast.keyword` has to be whitelisted or the
    # walk refuses the call before the evaluator\'s kwargs handling runs.
    assert evaluate_expression("round(2.678, ndigits=1)") == 2.7


def test_expression_evaluator_refuses_composed_resource_bombs():
    """Per-operation operand caps launder through composition — bound the result.

    Every expression below keeps each individual exponent under
    ``MAX_POW_EXPONENT`` and each multiplier under ``MAX_REPEAT``, so the
    per-operation guards pass; what has to stop them is the width/length of the
    operand and of the predicted result. They are refused *before* the value is
    computed, so the check itself cannot be the DoS.
    """
    import time

    import pytest as _pytest

    from app.services.flow_expression import ExpressionError, evaluate_expression

    for bomb in (
        "(9**512)**512**512",   # widths multiply: 9^(512^3)
        "(9**512)**512",        # already 831k bits
        "9999999999 ** 512",    # legal exponent, huge base
        '("a"*10000)*10000*10000',  # legal multipliers, terabyte result
        '("a"*10000)*10000',        # legal multipliers, 100 MB result
    ):
        # CPU time of this thread: a loaded runner stretches wall-clock time,
        # not the work the bound is about.
        started = time.thread_time()
        with _pytest.raises(ExpressionError):
            evaluate_expression(bomb)
        # A guard that computes the bomb before rejecting it is not a guard.
        assert time.thread_time() - started < 1.0, bomb

    # Ordinary arithmetic and concatenation still evaluate.
    assert evaluate_expression("2 ** 10") == 1024
    assert evaluate_expression('"ab" * 3') == "ababab"
    assert evaluate_expression('("a" * 10) + ("b" * 10)') == "a" * 10 + "b" * 10


def test_expression_evaluator_keeps_raw_errors_inside_the_contract():
    """Type errors must surface as ExpressionError, not escape as TypeError.

    The node\'s error port and the step journal both key off
    :class:`ExpressionError`; a raw ``TypeError`` from an operator would bypass
    them and surface as an unhandled run failure.
    """
    import pytest as _pytest

    from app.services.flow_expression import ExpressionError, evaluate_expression

    for bad in ("1 in 2", '-"a"', '"a" < 1', "1/0", "1e300 ** 5"):
        with _pytest.raises(ExpressionError):
            evaluate_expression(bad)


@pytest.mark.asyncio
async def test_expression_node_computes_without_llm():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("p1", "util.primitive", value_type="number", value="40"),
            _node(
                "x1",
                "util.expression",
                expression="({{p1.value}} + 2) * 10",
            ),
            _node(
                "x2",
                "util.expression",
                expression='{"double": {{x1.value}} * 2, "label": upper("fini")}',
            ),
        ],
        "edges": [
            _edge("t1", "x1"),
            _edge("x1", "x2"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
        assert steps["x1"].output["value"] == 420
        # Object results also publish `fields` for {{x2.fields.<key>}}.
        assert steps["x2"].output["fields"] == {"double": 840, "label": "FINI"}


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the local sandbox wrapper scrubs USERPROFILE and the interpreter cannot "
        "resolve a home directory on Windows (same as test_code_interpreter_mount); "
        "CI runs on Linux, where the local subprocess sandbox needs no service"
    ),
)
async def test_code_node_runs_through_the_platform_sandbox(monkeypatch):
    from app.core.config import settings as app_settings

    # The node rides the code interpreter's kill-switch; enable it here.
    monkeypatch.setattr(app_settings, "disable_code_interpreter", False)
    monkeypatch.setattr(app_settings, "custom_tool_sandbox", "local", raising=False)
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "c1",
                "util.code",
                code='import json\nprint(json.dumps({"upper": inputs["texte"].upper(), "n": len(inputs["texte"])}))',
                inputs={"texte": "{{t1.payload.mot}}"},
                timeout=30,
            ),
        ],
        "edges": [_edge("t1", "c1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload={"mot": "sandbox"}
        )
        assert outcome.kind == "completed", run.error
        steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
        assert steps["c1"].output["fields"] == {"upper": "SANDBOX", "n": 7}


# ── Debugger: breakpoints + step mode ────────────────────────────────────────


@pytest.mark.asyncio
async def test_breakpoint_pauses_then_continue_and_step():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("b1", "util.cast", value="un", to="text"),
            _node("b2", "util.cast", value="deux", to="text"),
            _node("b3", "util.cast", value="trois", to="text"),
        ],
        "edges": [
            _edge("t1", "b1", th="in"),
            _edge("b1", "b2", sh="ok", kind="condition_branch"),
            _edge("b2", "b3", sh="ok", kind="condition_branch"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    graph["nodes"][2]["config"]["breakpoint"] = True  # pause before b2
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)

        # Paused BEFORE b2: b1 ran, b2 has no journal row yet.
        assert outcome.kind == "park"
        assert run.status == "awaiting"
        assert run.state["waiting"] == {
            "kind": "breakpoint", "node": "b2", "released": False,
        }
        assert {s_.node_id for s_ in await _steps(session, run)} == {"t1", "b1"}

        # Release with STEP: b2 runs, then the walker pauses before b3.
        state = dict(run.state)
        state["waiting"] = {**state["waiting"], "released": True}
        state["step_mode"] = True
        run.state = state
        await session.commit()
        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "park"
        assert run.state["waiting"]["node"] == "b3"
        assert {s_.node_id for s_ in await _steps(session, run)} == {"t1", "b1", "b2"}

        # Release with CONTINUE: step mode off, the run finishes.
        state = dict(run.state)
        state["waiting"] = {**state["waiting"], "released": True}
        state["step_mode"] = False
        run.state = state
        await session.commit()
        outcome3 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome3.kind == "completed"
        assert {s_.node_id for s_ in await _steps(session, run)} == {
            "t1", "b1", "b2", "b3",
        }


@pytest.mark.asyncio
async def test_ask_user_parks_and_resumes_with_the_answer(monkeypatch):
    """The HITL node: notify → park kind "human" → answer injected → resume."""
    sent: list[dict] = []

    async def fake_notify(db, user_id, **kwargs):
        sent.append(kwargs)

    import app.services.notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify_web_user", fake_notify)

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "ask", "action.ask_user",
                title="Validation", question="On publie ?",
            ),
            _node("fin", "util.cast", value="reponse:{{ask.response}}", to="text"),
        ],
        "edges": [
            _edge("t1", "ask", th="in"),
            _edge("ask", "fin"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)

        # Parked on the question; the bell rang with the HITL source and the
        # deep-link metadata the notification lane needs.
        assert outcome.kind == "park"
        assert run.status == "awaiting"
        waiting = run.state["waiting"]
        assert waiting["kind"] == "human"
        assert waiting["node"] == "ask"
        assert waiting["question"] == "On publie ?"
        assert waiting["released"] is False
        assert len(sent) == 1
        assert sent[0]["extra_data"]["notification_source"] == "flow_hitl"
        assert sent[0]["extra_data"]["flow_run_id"] == str(run.id)

        # Unanswered resume tick: still parked, no duplicate notification.
        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "park"
        assert len(sent) == 1

        # The respond endpoint's write: answer + release, then resume.
        state = dict(run.state)
        state["waiting"] = {
            **state["waiting"],
            "released": True,
            "response": "Oui, publie",
            "responded_by": "admin@filigran.io",
        }
        run.state = state
        await session.commit()
        outcome3 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome3.kind == "completed"

        steps = await _steps(session, run)
        ask_steps = [s_ for s_ in steps if s_.node_id == "ask"]
        assert len(ask_steps) == 1  # ONE journal row for the parked node
        assert ask_steps[0].output == {
            "response": "Oui, publie",
            "option": "",
            "responded_by": "admin@filigran.io",
        }
        fin = [s_ for s_ in steps if s_.node_id == "fin"]
        assert fin and fin[-1].input["value"] == "reponse:Oui, publie"


@pytest.mark.asyncio
async def test_ask_user_with_options_parks_the_ask_and_resumes_with_the_option(monkeypatch):
    """#842 S6: options, recommendation and deadline park as the ask; the
    answer through the shared respond path makes {{ask.option}} the key."""
    from types import SimpleNamespace

    import app.services.notify as notify_mod
    from app.api.v1 import flow_runs as flow_runs_api

    async def fake_notify(db, user_id, **kwargs):
        return None

    async def fake_wake():
        return None

    monkeypatch.setattr(notify_mod, "notify_web_user", fake_notify)
    import app.services.orchestration_engine as engine

    monkeypatch.setattr(engine, "notify_orchestration_wake", fake_wake)

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node(
                "ask",
                "action.ask_user",
                title="Validation",
                question="On publie ?",
                options='[{"key": "A", "label": "Publier"}, {"key": "B", "label": "Attendre"}]',
                recommendation="B",
                deadline="2026-12-01T17:00:00Z",
            ),
            _node("fin", "util.cast", value="choix:{{ask.option}}", to="text"),
        ],
        "edges": [
            _edge("t1", "ask", th="in"),
            _edge("ask", "fin"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "park"
        ask = run.state["waiting"]["ask"]
        assert [o["key"] for o in ask["options"]] == ["A", "B"]
        assert ask["recommendation"] == "B"
        assert ask["deadline"] == "2026-12-01T17:00:00+00:00"

        async def visible(db, u, rid):
            return run

        monkeypatch.setattr(flow_runs_api, "get_visible_run", visible)
        monkeypatch.setattr(flow_runs_api, "_get_controllable_run", lambda *a, **k: visible(None, None, None))

        async def not_paused(db, r):
            return None

        monkeypatch.setattr(flow_runs_api, "_refuse_if_run_flow_paused", not_paused)
        answerer = SimpleNamespace(id=user.id, email="admin@filigran.io")
        await flow_runs_api.answer_flow_park(session, answerer, str(run.id), "b: lundi")
        assert run.state["waiting"]["option"] == "B"

        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "completed"
        steps = await _steps(session, run)
        ask_step = next(s_ for s_ in steps if s_.node_id == "ask")
        assert ask_step.output["option"] == "B"
        assert ask_step.output["response"] == "b: lundi"
        fin = [s_ for s_ in steps if s_.node_id == "fin"]
        assert fin and fin[-1].input["value"] == "choix:B"


@pytest.mark.asyncio
async def test_reroute_passes_the_upstream_value_through():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("x1", "util.expression", expression="2 + 3"),
            _node("k1", "util.reroute"),
            _node("fin", "util.cast", value="{{k1.value}}", to="text"),
        ],
        "edges": [
            _edge("t1", "x1"),
            _edge("x1", "k1"),
            _edge("k1", "fin"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
        assert steps["fin"].input["value"] == "5"


@pytest.mark.asyncio
async def test_runner_command_node_dispatches_parks_and_joins(monkeypatch):
    """`action.runner_command`: dispatch rides the runners transport,
    wait mode parks on the durable command row and the resume tick routes the
    terminal status to the ok/fail pins with the structured result readable
    downstream."""
    from app.models.runner import Runner, RunnerCommand
    from app.services import runner_transport

    async with _temp_database() as sessions, sessions() as session:
        # The runner tables are not in _TABLES (they drag the attachment FK
        # chain); a stub file_attachments satisfies RunnerCommand's FK.
        conn = await session.connection()
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS file_attachments (id UUID PRIMARY KEY)")
        )
        await conn.run_sync(
            lambda sync: Runner.__table__.create(sync, checkfirst=True)
        )
        await conn.run_sync(
            lambda sync: RunnerCommand.__table__.create(sync, checkfirst=True)
        )
        await session.commit()

        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        runner = Runner(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Test box",
            platform="linux",
            key_prefix="rnr_test",
            key_hash="x",
            permission_mode="full_control",
            status="online",
            last_seen_at=datetime.now(UTC),
        )
        session.add(runner)
        await session.commit()

        flow.graph = {
            "nodes": [
                _node(
                    "t1",
                    "trigger.manual",
                    parameters=[{"name": "what", "type": "string", "required": True}],
                ),
                _node(
                    "rc",
                    "action.runner_command",
                    runner_id=str(runner.id),
                    command_kind="run_shell",
                    payload={"command": "echo {{t1.payload.what}}"},
                    mode="wait",
                ),
                _node("fin", "util.cast", value="{{rc.result.output}}", to="text"),
                _node("boom", "util.cast", value="{{rc.error}}", to="text"),
            ],
            "edges": [
                _edge("t1", "rc"),
                _edge("rc", "fin", sh="ok", kind="condition_branch"),
                _edge("rc", "boom", sh="fail", kind="condition_branch"),
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        # The transport opens its own app-level session (wrong DB here), so
        # the dispatch is faked at its seam: insert the durable row in the
        # test database and hand back (command_id, status) like the real one.
        dispatched: dict = {}

        async def _fake_dispatch(**kwargs):
            cmd = RunnerCommand(
                id=uuid.uuid4(),
                runner_id=kwargs["runner_id"],
                user_id=kwargs["user_id"],
                flow_run_id=kwargs["flow_run_id"],
                kind=kwargs["kind"],
                payload=kwargs["payload"],
                status="pending",
            )
            session.add(cmd)
            await session.flush()
            dispatched.update(id=cmd.id, payload=kwargs["payload"])
            return str(cmd.id), "pending"

        monkeypatch.setattr(runner_transport, "dispatch_command", _fake_dispatch)

        run, outcome = await _run_to_outcome(
            session, user, flow, payload={"payload": {"what": "hello"}}
        )
        assert outcome.kind == "park"
        assert run.state["waiting"]["kind"] == "runner_command"
        # The payload template rendered before dispatch.
        assert dispatched["payload"] == {"command": "echo hello"}

        # Not terminal yet: a wake re-parks (the safety-net poll).
        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "park"

        # The machine reports back — terminal row, then the join resumes.
        cmd = await session.get(RunnerCommand, dispatched["id"])
        cmd.status = "completed"
        cmd.result = {"output": "hello", "exit_code": 0}
        await session.commit()

        outcome3 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome3.kind == "completed"
        steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
        assert steps["rc"].output["status"] == "completed"
        assert steps["rc"].output["result"]["output"] == "hello"
        # ok pin taken, fail pin never ran.
        assert steps["fin"].output["value"] == "hello"
        assert "boom" not in steps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout_bytes",
    [5 * 1024 * 1024, 900 * 1024],
    ids=["five-mib-result", "result-under-one-mib"],
)
async def test_a_flow_reads_at_most_the_mebibyte_of_result_it_always_did(
    monkeypatch, stdout_bytes
):
    """The runner API takes a result of up to 16 MiB, and a flow reads at
    most 1 MiB of it, as it always has - in the step row and in
    ``{{rc.result.stdout}}`` - while the command row keeps it whole. A smaller result is read as it is."""
    from app.models.runner import Runner, RunnerCommand
    from app.services import runner_transport

    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS file_attachments (id UUID PRIMARY KEY)")
        )
        await conn.run_sync(lambda sync: Runner.__table__.create(sync, checkfirst=True))
        await conn.run_sync(lambda sync: RunnerCommand.__table__.create(sync, checkfirst=True))
        await session.commit()

        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        runner = Runner(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Test box",
            platform="linux",
            key_prefix="rnr_test",
            key_hash="x",
            permission_mode="full_control",
            status="online",
            last_seen_at=datetime.now(UTC),
        )
        session.add(runner)
        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "rc",
                    "action.runner_command",
                    runner_id=str(runner.id),
                    command_kind="run_shell",
                    payload={"command": "cat big.log"},
                    mode="wait",
                ),
                _node("fin", "util.cast", value="{{rc.result.stdout}}", to="text"),
            ],
            "edges": [
                _edge("t1", "rc"),
                _edge("rc", "fin", sh="ok", kind="condition_branch"),
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        dispatched: dict = {}

        async def _fake_dispatch(**kwargs):
            cmd = RunnerCommand(
                id=uuid.uuid4(),
                runner_id=kwargs["runner_id"],
                user_id=kwargs["user_id"],
                flow_run_id=kwargs["flow_run_id"],
                kind=kwargs["kind"],
                payload=kwargs["payload"],
                status="pending",
            )
            session.add(cmd)
            await session.flush()
            dispatched["id"] = cmd.id
            return str(cmd.id), "pending"

        monkeypatch.setattr(runner_transport, "dispatch_command", _fake_dispatch)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "park"

        stdout = "a" * stdout_bytes
        result = {"exit_code": 0, "stdout": stdout, "stderr": ""}
        cmd = await session.get(RunnerCommand, dispatched["id"])
        cmd.status = "completed"
        cmd.result = result
        await session.commit()

        assert (await FlowWalker(session, run, user).run_burst()).kind == "completed"
        await session.commit()
        steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
        # The step row as stored, not the session's copy of it.
        read = (
            await session.scalar(
                select(FlowRunStep.output).where(FlowRunStep.id == steps["rc"].id)
            )
        )["result"]
        # The command's own record keeps the result whole.
        stored = await session.scalar(
            select(RunnerCommand.result).where(RunnerCommand.id == dispatched["id"])
        )
        assert stored == result
        if stdout_bytes < flow_runner.FLOW_RUNNER_RESULT_MAX_BYTES:
            assert json.dumps(read, sort_keys=True) == json.dumps(result, sort_keys=True)
            assert steps["fin"].output["value"] == stdout
            return
        assert flow_runner._json_bytes(read) <= flow_runner.FLOW_RUNNER_RESULT_MAX_BYTES
        assert read[flow_runner.FLOW_RUNNER_RESULT_TRUNCATED_KEY] == {
            "original_bytes": flow_runner._json_bytes(result),
            "limit_bytes": flow_runner.FLOW_RUNNER_RESULT_MAX_BYTES,
        }
        assert read["exit_code"] == 0
        assert read["stdout"].startswith("a" * (900 * 1024))
        assert "[TRUNCATED" in read["stdout"]
        # What the next node reads is the bounded value.
        assert steps["fin"].output["value"] == read["stdout"]


@pytest.mark.asyncio
async def test_runner_command_is_never_dispatched_for_a_run_cancelled_mid_node(monkeypatch):
    """A cancel that commits while the node runs, before its dispatch: the node
    re-reads the run under its row lock, sees it ended and dispatches nothing,
    so no command can outlive the run."""
    import asyncio

    from app.api.v1 import helpers
    from app.models.flow_run import FLOW_RUN_STATUS_CANCELLED
    from app.models.runner import Runner, RunnerCommand
    from app.services import runner_transport

    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS file_attachments (id UUID PRIMARY KEY)")
        )
        await conn.run_sync(lambda sync: Runner.__table__.create(sync, checkfirst=True))
        await conn.run_sync(
            lambda sync: RunnerCommand.__table__.create(sync, checkfirst=True)
        )
        await session.commit()

        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        runner = Runner(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Test box",
            platform="linux",
            key_prefix="rnr_test",
            key_hash="x",
            permission_mode="full_control",
            status="online",
            last_seen_at=datetime.now(UTC),
        )
        session.add(runner)
        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "rc",
                    "action.runner_command",
                    runner_id=str(runner.id),
                    command_kind="run_shell",
                    payload={"command": "echo hi"},
                    mode="wait",
                ),
            ],
            "edges": [_edge("t1", "rc")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        dispatched: list[dict] = []

        async def _fake_dispatch(**kwargs):
            dispatched.append(kwargs)
            return str(uuid.uuid4()), "pending"

        monkeypatch.setattr(runner_transport, "dispatch_command", _fake_dispatch)
        real_group_ids = helpers.get_user_group_ids

        async def _cancelled_meanwhile(db, user_id):
            # The operator's cancel lands here, from another connection.
            async with sessions() as other:
                row = (
                    await other.execute(select(FlowRun).where(FlowRun.flow_id == flow.id))
                ).scalar_one()
                row.status = FLOW_RUN_STATUS_CANCELLED
                await asyncio.wait_for(other.commit(), 10)
            return await real_group_ids(db, user_id)

        monkeypatch.setattr(helpers, "get_user_group_ids", _cancelled_meanwhile)
        await _run_to_outcome(session, user, flow, payload={"payload": {}})
        assert dispatched == []


@pytest.mark.asyncio
async def test_runner_command_vanished_row_takes_the_fail_pin(monkeypatch):
    """Deleting the runner mid-wait cascades the command row away; the node
    must route to Fail with the reason — never kill the whole run."""
    from app.models.runner import Runner, RunnerCommand
    from app.services import runner_transport

    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS file_attachments (id UUID PRIMARY KEY)")
        )
        await conn.run_sync(lambda sync: Runner.__table__.create(sync, checkfirst=True))
        await conn.run_sync(
            lambda sync: RunnerCommand.__table__.create(sync, checkfirst=True)
        )
        await session.commit()

        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        runner = Runner(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Doomed box",
            platform="linux",
            key_prefix="rnr_test",
            key_hash="x",
            permission_mode="full_control",
            status="online",
            last_seen_at=datetime.now(UTC),
        )
        session.add(runner)
        await session.commit()

        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node(
                    "rc",
                    "action.runner_command",
                    runner_id=str(runner.id),
                    command_kind="run_shell",
                    payload={"command": "echo hi"},
                    mode="wait",
                ),
                _node("sos", "util.cast", value="{{rc.error}}", to="text"),
            ],
            "edges": [
                _edge("t1", "rc"),
                _edge("rc", "sos", sh="fail", kind="condition_branch"),
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        cmd_holder: dict = {}

        async def _fake_dispatch(**kwargs):
            cmd = RunnerCommand(
                id=uuid.uuid4(),
                runner_id=kwargs["runner_id"],
                user_id=kwargs["user_id"],
                kind=kwargs["kind"],
                payload=kwargs["payload"],
                status="pending",
            )
            session.add(cmd)
            await session.flush()
            cmd_holder["id"] = cmd.id
            return str(cmd.id), "pending"

        monkeypatch.setattr(runner_transport, "dispatch_command", _fake_dispatch)

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "park"

        # Un-enroll: the FK cascade removes the command row under the park.
        await session.delete(await session.get(RunnerCommand, cmd_holder["id"]))
        await session.commit()

        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "completed"
        steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
        assert steps["rc"].output["status"] == "cancelled"
        assert "vanished" in steps["sos"].output["value"]


@pytest.mark.asyncio
async def test_wait_event_hears_ownerless_platform_emissions():
    """A global event with no owning user (company runner lifecycle) must
    resume any parked waiter — the platform arm of find_matching_event."""
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        et = await _declare_event_type(
            session, user, fields=[{"name": "runner_id", "type": "string"}]
        )
        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node("w1", "util.wait_event", event_type_id=str(et.id)),
            ],
            "edges": [_edge("t1", "w1")],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "park"

        from app.services.flow_events import emit_flow_event

        await emit_flow_event(
            session,
            event_type=et,
            user_id=None,  # ownerless: platform emission
            flow_id=None,
            run_id=None,
            scope="global",
            payload={"runner_id": "r-1"},
            chain_depth=0,
        )
        await session.commit()

        outcome2 = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome2.kind == "completed"
        w1 = next(s for s in await _steps(session, run) if s.node_id == "w1")
        assert w1.output["fields"] == {"runner_id": "r-1"}


@pytest.mark.asyncio
async def test_get_variable_collection_feeds_a_foreach():
    """Get variable (source=collection) publishes the visible runners as an
    iterable list; the Loop walks it and item fields resolve downstream —
    the generic 'foreach over a platform collection' contract."""
    from app.models.runner import Runner, RunnerCommand

    async with _temp_database() as sessions, sessions() as session:
        conn = await session.connection()
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS file_attachments (id UUID PRIMARY KEY)")
        )
        await conn.run_sync(lambda sync: Runner.__table__.create(sync, checkfirst=True))
        await conn.run_sync(
            lambda sync: RunnerCommand.__table__.create(sync, checkfirst=True)
        )
        await session.commit()

        user, flow = await _seed(
            session,
            graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        )
        for name, online in (("alpha", True), ("bravo", False)):
            session.add(
                Runner(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    name=name,
                    platform="linux",
                    key_prefix="rnr_test",
                    key_hash="x",
                    permission_mode="full_control",
                    status="online" if online else "offline",
                    last_seen_at=datetime.now(UTC) if online else None,
                )
            )
        await session.commit()

        flow.graph = {
            "nodes": [
                _node("t1", "trigger.manual"),
                _node("get", "util.get_variable", source="collection", collection="runners"),
                _node("each", "util.loop", items="{{get.value}}", max_iterations=10),
                _node("name", "util.cast", value="{{each.item.name}}", to="text"),
                _node("fin", "util.cast", value="{{get.count}}", to="number"),
            ],
            "edges": [
                _edge("t1", "get"),
                _edge("get", "each"),
                _edge("each", "name", sh="each", kind="condition_branch"),
                _edge("each", "fin", sh="done", kind="condition_branch"),
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        await session.commit()

        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        get_step = next(s_ for s_ in steps if s_.node_id == "get")
        assert get_step.output["count"] == 2
        assert {item["name"] for item in get_step.output["value"]} == {"alpha", "bravo"}
        assert {item["status"] for item in get_step.output["value"]} == {"online", "offline"}
        # One body pass per item, the item's fields readable by path.
        names = [s_.output["value"] for s_ in steps if s_.node_id == "name"]
        assert sorted(names) == ["alpha", "bravo"]
        fin = next(s_ for s_ in steps if s_.node_id == "fin")
        assert fin.output["value"] == 2


@pytest.mark.asyncio
async def test_script_node_injects_platform_variables(monkeypatch):
    """{{var_id.*}} bindings in a Custom tool's code must resolve inside a
    flow exactly as they do on the agent path — the pagination use case binds
    an API token that way, and the loader refuses to run unresolved code."""
    seen: dict = {}

    async def fake_execute_tool_auto(function_code, inputs, language="javascript",
                                     timeout=300, **kw):
        seen["code"] = function_code
        return '{"ok": true}'

    import app.services.custom_tool_executor as executor

    monkeypatch.setattr(executor, "execute_tool_auto", fake_execute_tool_auto)

    from app.models.agent import Agent
    from app.models.chat import Conversation
    from app.models.custom_tool import CustomTool

    async with _temp_database() as sessions:
        async with sessions() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        Agent.__table__,
                        Conversation.__table__,
                        CustomTool.__table__,
                    ],
                )
            )
            await session.commit()
        async with sessions() as session:
            user, flow = await _seed(
                session,
                graph={"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
            )
            var = Variable(
                id=uuid.uuid4(),
                user_id=user.id,
                name="api_token",
                value="tok-secret-123",
                is_secret=False,
            )
            tool = CustomTool(
                id=uuid.uuid4(),
                user_id=user.id,
                name="fetcher",
                language="javascript",
                js_function=(
                    'const auth = "Bearer {{var_id.' + str(var.id) + '}}";\n'
                    'console.log(JSON.stringify({ok: true}));'
                ),
                input_schema={"type": "object", "properties": {}},
            )
            session.add_all([var, tool])
            await session.commit()

            flow.graph = {
                "nodes": [
                    _node("t1", "trigger.manual"),
                    _node("sc", "action.script",
                          custom_tool_id=str(tool.id), arguments={}),
                ],
                "edges": [_edge("t1", "sc")],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }
            await session.commit()

            run, outcome = await _run_to_outcome(session, user, flow)
            assert outcome.kind == "completed"
            assert "tok-secret-123" in seen["code"]
            assert "{{var_id." not in seen["code"]
            steps = {s_.node_id: s_ for s_ in await _steps(session, run)}
            assert steps["sc"].output["fields"] == {"ok": True}


@pytest.mark.asyncio
async def test_merge_inside_a_loop_body_does_not_livelock():
    """A `wait_all` merge fed from inside a loop body used to re-queue
    forever: the queue never emptied, so the drain rule never advanced the
    loop and the run burned MAX_STEPS_PER_RUN. The loop must win when the
    queue holds nothing but waiting merges."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("each", "util.loop", items='["a", "b", "c"]', max_iterations=5),
            _node("body", "util.cast", value="item:{{each.item}}", to="text"),
            _node("join", "util.merge", strategy="wait_all"),
            _node("fin", "util.cast", value="{{join.merged}}", to="text"),
        ],
        "edges": [
            _edge("t1", "each"),
            _edge("each", "body", sh="each"),
            # util.cast branches on ok/fail — there is no `out` handle.
            _edge("body", "join", sh="ok"),
            _edge("each", "join", sh="done"),
            _edge("join", "fin"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        # The loop ran its three passes instead of starving.
        assert len([s_ for s_ in steps if s_.node_id == "body"]) == 3
        # And the merge fired once, carrying every arrival.
        merges = [s_ for s_ in steps if s_.node_id == "join"]
        assert len(merges) < 20, f"merge re-queued {len(merges)}x — livelock"
        fin = next(s_ for s_ in steps if s_.node_id == "fin")
        for item in ("item:a", "item:b", "item:c"):
            assert item in fin.output["value"]


# ── Live journal: two-phase steps, per-node commits, SSE frames ──────────────


@pytest.mark.asyncio
async def test_journal_is_visible_while_a_node_executes(monkeypatch):
    """The walker commits a `running` step BEFORE the node executes, so a
    concurrent reader (the runs panel) sees which node is in flight — the
    n8n-style live trace, not just a post-mortem."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("probe", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [_edge("t1", "probe")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run = await create_flow_run(
            session,
            flow,
            trigger_type="manual",
            trigger_payload={},
            trigger_context={"type": "manual"},
            user_id=user.id,
        )
        await session.commit()

        seen: dict = {}
        orig_dispatch = FlowWalker._dispatch

        async def spying_dispatch(self, node, kind):
            if node["id"] == "probe":
                # A SECOND session: only committed rows are visible here.
                async with sessions() as other:
                    rows = (
                        (
                            await other.execute(
                                select(FlowRunStep)
                                .where(FlowRunStep.flow_run_id == run.id)
                                .order_by(FlowRunStep.seq)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    seen["steps"] = [(r.node_id, r.status) for r in rows]
                    seen["probe_input"] = rows[-1].input if rows else None
                    seen["run_status"] = (
                        await other.execute(
                            select(FlowRun.status).where(FlowRun.id == run.id)
                        )
                    ).scalar_one()
            return await orig_dispatch(self, node, kind)

        monkeypatch.setattr(FlowWalker, "_dispatch", spying_dispatch)
        outcome = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome.kind == "completed"
        # Mid-execution: the trigger was committed `completed`, the probe
        # committed `running` with its rendered input, the run `running`.
        assert seen["steps"] == [("t1", "completed"), ("probe", "running")]
        assert seen["probe_input"] == {"left": "x", "operator": "is_not_empty"}
        assert seen["run_status"] == "running"
        # Post-run: the same row was finalized in place, not duplicated.
        steps = await _steps(session, run)
        probe = [s_ for s_ in steps if s_.node_id == "probe"]
        assert [s_.status for s_ in probe] == ["completed"]
        assert probe[0].completed_at is not None


@pytest.mark.asyncio
async def test_interrupted_in_flight_node_is_failed_and_requeued():
    """A worker crash mid-node leaves a committed `running` row named by
    ``state.in_flight``. The next burst must fail that orphan honestly and
    re-queue its node instead of skipping it (or colliding on seq)."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [_edge("t1", "a")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run = await create_flow_run(
            session,
            flow,
            trigger_type="manual",
            trigger_payload={},
            trigger_context={"type": "manual"},
            user_id=user.id,
        )
        # Simulate the crashed burst's committed leftovers: trigger done,
        # node `a` opened `running` and popped from the queue, then death.
        trigger_step = FlowRunStep(
            flow_run_id=run.id,
            seq=0,
            node_id="t1",
            node_kind="trigger.manual",
            status="completed",
            output={"payload": {}},
        )
        orphan = FlowRunStep(
            flow_run_id=run.id,
            seq=1,
            node_id="a",
            node_kind="condition.if",
            status="running",
        )
        session.add_all([trigger_step, orphan])
        await session.flush()
        run.status = "running"
        run.state = {
            "seq": 2,
            "queue": [],
            "loops": {},
            "merges": {},
            "vars": {},
            "vars_tainted": [],
            "node_counters": {},
            "untrusted": [],
            "started": True,
            "in_flight": {"node": "a", "step_id": str(orphan.id)},
        }
        await session.commit()

        outcome = await FlowWalker(session, run, user).run_burst()
        await session.commit()
        assert outcome.kind == "completed"
        steps = await _steps(session, run)
        a_steps = [s_ for s_ in steps if s_.node_id == "a"]
        assert [s_.status for s_ in a_steps] == ["failed", "completed"]
        assert "Interrupted by a worker restart" in (a_steps[0].error or "")
        # The re-execution took a fresh seq — no unique-key collision.
        assert a_steps[1].seq == 2


@pytest.mark.asyncio
async def test_step_transitions_publish_sse_frames(monkeypatch):
    """Every committed transition emits one `flow_run_update` frame — the
    accelerator the frontend uses to refetch the trace it renders."""
    frames: list[tuple] = []

    async def fake_publish(run, *, node_id=None, node_status=None):
        frames.append((run.status, node_id, node_status))

    monkeypatch.setattr(flow_runner, "publish_flow_run_update", fake_publish)

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [_edge("t1", "a")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        _run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed"
    assert ("running", "t1", "completed") in frames
    assert ("running", "a", "running") in frames
    assert ("running", "a", "completed") in frames
    assert frames[-1] == ("completed", None, None)


# ── Cross-flow run listing (Operations → Executions) ─────────────────────────


@pytest.mark.asyncio
async def test_global_run_listing_filters_and_visibility():
    """`GET /flows/runs/all` — visibility snapshot in SQL, status vocabulary
    mapping, ownership + search filters, and the flow name on each row."""
    from app.api.v1.flow_runs import list_all_flow_runs

    async def _list(session, user, status_filter=None, search=None, ownership=None):
        # Direct call: FastAPI's Query() defaults only resolve over HTTP,
        # so every parameter is passed explicitly.
        return await list_all_flow_runs(
            limit=50,
            offset=0,
            status_filter=status_filter,
            search=search,
            ownership=ownership,
            db=session,
            user=user,
        )

    graph = {
        "nodes": [_node("t1", "trigger.manual")],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        owner, private_flow = await _seed(session, graph=graph, name="Private flow")
        stranger = User(
            id=uuid.uuid4(),
            email=f"stranger-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Stranger",
            hashed_password="x",
        )
        session.add(stranger)
        await session.flush()
        company_flow = Flow(
            id=uuid.uuid4(),
            user_id=owner.id,
            name="Company enrichment",
            graph=graph,
            is_company_managed=True,
        )
        session.add(company_flow)
        await session.flush()

        private_run = await create_flow_run(
            session,
            private_flow,
            trigger_type="manual",
            trigger_payload={},
            trigger_context={"type": "manual"},
            user_id=owner.id,
        )
        company_run = await create_flow_run(
            session,
            company_flow,
            trigger_type="schedule",
            trigger_payload={},
            trigger_context={"type": "schedule"},
            user_id=owner.id,
        )
        await session.flush()
        private_run.status = "awaiting"
        company_run.status = "completed"
        await session.commit()

        # The owner sees both; rows carry their flow's name.
        page = await _list(session, owner)
        assert page.total == 2
        assert {r.flow_name for r in page.runs} == {
            "Private flow",
            "Company enrichment",
        }

        # A stranger sees only the company-managed run.
        page = await _list(session, stranger)
        assert [r.flow_name for r in page.runs] == ["Company enrichment"]

        # Executions-tab vocabulary: awaiting_human maps onto `awaiting`.
        page = await _list(session, owner, status_filter="awaiting_human")
        assert [r.id for r in page.runs] == [str(private_run.id)]

        # Ownership: the stranger started nothing, everything is "shared".
        assert (
            await _list(session, stranger, ownership="mine")
        ).total == 0
        assert (
            await _list(session, stranger, ownership="shared")
        ).total == 1

        # Search matches the flow name.
        page = await _list(session, owner, search="enrich")
        assert [r.flow_name for r in page.runs] == ["Company enrichment"]

        # A typo'd status must read as "bad filter" (422), not as "no runs".
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            await _list(session, owner, status_filter="complete")
        assert excinfo.value.status_code == 422

        # Same for ownership: a typo must not widen to everybody's runs.
        with pytest.raises(HTTPException) as excinfo:
            await _list(session, owner, ownership="min")
        assert excinfo.value.status_code == 422
        # "all" and an omitted filter answer the same question.
        assert (await _list(session, owner, ownership="all")).total == (
            await _list(session, owner)
        ).total


@pytest.mark.asyncio
async def test_fail_recovery_closes_the_dangling_running_step(monkeypatch):
    """`_fail`'s rollback-recovery branch discards any uncommitted step
    finalization (e.g. `_route_error` closing the failing node's row) — it
    must re-close still-`running` rows on the fresh transaction, or the
    journal shows a node running forever inside a failed run."""

    async def exploding_notify(self):
        raise RuntimeError("boom during the terminal write")

    monkeypatch.setattr(FlowWalker, "_notify_parent", exploding_notify)

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            # An unexecutable kind: _dispatch returns an error, and with no
            # `error` port wired the run takes the _route_error → _fail path.
            _node("boom", "does.not.exist"),
        ],
        "edges": [_edge("t1", "boom")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "failed"
        assert run.status == "failed"
        steps = await _steps(session, run)
        boom = next(s_ for s_ in steps if s_.node_id == "boom")
        assert boom.status == "failed", "the dangling running row must be closed"
        assert boom.completed_at is not None


@pytest.mark.asyncio
async def test_close_running_steps_closes_stranded_rows():
    """An out-of-band termination (operator cancel mid-node, dead-letter)
    must close committed `running` rows — the terminal-status guard stops
    any later burst, so the orphan sweep can never reach them."""
    from app.services.flow_runner import close_running_steps

    graph = {
        "nodes": [_node("t1", "trigger.manual")],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=graph)
        run = await create_flow_run(
            session,
            flow,
            trigger_type="manual",
            trigger_payload={},
            trigger_context={"type": "manual"},
            user_id=user.id,
        )
        done = FlowRunStep(
            flow_run_id=run.id,
            seq=0,
            node_id="t1",
            node_kind="trigger.manual",
            status="completed",
        )
        stranded = FlowRunStep(
            flow_run_id=run.id,
            seq=1,
            node_id="mid",
            node_kind="action.script",
            status="running",
        )
        session.add_all([done, stranded])
        await session.commit()

        await close_running_steps(session, run.id, error="Cancelled mid-node.")
        await session.commit()

        steps = await _steps(session, run)
        by_node = {s_.node_id: s_ for s_ in steps}
        assert by_node["mid"].status == "failed"
        assert by_node["mid"].error == "Cancelled mid-node."
        assert by_node["mid"].completed_at is not None
        # Finished rows are untouched.
        assert by_node["t1"].status == "completed"


# ── LLM-mode candidate resolution ────────────────────────────────────
#
# `resolve_callable_definitions` decides what an LLM-mode Call-tool node may
# offer a model, and the pick runs in the same scope. That scope is the only
# thing standing between a user and offering, then invoking, a tool outside
# their reach. It is a security boundary, so it is tested here rather than
# asserted in a comment.


async def _seed_user(session, *, email=None, is_admin=False):
    # `is_admin` is a property over `role` — set the column it reads.
    user = User(
        id=uuid.uuid4(),
        email=email or f"cand-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Candidate Test",
        hashed_password="x",
        role="admin" if is_admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_custom_tool(session, *, name, user_id, is_company_managed=False):
    tool = CustomTool(
        id=uuid.uuid4(),
        user_id=None if is_company_managed else user_id,
        is_company_managed=is_company_managed,
        name=name,
        description=f"{name} description",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        language="javascript",
        js_function="return 1;",
    )
    session.add(tool)
    await session.flush()
    return tool


async def test_another_users_custom_tool_is_never_offered():
    """The boundary: a tool you cannot see is not a tool you may offer."""
    from app.services.flow_runner import resolve_callable_definitions

    async with _temp_database() as session_factory, session_factory() as session:
        mine = await _seed_user(session)
        theirs = await _seed_user(session)
        await _seed_custom_tool(session, name="my_tool", user_id=mine.id)
        await _seed_custom_tool(session, name="their_tool", user_id=theirs.id)
        await session.commit()

        definitions, unresolved = await resolve_callable_definitions(
            session, mine, ["my_tool", "their_tool"]
        )

        assert [d.name for d in definitions] == ["my_tool"]
        # Reported, not silently dropped: a node whose candidate vanished
        # must say so instead of quietly narrowing what the model may call.
        assert unresolved == ["their_tool"]


async def test_an_admin_sees_every_custom_tool():
    from app.services.flow_runner import resolve_callable_definitions

    async with _temp_database() as session_factory, session_factory() as session:
        admin = await _seed_user(session, is_admin=True)
        other = await _seed_user(session)
        await _seed_custom_tool(session, name="their_tool", user_id=other.id)
        await session.commit()

        definitions, unresolved = await resolve_callable_definitions(
            session, admin, ["their_tool"]
        )

        assert [d.name for d in definitions] == ["their_tool"]
        assert unresolved == []


async def test_a_company_managed_custom_tool_is_offered_to_everyone():
    from app.services.flow_runner import resolve_callable_definitions

    async with _temp_database() as session_factory, session_factory() as session:
        author = await _seed_user(session)
        reader = await _seed_user(session)
        await _seed_custom_tool(
            session,
            name="shared_tool",
            user_id=author.id,
            is_company_managed=True,
        )
        await session.commit()

        definitions, _ = await resolve_callable_definitions(
            session, reader, ["shared_tool"]
        )
        assert [d.name for d in definitions] == ["shared_tool"]


async def test_a_builtin_wins_a_name_collision_with_a_custom_tool():
    """Resolution ORDER is load-bearing.

    ``_call_tool_by_name`` tries built-ins first, so the definition offered to
    the model must be the built-in's too — otherwise the model fills the custom
    tool's schema and the runner invokes the built-in with it.
    """
    from app.services.flow_runner import (
        flow_builtin_tools,
        resolve_callable_definitions,
    )

    builtin_name = flow_builtin_tools()[0][0].name

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await _seed_custom_tool(session, name=builtin_name, user_id=user.id)
        await session.commit()

        definitions, _ = await resolve_callable_definitions(
            session, user, [builtin_name]
        )

        assert len(definitions) == 1
        # Built-in sources are namespaced (`builtin:<family>`); what matters is
        # that the CUSTOM tool's schema did not win the name.
        assert definitions[0].source.startswith("builtin")


async def test_candidate_order_follows_the_authors_declaration():
    """The declared order is the order the model is shown them in."""
    from app.services.flow_runner import (
        flow_builtin_tools,
        resolve_callable_definitions,
    )

    builtin_name = flow_builtin_tools()[0][0].name

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await _seed_custom_tool(session, name="zeta_tool", user_id=user.id)
        await session.commit()

        definitions, _ = await resolve_callable_definitions(
            session, user, ["zeta_tool", builtin_name]
        )
        assert [d.name for d in definitions] == ["zeta_tool", builtin_name]


# ── What a flow wires runs for whoever runs it ───────────────────────
#
# The custom tools a flow's configuration wires - through a capability node,
# `action.script` or `action.call_tool` - were chosen by someone allowed to
# write that flow, and a run re-checks none of them against the person running
# it. A company-managed flow (admins write it, any user runs it by hand) wiring
# an admin's private tool runs that tool for every runner.


def _record_sandbox(monkeypatch) -> list[str]:
    """Fake the sandbox; the code of every tool that reached it, in order."""
    import app.services.custom_tool_executor as executor

    ran: list[str] = []

    async def fake_execute_tool_auto(
        function_code, inputs, language="javascript", timeout=300, **kw
    ):
        ran.append(function_code)
        return '{"ok": true}'

    monkeypatch.setattr(executor, "execute_tool_auto", fake_execute_tool_auto)
    return ran


async def _seed_company_flow(session, graph):
    flow = Flow(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        name="Company flow",
        graph=graph,
    )
    session.add(flow)
    await session.commit()
    return flow


@pytest.mark.parametrize("kind", ["action.script", "action.call_tool"])
async def test_a_company_managed_flow_runs_an_admin_private_tool_for_any_runner(
    monkeypatch, kind
):
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        admin = await _seed_user(session, is_admin=True)
        runner = await _seed_user(session)
        tool = await _seed_custom_tool(session, name="admin_lookup", user_id=admin.id)
        tool.js_function = "return 'admin';"
        config = (
            {"custom_tool_id": str(tool.id), "arguments": {"query": "x"}}
            if kind == "action.script"
            else {"tool_name": "admin_lookup", "arguments": {"query": "x"}}
        )
        flow = await _seed_company_flow(
            session,
            {
                "nodes": [_node("t1", "trigger.manual"), _node("act", kind, **config)],
                "edges": [_edge("t1", "act")],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )

        run, outcome = await _run_to_outcome(session, runner, flow)

        assert outcome.kind == "completed", run.error
        assert ran == ["return 'admin';"]


# ── What a Call-tool node's tool name designates ─────────────────────
#
# A name is not an id: it is resolved at run time, among the custom tools the
# flow's writers could pick (its owner's reach, every tool for a
# company-managed flow), the same way whoever runs the flow.


def _call_tool_graph(tool_name):
    return {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("act", "action.call_tool", tool_name=tool_name, arguments={"query": "x"}),
        ],
        "edges": [_edge("t1", "act")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


async def _seed_owned_flow(session, owner, graph, **fields):
    flow = Flow(id=uuid.uuid4(), user_id=owner.id, name="Tool flow", graph=graph, **fields)
    session.add(flow)
    await session.commit()
    return flow


async def _tool(session, *, name, owner, code, **fields):
    """A custom tool running *code*; company-managed when *owner* is None."""
    tool = await _seed_custom_tool(
        session,
        name=name,
        user_id=owner.id if owner else None,
        is_company_managed=owner is None,
    )
    tool.js_function = code
    for key, value in fields.items():
        setattr(tool, key, value)
    await session.commit()
    return tool


async def _call_tool_error(session, run):
    step = next(s_ for s_ in await _steps(session, run) if s_.node_id == "act")
    assert step.status == "failed"
    return step.error


async def test_a_call_tool_node_never_runs_another_users_private_tool(monkeypatch):
    """The name of a tool its owner cannot use fails exactly like a name no
    tool carries."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        other = await _seed_user(session)
        await _tool(session, name="their_lookup", owner=other, code="return 'theirs';")
        hidden = await _seed_owned_flow(session, author, _call_tool_graph("their_lookup"))
        unknown = await _seed_owned_flow(session, author, _call_tool_graph("nobody_has_it"))

        hidden_run, hidden_outcome = await _run_to_outcome(session, author, hidden)
        unknown_run, _ = await _run_to_outcome(session, author, unknown)

        assert hidden_outcome.kind == "failed"
        assert ran == []
        assert await _call_tool_error(session, hidden_run) == (
            "No tool named `their_lookup` is available to this flow."
        )
        assert await _call_tool_error(session, unknown_run) == (
            "No tool named `nobody_has_it` is available to this flow."
        )


@pytest.mark.parametrize("reach", ["own", "group", "user", "company"])
async def test_a_call_tool_node_runs_a_tool_its_owner_can_use(monkeypatch, reach):
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        colleague = await _seed_user(session)
        fields = {}
        if reach == "group":
            team = Group(name=f"team-{uuid.uuid4().hex[:8]}", created_by=colleague.id)
            session.add(team)
            await session.flush()
            session.add(GroupMember(group_id=team.id, user_id=author.id))
            fields = {"shared_group_ids": [str(team.id)]}
        elif reach == "user":
            fields = {"shared_user_ids": [str(author.id)]}
        owner = {"own": author, "company": None}.get(reach, colleague)
        await _tool(session, name="lookup", owner=owner, code=f"return '{reach}';", **fields)
        flow = await _seed_owned_flow(session, author, _call_tool_graph("lookup"))

        run, outcome = await _run_to_outcome(session, author, flow)

        assert outcome.kind == "completed", run.error
        assert ran == [f"return '{reach}';"]


async def test_a_shared_flow_runs_its_owners_tool_whoever_runs_it(monkeypatch):
    """What the flow stores runs as stored: the runner's own tool of the same
    name does not replace the owner's."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        runner = await _seed_user(session)
        await _tool(session, name="lookup", owner=runner, code="return 'runner';")
        await _tool(session, name="lookup", owner=owner, code="return 'owner';")
        flow = await _seed_owned_flow(
            session, owner, _call_tool_graph("lookup"), shared_user_ids=[str(runner.id)]
        )

        run, outcome = await _run_to_outcome(session, runner, flow)

        assert outcome.kind == "completed", run.error
        assert ran == ["return 'owner';"]


async def test_a_tool_name_takes_the_owners_then_a_company_managed_then_an_admins(monkeypatch):
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        admin = await _seed_user(session, is_admin=True)
        colleague = await _seed_user(session)
        shared = {"shared_user_ids": [str(author.id)]}
        await _tool(session, name="lookup", owner=colleague, code="return 'colleague';", **shared)
        admins = await _tool(session, name="lookup", owner=admin, code="return 'admin';", **shared)
        company = await _tool(session, name="lookup", owner=None, code="return 'company';")
        own = await _tool(session, name="lookup", owner=author, code="return 'own';")
        flow = await _seed_owned_flow(session, author, _call_tool_graph("lookup"))

        for retired in (own, company, admins, None):
            run, outcome = await _run_to_outcome(session, author, flow)
            assert outcome.kind == "completed", run.error
            if retired is not None:
                retired.enabled = False
                await session.commit()

        assert ran == [
            "return 'own';",
            "return 'company';",
            "return 'admin';",
            "return 'colleague';",
        ]


async def test_two_tools_of_a_name_in_the_deciding_tier_are_refused(monkeypatch):
    """Colleagues fill the last tier, so two of their tools of one name are
    never settled by an order; a tool of the owner's own settles it."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        for index in range(2):
            colleague = await _seed_user(session)
            await _tool(
                session,
                name="lookup",
                owner=colleague,
                code=f"return 'colleague {index}';",
                shared_user_ids=[str(author.id)],
            )
        flow = await _seed_owned_flow(session, author, _call_tool_graph("lookup"))

        run, outcome = await _run_to_outcome(session, author, flow)

        assert outcome.kind == "failed"
        assert ran == []
        assert await _call_tool_error(session, run) == (
            "More than one custom tool this flow can use is named `lookup`; "
            "give them distinct names so it designates one."
        )

        await _tool(session, name="lookup", owner=author, code="return 'own';")
        run, outcome = await _run_to_outcome(session, author, flow)

        assert outcome.kind == "completed", run.error
        assert ran == ["return 'own';"]


async def test_a_members_tool_neither_takes_over_nor_blocks_a_company_flows_tool(monkeypatch):
    """A company-managed flow is readable by every user, its tool names too:
    one of theirs under the same name changes nothing."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        admin = await _seed_user(session, is_admin=True)
        member = await _seed_user(session)
        await _tool(session, name="admin_lookup", owner=member, code="return 'member';")
        await _tool(session, name="admin_lookup", owner=admin, code="return 'admin';")
        flow = await _seed_company_flow(session, _call_tool_graph("admin_lookup"))

        run, outcome = await _run_to_outcome(session, member, flow)

        assert outcome.kind == "completed", run.error
        assert ran == ["return 'admin';"]


async def test_llm_mode_runs_the_tool_it_offered(monkeypatch):
    """The model fills the schema of the tool it was offered, and that tool,
    not another of the same name, is the one that runs."""
    from app.services import flow_tool_selector
    from app.services.flow_tool_selector import ToolSelection

    ran = _record_sandbox(monkeypatch)
    offered: list[str] = []

    async def fake_select_tool(db, *, definitions, **kwargs):
        offered.extend(d.entity_id or d.name for d in definitions)
        return ToolSelection(tool_name="lookup", arguments={"query": "x"}, attempts=1)

    monkeypatch.setattr(flow_tool_selector, "select_tool", fake_select_tool)
    async with _temp_database() as sessions, sessions() as session:
        runner = await _seed_user(session)
        stranger = await _seed_user(session)
        colleague = await _seed_user(session)
        await _tool(session, name="lookup", owner=stranger, code="return 'stranger';")
        await _tool(
            session,
            name="lookup",
            owner=colleague,
            code="return 'colleague';",
            shared_user_ids=[str(runner.id)],
        )
        own = await _tool(session, name="lookup", owner=runner, code="return 'own';")
        flow = await _seed_owned_flow(
            session,
            runner,
            {
                "nodes": [
                    _node("t1", "trigger.manual"),
                    _node("act", "action.call_tool", objective="Look it up", candidate_tools=["lookup"]),
                    _node("llm", "capability.llm"),
                ],
                "edges": [
                    _edge("t1", "act"),
                    _edge("llm", "act", sh="provides", th="llm", kind="capability"),
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )

        run, outcome = await _run_to_outcome(session, runner, flow)

        assert outcome.kind == "completed", run.error
        assert offered == [str(own.id)]
        assert ran == ["return 'own';"]


def _llm_call_tool_graph(candidates):
    return {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("act", "action.call_tool", objective="Look it up", candidate_tools=candidates),
            _node("llm", "capability.llm"),
        ],
        "edges": [
            _edge("t1", "act"),
            _edge("llm", "act", sh="provides", th="llm", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def _fake_selector(monkeypatch, offered, *, during=None):
    """The model picks the first offered tool; *during* runs while it thinks."""
    from app.services import flow_tool_selector
    from app.services.flow_tool_selector import ToolSelection

    async def fake_select_tool(db, *, definitions, **kwargs):
        offered.extend(d.entity_id or d.name for d in definitions)
        if during is not None:
            await during()
        return ToolSelection(tool_name=definitions[0].name, arguments={"query": "x"}, attempts=1)

    monkeypatch.setattr(flow_tool_selector, "select_tool", fake_select_tool)


async def test_llm_mode_never_offers_the_runners_own_tool_outside_the_flows_reach(monkeypatch):
    """A flow's writer names a tool of the flow's reach: a runner's private
    tool of that name is not what the name designates, so a writer cannot
    make it run and read its output in the run."""
    ran = _record_sandbox(monkeypatch)
    offered: list[str] = []
    _fake_selector(monkeypatch, offered)
    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        runner = await _seed_user(session)
        await _tool(session, name="their_secret", owner=runner, code="return 'runner';")
        flow = await _seed_owned_flow(
            session, owner, _llm_call_tool_graph(["their_secret"]), shared_user_ids=[str(runner.id)]
        )

        run, outcome = await _run_to_outcome(session, runner, flow)

        assert outcome.kind == "failed"
        assert offered == []
        assert ran == []
        assert "`their_secret`" in await _call_tool_error(session, run)


async def test_llm_mode_offers_the_flows_tool_only_to_a_runner_who_can_use_it(monkeypatch):
    ran = _record_sandbox(monkeypatch)
    offered: list[str] = []
    _fake_selector(monkeypatch, offered)
    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        runner = await _seed_user(session)
        await _tool(session, name="lookup", owner=runner, code="return 'runner';")
        mine = await _tool(session, name="lookup", owner=owner, code="return 'owner';")
        flow = await _seed_owned_flow(
            session, owner, _llm_call_tool_graph(["lookup"]), shared_user_ids=[str(runner.id)]
        )

        # The owner's own tool, which the runner cannot use: offered to nobody
        # but its owner, and the runner's tool of the same name is not it.
        run, outcome = await _run_to_outcome(session, runner, flow)
        assert outcome.kind == "failed"
        assert offered == [] and ran == []

        run, outcome = await _run_to_outcome(session, owner, flow)
        assert outcome.kind == "completed", run.error
        assert offered == [str(mine.id)]

        mine.shared_user_ids = [str(runner.id)]
        await session.commit()
        run, outcome = await _run_to_outcome(session, runner, flow)
        assert outcome.kind == "completed", run.error
        assert offered == [str(mine.id), str(mine.id)]
        assert ran == ["return 'owner';", "return 'owner';"]


async def test_llm_mode_runs_the_offered_tool_whatever_changes_during_the_call(monkeypatch):
    """The pick runs by id: a same-named tool created while the model thinks
    does not replace it, and one disabled meanwhile does not run."""
    ran = _record_sandbox(monkeypatch)
    offered: list[str] = []
    state: dict = {}

    async def during():
        await state["change"]()

    _fake_selector(monkeypatch, offered, during=during)
    async with _temp_database() as sessions, sessions() as session:
        runner = await _seed_user(session)
        company = await _tool(session, name="lookup", owner=None, code="return 'company';")
        flow = await _seed_owned_flow(session, runner, _llm_call_tool_graph(["lookup"]))

        async def create_own():
            await _tool(session, name="lookup", owner=runner, code="return 'own';")

        state["change"] = create_own
        run, outcome = await _run_to_outcome(session, runner, flow)
        assert outcome.kind == "completed", run.error
        assert offered == [str(company.id)]
        assert ran == ["return 'company';"]

    async with _temp_database() as sessions, sessions() as session:
        runner = await _seed_user(session)
        own = await _tool(session, name="lookup", owner=runner, code="return 'own';")
        flow = await _seed_owned_flow(session, runner, _llm_call_tool_graph(["lookup"]))

        async def disable():
            own.enabled = False
            await session.commit()

        state["change"] = disable
        ran.clear()
        run, outcome = await _run_to_outcome(session, runner, flow)
        assert outcome.kind == "failed"
        assert ran == []
        assert await _call_tool_error(session, run) == (
            "`lookup` was chosen but is no longer callable."
        )


async def test_a_designated_tool_that_does_not_load_fails_the_step(monkeypatch):
    """Its name designates it, so neither the loader's "not found" text nor a
    same-named integration tool stands in for its output."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        await _tool(session, name="lookup", owner=author, code="")
        flow = await _seed_owned_flow(session, author, _call_tool_graph("lookup"))

        run, outcome = await _run_to_outcome(session, author, flow)

        assert outcome.kind == "failed"
        assert ran == []
        assert await _call_tool_error(session, run) == (
            "The custom tool `lookup` could not be loaded; check that it has code to run."
        )


class _StopTurnError(Exception):
    """Raised by the tool loader stub: everything asserted is built by then."""


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def decr(self, key):
        return 0

    async def expire(self, key, ttl):
        return True

    async def delete(self, key):
        return 1


async def test_a_company_managed_flow_hands_its_agent_an_admin_private_tool(monkeypatch):
    """A capability node wires an admin's private custom tool into an agent
    node: a non-admin's run reaches the agent's REAL turn, which loads the
    wired tool beside the agent's own. The turn stops at the tool loader."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core.config import settings

    # Custom tools are off by default outside Copilot mode.
    monkeypatch.setattr(settings, "disable_custom_tools", False)
    loader = AsyncMock(side_effect=_StopTurnError)
    llm = (SimpleNamespace(provider_name="fake"), "fake-model")
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(
            return_value=(None, False)
        ),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=llm),
        "app.services.builtin_tools.load_custom_and_mcp_tools": loader,
    }.items():
        monkeypatch.setattr(target, value)

    async with _temp_database() as sessions, sessions() as session:
        admin = await _seed_user(session, is_admin=True)
        runner = await _seed_user(session)
        wired = await _seed_custom_tool(session, name="admin_lookup", user_id=admin.id)
        own = await _seed_custom_tool(
            session, name="agent_tool", user_id=admin.id, is_company_managed=True
        )
        agent = Agent(
            id=uuid.uuid4(),
            user_id=None,
            is_company_managed=True,
            name="Company agent",
            slug=f"company-agent-{uuid.uuid4().hex[:8]}",
            enabled=True,
            custom_tool_ids=[str(own.id)],
            integration_bindings={},
            mcp_server_ids=[],
            knowledge_base_ids=[],
            builtin_tool_ids=[],
            handover_agent_ids=[],
            output_format="markdown",
            memory_short_term_enabled=False,
            memory_medium_term_enabled=False,
            memory_persistent_enabled=False,
            memory_user_enabled=False,
        )
        session.add(agent)
        await session.flush()
        flow = await _seed_company_flow(
            session,
            {
                "nodes": [
                    _node("t1", "trigger.manual"),
                    _node("ag", "agent", agent_id=str(agent.id), prompt="Go."),
                    _node("cap", "capability.tool", tool_ids=[str(wired.id)]),
                ],
                "edges": [
                    _edge("t1", "ag"),
                    _edge("cap", "ag", sh="provides", th="tools", kind="capability"),
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )

        await _run_to_outcome(session, runner, flow)

        loader.assert_awaited_once()
        assert loader.await_args.kwargs["user_id"] == str(runner.id)
        assert list(loader.await_args.kwargs["custom_tool_ids"]) == [
            str(own.id),
            str(wired.id),
        ]


# ── Save-time candidate validation ───────────────────────────────────


def _call_tool_node(candidates):
    return _node("c1", "action.call_tool", candidate_tools=candidates)


async def test_check_tool_references_warns_on_a_stale_candidate():
    from app.services.flow_references import check_tool_references

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await _seed_custom_tool(session, name="live_tool", user_id=user.id)
        await session.commit()

        issues = await check_tool_references(
            session,
            user,
            graph={
                "nodes": [_call_tool_node(["live_tool", "gone_tool"])],
                "edges": [],
            },
        )

        assert len(issues) == 1
        assert issues[0]["code"] == "call_tool.candidate_missing"
        assert issues[0]["severity"] == "warning"
        assert issues[0]["field"] == "candidate_tools"
        assert "`gone_tool`" in issues[0]["message"]
        # Some candidates survive: the message says they are left out.
        assert "left out" in issues[0]["message"]


async def test_check_tool_references_flags_a_node_with_nothing_left_to_call():
    from app.services.flow_references import check_tool_references

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await session.commit()

        issues = await check_tool_references(
            session,
            user,
            graph={
                "nodes": [_call_tool_node(["gone_a", "gone_b"])],
                "edges": [],
            },
        )

        assert len(issues) == 1
        assert "nothing to call" in issues[0]["message"]


async def test_check_tool_references_is_quiet_when_every_candidate_resolves():
    from app.services.flow_references import check_tool_references

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await _seed_custom_tool(session, name="live_tool", user_id=user.id)
        await session.commit()

        issues = await check_tool_references(
            session,
            user,
            graph={"nodes": [_call_tool_node(["live_tool"])], "edges": []},
        )
        assert issues == []


async def test_check_tool_references_short_circuits_without_a_call_tool_node():
    """It runs on EVERY flow save, including saves of flows that have no
    Call-tool node — those must not even reach the database."""
    from app.services.flow_references import check_tool_references

    class _ExplodingSession:
        async def execute(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("no query should be issued")

    user = User(
        id=uuid.uuid4(),
        email="nodb@test.local",
        full_name="No DB",
        hashed_password="x",
    )
    issues = await check_tool_references(
        _ExplodingSession(),
        user,
        graph={"nodes": [_node("t1", "trigger.manual")], "edges": []},
    )
    assert issues == []


async def test_check_tool_references_warns_past_the_candidate_cap():
    from app.services.flow_references import check_tool_references
    from app.services.flow_tool_selector import MAX_CANDIDATE_TOOLS

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        names = [f"tool_{i}" for i in range(MAX_CANDIDATE_TOOLS + 1)]
        for name in names:
            await _seed_custom_tool(session, name=name, user_id=user.id)
        await session.commit()

        issues = await check_tool_references(
            session,
            user,
            graph={"nodes": [_call_tool_node(names)], "edges": []},
        )

        # Every candidate resolves, so the ONLY issue is the cap — which
        # the runner's own check would not have caught until the node ran.
        assert [i["code"] for i in issues] == ["call_tool.too_many_candidates"]


# ── Ids a graph write adds ───────────────────────────────────────────
#
# A run reads every id a node names as stored, so the write refuses an
# id its writer could never have picked, as it does for an integration.


async def test_a_graph_write_refuses_an_id_its_writer_cannot_see():
    from app.services.flow_references import check_source_references

    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        other = await _seed_user(session)
        admin = await _seed_user(session, is_admin=True)
        theirs = await _seed_custom_tool(session, name="their_tool", user_id=other.id)
        mine = await _seed_custom_tool(session, name="my_tool", user_id=owner.id)
        company = await _seed_custom_tool(session, name="company_tool", user_id=None, is_company_managed=True)
        their_agent = Agent(id=uuid.uuid4(), user_id=other.id, name="Theirs")
        my_agent = Agent(id=uuid.uuid4(), user_id=owner.id, name="Mine")
        session.add_all([their_agent, my_agent])
        await session.commit()
        dangling = str(uuid.uuid4())
        graph = {
            "nodes": [
                _node("s1", "action.script", custom_tool_id=str(theirs.id)),
                _node("s2", "action.script", custom_tool_id=str(mine.id)),
                _node("s3", "action.script", custom_tool_id=str(company.id)),
                _node("s4", "action.script", custom_tool_id=dangling),
                _node("a1", "agent", agent_id=str(their_agent.id)),
                _node("a2", "agent", agent_id=str(my_agent.id)),
                _node("c1", "capability.tool", tool_ids=[str(mine.id), str(theirs.id)]),
            ],
            "edges": [],
        }

        async def refused(writer, *, previous=None, flow_owner_id=None):
            issues = await check_source_references(
                session, writer, graph=graph, previous_graph=previous, flow_owner_id=flow_owner_id
            )
            assert {i["code"] for i in issues} <= {"reference.not_visible"}
            return [(i["node_id"], i["field"]) for i in issues]

        # Its creator or owner: someone else's private rows are refused, an id
        # naming no row saves as before (an import from another install).
        assert await refused(owner) == [("s1", "custom_tool_id"), ("a1", "agent_id"), ("c1", "tool_ids")]
        assert await refused(owner, flow_owner_id=owner.id) == [
            ("s1", "custom_tool_id"), ("a1", "agent_id"), ("c1", "tool_ids"),
        ]
        # A co-editor: a missing id is refused like a hidden one.
        assert await refused(other, flow_owner_id=owner.id) == [
            ("s2", "custom_tool_id"), ("s4", "custom_tool_id"), ("a2", "agent_id"), ("c1", "tool_ids"),
        ]
        # What the flow already held is never re-litigated; admins are not checked.
        assert await refused(owner, previous=graph) == []
        assert await refused(admin, flow_owner_id=owner.id) == []

        # A baseline is per table: an id first saved as a missing agent is
        # still checked once it names a custom tool.
        laundered = {"nodes": [_node("s1", "action.script", custom_tool_id=str(theirs.id))], "edges": []}
        disguised = {"nodes": [_node("a1", "agent", agent_id=str(theirs.id))], "edges": []}
        issues = await check_source_references(
            session, owner, graph=laundered, previous_graph=disguised, flow_owner_id=owner.id
        )
        assert [(i["node_id"], i["field"]) for i in issues] == [("s1", "custom_tool_id")]


async def test_a_value_node_may_only_name_a_variable_its_writer_can_see():
    """Its picker is not a catalog field, and a run publishes the variable
    resolved for whoever runs the flow."""
    from app.services.flow_references import check_source_references

    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        other = await _seed_user(session)
        theirs = Variable(user_id=other.id, name="their_secret", value="s3cr3t", is_secret=True)
        mine = Variable(user_id=owner.id, name="mine", value="v")
        session.add_all([theirs, mine])
        await session.commit()
        graph = {
            "nodes": [
                _node("v1", "util.primitive", variable_id=str(theirs.id)),
                _node("v2", "util.primitive", variable_id=str(mine.id)),
            ],
            "edges": [],
        }

        issues = await check_source_references(
            session, owner, graph=graph, previous_graph=None, flow_owner_id=owner.id
        )

        assert [(i["node_id"], i["field"]) for i in issues] == [("v1", "variable_id")]


async def test_a_variable_placeholder_is_held_to_the_variables_its_writer_can_see():
    """A ``{{var_id.UUID}}`` in any config string resolves for whoever runs the
    flow, secrets included: a co-editor may not add the owner's, a writer
    keeps their own secret, and a copy drops the placeholder it cannot use."""
    from app.services.flow_references import check_source_references, strip_unseen_references

    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        editor = await _seed_user(session)
        secret = Variable(user_id=owner.id, name="owner_secret", value="s3cr3t", is_secret=True)
        session.add(secret)
        await session.commit()
        placeholder = f"{{{{var_id.{secret.id}}}}}"
        graph = {
            "nodes": [_node("s1", "util.stop", result=f"token={placeholder}", nested={"a": [placeholder]})],
            "edges": [],
        }

        async def refused(writer):
            issues = await check_source_references(
                session, writer, graph=graph, previous_graph=None, flow_owner_id=owner.id
            )
            return sorted((i["node_id"], i["field"]) for i in issues)

        assert await refused(editor) == [("s1", "nested"), ("s1", "result")]
        assert await refused(owner) == []

        copy = json.loads(json.dumps(graph))
        warnings = await strip_unseen_references(session, editor, copy)
        assert copy["nodes"][0]["config"] == {"result": "token=", "nested": {"a": [""]}}
        assert {w["field"] for w in warnings} == {"result", "nested"}


async def test_a_strict_mode_admin_agent_may_not_name_a_colleagues_variable(monkeypatch):
    from app.core.config import settings
    from app.services.flow_references import check_three_tier_references

    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    async with _temp_database() as sessions, sessions() as session:
        admin = await _seed_user(session, is_admin=True)
        member = await _seed_user(session)
        theirs = Variable(user_id=member.id, name="their_value", value="v")
        session.add(theirs)
        await session.commit()
        graph = {"nodes": [_node("v1", "util.primitive", variable_id=str(theirs.id))], "edges": []}

        issues = await check_three_tier_references(session, admin, graph=graph)

        assert [(i["node_id"], i["field"]) for i in issues] == [("v1", "variable_id")]


async def test_a_copy_leaves_out_what_its_copier_cannot_use():
    from app.services.flow_references import strip_unseen_references

    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        reader = await _seed_user(session)
        private = await _seed_custom_tool(session, name="owner_tool", user_id=owner.id)
        company = await _seed_custom_tool(session, name="company_tool", user_id=None, is_company_managed=True)
        agent = Agent(id=uuid.uuid4(), user_id=owner.id, name="Owner agent")
        session.add(agent)
        await session.commit()
        graph = {
            "nodes": [
                _node("s1", "action.script", custom_tool_id=str(private.id)),
                _node("a1", "agent", agent_id=str(agent.id)),
                _node("c1", "capability.tool", tool_ids=[str(company.id), str(private.id)]),
            ],
            "edges": [],
        }

        warnings = await strip_unseen_references(session, reader, graph)

        config = {n["id"]: n["config"] for n in graph["nodes"]}
        assert config["s1"]["custom_tool_id"] is None
        assert config["a1"]["agent_id"] is None
        assert config["c1"]["tool_ids"] == [str(company.id)]
        assert {(w["node_id"], w["code"], w["severity"]) for w in warnings} == {
            ("s1", "reference.not_copied", "warning"),
            ("a1", "reference.not_copied", "warning"),
            ("c1", "reference.not_copied", "warning"),
        }
        assert await strip_unseen_references(session, owner, json.loads(json.dumps(graph))) == []


async def test_check_tool_references_reads_a_tool_name_like_a_run():
    """Save and run resolve a name through the same function: what warns on
    save is what the step would say."""
    from app.services.flow_references import check_tool_references
    from app.services.flow_runner import ambiguous_tool_message

    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        for _ in range(2):
            colleague = await _seed_user(session)
            await _tool(
                session, name="twice", owner=colleague, code="return 1;",
                shared_user_ids=[str(author.id)],
            )
        await _tool(session, name="off", owner=author, code="return 1;", enabled=False)
        await _tool(session, name="mine", owner=author, code="return 1;")
        graph = {
            "nodes": [
                _node("a", "action.call_tool", tool_name="mine"),
                _node("b", "action.call_tool", tool_name="twice"),
                _node("c", "action.call_tool", tool_name="off"),
                # LLM mode: the leftover tool name does not run.
                _node("d", "action.call_tool", tool_name="nobody_has_it", candidate_tools=["mine"]),
                _node("llm", "capability.llm"),
            ],
            "edges": [_edge("llm", "d", sh="provides", th="llm", kind="capability")],
        }

        issues = await check_tool_references(session, author, graph=graph, flow_owner_id=author.id)

        assert [(i["node_id"], i["code"], i["severity"], i["message"]) for i in issues] == [
            ("b", "call_tool.ambiguous", "warning", ambiguous_tool_message("twice")),
            ("c", "call_tool.tool_missing", "warning", "No tool named `off` is available to this flow."),
        ]


async def test_the_call_tool_picker_offers_every_tool_by_id_and_marks_what_a_name_designates():
    """The drawer stores the id of the tool it picks next to its name,
    so every enabled tool its user can use is offered with its own schema;
    ``designated`` is the one a node storing the name alone runs, and
    ``origin`` tells tools of one name apart. A custom tool a built-in's name
    shadows never runs from this node and is not offered."""
    from app.api.v1.flows import list_callable_tools

    builtin_name = flow_runner.flow_builtin_tools()[0][0].name
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        admin = await _seed_user(session, is_admin=True)
        colleague = await _seed_user(session)
        company = await _tool(session, name="lookup", owner=None, code="return 1;")
        own = await _tool(session, name="lookup", owner=author, code="return 1;")
        own.input_schema = {"type": "object", "properties": {"own": {"type": "string"}}}
        await _tool(session, name="off", owner=author, code="return 1;", enabled=False)
        await _tool(session, name=builtin_name, owner=author, code="return 1;")
        shared = await _tool(
            session, name="enrich", owner=colleague, code="return 1;", shared_user_ids=[str(author.id)]
        )
        await session.commit()

        async def offered(user):
            items = await list_callable_tools(user=user, db=session)
            return {(i.name, i.id, i.designated, i.origin) for i in items if i.source == "custom"}

        assert await offered(author) == {
            ("lookup", str(own.id), True, "own"),
            ("lookup", str(company.id), False, "company"),
            ("enrich", str(shared.id), True, "shared"),
        }
        items = {i.id: i for i in await list_callable_tools(user=author, db=session)}
        assert items[str(own.id)].input_schema == own.input_schema
        assert items[str(shared.id)].owner_name == colleague.full_name
        assert items[str(company.id)].owner_name is None
        assert all(i.origin is None for i in await list_callable_tools(user=author, db=session) if i.source == "builtin")
        # An admin reaches every tool; a colleague's private one is nobody's
        # share, and two of one name in that tier are both offered, neither
        # designated.
        assert ("enrich", str(shared.id), True, "other") in await offered(admin)
        privates = [
            await _tool(session, name="private_lookup", owner=await _seed_user(session), code="return 1;")
            for _ in range(2)
        ]
        assert {("private_lookup", str(t.id), False, "other") for t in privates} <= await offered(admin)
        assert all(i.name != "private_lookup" for i in await list_callable_tools(user=author, db=session))
        # A share through a group reads as one.
        team = Group(name=f"team-{uuid.uuid4().hex[:8]}", created_by=colleague.id)
        session.add(team)
        await session.flush()
        session.add(GroupMember(group_id=team.id, user_id=author.id))
        by_group = await _tool(
            session, name="grouped", owner=colleague, code="return 1;", shared_group_ids=[str(team.id)]
        )
        assert ("grouped", str(by_group.id), True, "shared") in await offered(author)


async def test_a_coeditor_may_only_add_a_tool_name_that_designates_a_tool_they_can_use():
    """A name resolves in the owner's reach and the run hands its output to
    whoever reads the run: a co-editor adding the name of the owner's private
    tool is refused, like an integration they cannot see."""
    from app.services.flow_references import check_tool_references

    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        editor = await _seed_user(session)
        await _tool(session, name="owner_secret", owner=owner, code="return 1;")
        await _tool(session, name="editor_own", owner=editor, code="return 1;")
        await _tool(session, name="company_lookup", owner=None, code="return 1;")
        await _tool(
            session, name="shared_lookup", owner=owner, code="return 1;",
            shared_user_ids=[str(editor.id)],
        )

        def graph(*names, llm=False):
            nodes = [
                _node(f"n{i}", "action.call_tool", **({"candidate_tools": [n]} if llm else {"tool_name": n}))
                for i, n in enumerate(names)
            ]
            return {"nodes": nodes, "edges": []}

        async def errors(writer, new, previous=None):
            issues = await check_tool_references(
                session, writer, graph=new, previous_graph=previous, flow_owner_id=owner.id
            )
            return [(i["node_id"], i["field"]) for i in issues if i["severity"] == "error"]

        added = graph("owner_secret", "editor_own", "company_lookup", "shared_lookup", "nobody_has_it")
        # Its own tool is outside the owner's reach, and a name no tool carries
        # is refused like a hidden one, so a refusal says nothing about the
        # owner's tools.
        assert await errors(editor, added) == [
            ("n0", "tool_name"), ("n1", "tool_name"), ("n4", "tool_name"),
        ]
        assert await errors(editor, graph("owner_secret", llm=True)) == [("n0", "candidate_tools")]
        # A name the flow already held is never re-litigated; the owner writes
        # in their own reach.
        assert await errors(editor, added, previous=added) == []
        assert await errors(owner, added) == []
        # A run checks the strict-mode admin only: what a member runs is what
        # REST lets them run.
        run_check = await check_tool_references(
            session, editor, graph=added, flow_owner_id=owner.id, check_co_editors=False
        )
        assert [i for i in run_check if i["severity"] == "error"] == []


async def test_a_strict_mode_admin_agent_may_not_add_a_tool_name_past_its_three_tiers():
    from app.services.flow_references import check_tool_references

    async with _temp_database() as sessions, sessions() as session:
        admin = await _seed_user(session, is_admin=True)
        member = await _seed_user(session)
        await _tool(session, name="member_private", owner=member, code="return 1;")
        new = {"nodes": [_node("n0", "action.call_tool", tool_name="member_private")], "edges": []}

        async def errors(bypass):
            issues = await check_tool_references(
                session, admin, graph=new, flow_owner_id=admin.id, admin_bypass=bypass
            )
            return [i["code"] for i in issues if i["severity"] == "error"]

        assert await errors(False) == ["call_tool.not_usable"]
        assert await errors(True) == []
        assert await errors(None) == []


# ── The tool a Call-tool node picked ─────────────────────────────────
#
# The drawer stores the id of the custom tool it picks next to its name. A run
# executes that tool while it is still an enabled tool of that name in the
# flow's reach, and resolves the name for a node without one, or once that
# tool is gone.


def _pinned_call_tool_graph(tool_name, tool_id):
    graph = _call_tool_graph(tool_name)
    graph["nodes"][1]["config"]["tool_id"] = str(tool_id)
    return graph


async def _colleague_tool(session, user, *, code, name="lookup", **fields):
    """A tool of a new colleague of *user*, shared with them: the last tier."""
    colleague = await _seed_user(session)
    return await _tool(session, name=name, owner=colleague, code=code, shared_user_ids=[str(user.id)], **fields)


async def test_a_pinned_tool_runs_whatever_later_carries_its_name(monkeypatch):
    """A tool of the same name shared into the picked tool's tier used to
    make every run fail as ambiguous. The pin keeps running the tool picked,
    and an own tool of the name created later does not take it over; a node
    storing the name alone still resolves it as before."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        picked = await _colleague_tool(session, author, code="return 'picked';")
        pinned = await _seed_owned_flow(session, author, _pinned_call_tool_graph("lookup", picked.id))
        by_name = await _seed_owned_flow(session, author, _call_tool_graph("lookup"))

        await _colleague_tool(session, author, code="return 'later';")
        run, outcome = await _run_to_outcome(session, author, pinned)
        assert outcome.kind == "completed", run.error
        run, outcome = await _run_to_outcome(session, author, by_name)
        assert outcome.kind == "failed"
        assert await _call_tool_error(session, run) == flow_runner.ambiguous_tool_message("lookup")

        await _tool(session, name="lookup", owner=author, code="return 'own';")
        run, outcome = await _run_to_outcome(session, author, pinned)
        assert outcome.kind == "completed", run.error

        assert ran == ["return 'picked';", "return 'picked';"]


async def test_a_pin_that_is_gone_falls_back_to_the_name(monkeypatch):
    """Deleted, disabled or renamed, the picked tool no longer runs and the
    name decides, as for a node written before pins: another tool of that
    name, or the refusal when the name designates several."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        await _tool(session, name="lookup", owner=None, code="return 'company';")
        for gone in ("deleted", "disabled", "renamed"):
            picked = await _tool(session, name="lookup", owner=author, code=f"return '{gone}';")
            flow = await _seed_owned_flow(session, author, _pinned_call_tool_graph("lookup", picked.id))
            if gone == "deleted":
                await session.delete(picked)
            elif gone == "disabled":
                picked.enabled = False
            else:
                picked.name = "lookup_v2"
            await session.commit()

            run, outcome = await _run_to_outcome(session, author, flow)
            assert outcome.kind == "completed", run.error
        assert ran == ["return 'company';"] * 3

        first = await _colleague_tool(session, author, name="report", code="return 'first';")
        for index in range(2):
            await _colleague_tool(session, author, name="report", code=f"return '{index}';")
        flow = await _seed_owned_flow(session, author, _pinned_call_tool_graph("report", first.id))
        await session.delete(first)
        await session.commit()

        run, outcome = await _run_to_outcome(session, author, flow)
        assert outcome.kind == "failed"
        assert await _call_tool_error(session, run) == flow_runner.ambiguous_tool_message("report")
        assert ran == ["return 'company';"] * 3


async def test_a_pin_never_reaches_past_the_flows_reach(monkeypatch):
    """A pin names a tool, it grants none: another user's private tool of the
    node's name does not run, and the name decides."""
    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        stranger = await _seed_user(session)
        private = await _tool(session, name="lookup", owner=stranger, code="return 'private';")
        flow = await _seed_owned_flow(session, author, _pinned_call_tool_graph("lookup", private.id))

        run, outcome = await _run_to_outcome(session, author, flow)
        assert outcome.kind == "failed"
        assert await _call_tool_error(session, run) == "No tool named `lookup` is available to this flow."

        await _tool(session, name="lookup", owner=author, code="return 'own';")
        run, outcome = await _run_to_outcome(session, author, flow)
        assert outcome.kind == "completed", run.error
        assert ran == ["return 'own';"]


async def test_a_pin_from_another_install_saves_and_falls_back_to_the_name(monkeypatch):
    """An imported flow carries ids that name nothing here: it saves, as any
    such id does for its owner, and the node resolves its name."""
    from app.services.flow_references import check_source_references, check_tool_references

    ran = _record_sandbox(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        await _tool(session, name="lookup", owner=author, code="return 'own';")
        graph = _pinned_call_tool_graph("lookup", uuid.uuid4())

        assert await check_source_references(
            session, author, graph=graph, previous_graph=None, flow_owner_id=None
        ) == []
        assert [i["code"] for i in await check_tool_references(session, author, graph=graph)] == [
            "call_tool.pin_unavailable"
        ]
        flow = await _seed_owned_flow(session, author, graph)
        run, outcome = await _run_to_outcome(session, author, flow)

        assert outcome.kind == "completed", run.error
        assert ran == ["return 'own';"]


async def test_llm_mode_offers_the_candidate_tool_picked_for_a_name(monkeypatch):
    """The picked candidate is offered, over the tool its name designates; a
    runner who cannot use it is offered nothing in its place."""
    ran = _record_sandbox(monkeypatch)
    offered: list[str] = []
    _fake_selector(monkeypatch, offered)
    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        runner = await _seed_user(session)
        await _tool(session, name="lookup", owner=None, code="return 'company';")
        picked = await _colleague_tool(session, owner, code="return 'picked';")
        graph = _llm_call_tool_graph(["lookup"])
        graph["nodes"][1]["config"]["candidate_tool_ids"] = {"lookup": str(picked.id)}
        flow = await _seed_owned_flow(session, owner, graph, shared_user_ids=[str(runner.id)])

        run, outcome = await _run_to_outcome(session, owner, flow)
        assert outcome.kind == "completed", run.error
        assert offered == [str(picked.id)]

        run, outcome = await _run_to_outcome(session, runner, flow)
        assert outcome.kind == "failed"
        assert offered == [str(picked.id)]
        assert ran == ["return 'picked';"]


async def test_a_writer_may_only_pin_a_tool_they_can_see():
    """A pin is an id a run executes: the write refuses one its writer could
    not have picked, in either mode, and a copy leaves it out."""
    from app.services.flow_references import check_source_references, strip_unseen_references

    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        editor = await _seed_user(session)
        secret = await _tool(session, name="lookup", owner=owner, code="return 1;")
        editors = await _tool(session, name="lookup", owner=editor, code="return 1;")
        graph = {
            "nodes": [
                _node("n0", "action.call_tool", tool_name="lookup", tool_id=str(secret.id)),
                _node(
                    "n1", "action.call_tool",
                    candidate_tools=["lookup"], candidate_tool_ids={"lookup": str(secret.id)},
                ),
                _node("n2", "action.call_tool", tool_name="lookup", tool_id=str(editors.id)),
            ],
            "edges": [],
        }

        async def refused(writer, previous=None):
            issues = await check_source_references(
                session, writer, graph=graph, previous_graph=previous, flow_owner_id=owner.id
            )
            assert {i["code"] for i in issues} <= {"reference.not_visible"}
            return [(i["node_id"], i["field"]) for i in issues]

        assert await refused(editor) == [("n0", "tool_id"), ("n1", "candidate_tool_ids")]
        assert await refused(owner) == [("n2", "tool_id")]
        assert await refused(editor, previous=graph) == []

        copy = json.loads(json.dumps(graph))
        await strip_unseen_references(session, editor, copy)
        config = {n["id"]: n["config"] for n in copy["nodes"]}
        assert config["n0"]["tool_id"] is None
        assert config["n1"]["candidate_tool_ids"] == {}
        assert config["n2"]["tool_id"] == str(editors.id)


async def test_a_strict_mode_admin_agent_may_not_pin_past_its_three_tiers(monkeypatch):
    from app.core.config import settings
    from app.services.flow_references import check_three_tier_references

    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    async with _temp_database() as sessions, sessions() as session:
        admin = await _seed_user(session, is_admin=True)
        member = await _seed_user(session)
        private = await _tool(session, name="member_private", owner=member, code="return 1;")
        graph = {
            "nodes": [_node("n0", "action.call_tool", tool_name="member_private", tool_id=str(private.id))],
            "edges": [],
        }

        issues = await check_three_tier_references(session, admin, graph=graph)

        assert [(i["node_id"], i["field"], i["code"]) for i in issues] == [
            ("n0", "tool_id", "reference.outside_tiers")
        ]


async def test_check_tool_references_reads_a_pin_like_a_run():
    """A picked tool that still runs says nothing, whatever else carries its
    name; one that is gone says the name now decides, or that it cannot."""
    from app.services.flow_references import check_tool_references

    async with _temp_database() as sessions, sessions() as session:
        author = await _seed_user(session)
        twice = [await _colleague_tool(session, author, name="twice", code="return 1;") for _ in range(2)]
        gone = await _tool(session, name="gone", owner=author, code="return 1;", enabled=False)
        await _tool(session, name="gone", owner=None, code="return 1;")
        graph = {
            "nodes": [
                _node("a", "action.call_tool", tool_name="twice", tool_id=str(twice[0].id)),
                _node("b", "action.call_tool", tool_name="gone", tool_id=str(gone.id)),
                _node("c", "action.call_tool", tool_name="twice", tool_id=str(uuid.uuid4())),
                _node("d", "action.call_tool", candidate_tools=["twice"], candidate_tool_ids={"twice": str(twice[1].id)}),
                _node("e", "action.call_tool", candidate_tools=["gone"], candidate_tool_ids={"gone": str(gone.id)}),
            ],
            "edges": [],
        }

        issues = await check_tool_references(session, author, graph=graph, flow_owner_id=author.id)

        assert [(i["node_id"], i["code"], i["field"], i["message"]) for i in issues] == [
            ("b", "call_tool.pin_unavailable", "tool_name", flow_runner.unpinned_tool_message("gone")),
            ("c", "call_tool.ambiguous", "tool_name", flow_runner.ambiguous_tool_message("twice")),
            ("e", "call_tool.pin_unavailable", "candidate_tools", flow_runner.unpinned_tool_message("gone")),
        ]


async def test_a_coeditor_who_pins_a_tool_they_can_use_is_held_to_the_name_only_as_its_fallback():
    """The pin runs while its tool does; once it goes the name decides. So a
    co-editor's pinned tool may share an ambiguous name (that step then fails
    rather than run another tool), never one that designates a tool of the
    owner's they cannot use."""
    from app.services.flow_references import check_tool_references

    async with _temp_database() as sessions, sessions() as session:
        owner = await _seed_user(session)
        editor = await _seed_user(session)
        in_reach = {"shared_user_ids": [str(owner.id)]}
        shared = await _tool(session, name="shared_lookup", owner=editor, code="return 1;", **in_reach)
        await _colleague_tool(session, owner, name="shared_lookup", code="return 1;")
        masked = await _tool(session, name="masked", owner=editor, code="return 1;", **in_reach)
        await _tool(session, name="masked", owner=owner, code="return 1;")
        graph = {
            "nodes": [
                _node("n0", "action.call_tool", tool_name="shared_lookup", tool_id=str(shared.id)),
                _node("n1", "action.call_tool", tool_name="masked", tool_id=str(masked.id)),
                _node("n2", "action.call_tool", tool_name="shared_lookup"),
            ],
            "edges": [],
        }

        issues = await check_tool_references(session, editor, graph=graph, flow_owner_id=owner.id)

        errors = [(i["node_id"], i["message"]) for i in issues if i["severity"] == "error"]
        assert [node_id for node_id, _ in errors] == ["n1", "n2"]
        assert "should the tool you picked become unavailable" in errors[0][1]
        assert "no tool of that name that you can use" in errors[1][1]


# ── Workset action nodes, through the real walker ────────────────────
#
# What these pin is the wiring: the corpus comes from the canvas, an unwired
# node fails with an explanation rather than inventing a corpus, and the
# ingest → seal → digest shape actually composes.


def _workset_node(node_id="ws", **config):
    base = {"key": "reports", "scope": "run", "processing": "none"}
    base.update(config)
    return _node(node_id, "capability.workset", **base)


def _cap_edge(source, target):
    return _edge(
        source, target, sh="provides", th="workset", kind="capability"
    )


async def test_the_add_node_ingests_without_an_agent():
    """A For-each of these is how a reporting window is collected for the cost
    of a loop instead of an agentic iteration per document."""
    from app.models.workset import Workset, WorksetItem

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(),
            _node(
                "add",
                "action.workset_add",
                title="{{t1.payload.name}}",
                content="{{t1.payload.body}}",
                external_ref="{{t1.payload.id}}",
                occurred_at="{{t1.payload.date}}",
            ),
        ],
        "edges": [_edge("t1", "add"), _cap_edge("ws", "add")],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        _run, outcome = await _run_to_outcome(
            session,
            user,
            flow,
            payload={
                "name": "APT report",
                "body": "A" * 400,
                "id": "report-1",
                "date": "2026-08-01T00:00:00+00:00",
            },
        )
        assert outcome.kind == "completed", outcome.error

        item = (
            await session.execute(select(WorksetItem))
        ).scalar_one()
        assert item.title == "APT report"
        assert item.external_ref == "report-1"
        # The document's OWN date, taken from the payload rather than now.
        assert item.occurred_at.year == 2026 and item.occurred_at.month == 8
        assert item.content_chars == 400

        workset = (await session.execute(select(Workset))).scalar_one()
        # Run-scoped: the corpus is bound to THIS execution.
        assert workset.scope == "run"
        assert workset.item_count == 1

        steps = await _steps(session, _run)
        add_step = next(s for s in steps if s.node_id == "add")
        assert add_step.output["result"] == "created"
        assert add_step.output["evicted"] == 0


async def test_an_unwired_action_node_says_what_is_missing():
    """Inventing a corpus would be worse than failing: the flow would write
    into something nobody put on the canvas."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("add", "action.workset_add", title="Orphan", content="A" * 400),
        ],
        "edges": [_edge("t1", "add")],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        _run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "failed"
        assert "No workset is wired" in (outcome.error or "")
        assert "named on the canvas" in (outcome.error or "")


async def test_two_runs_of_a_run_scoped_workset_do_not_share_a_corpus():
    from app.models.workset import Workset

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(scope="run"),
            _node("add", "action.workset_add", title="R", content="A" * 400),
        ],
        "edges": [_edge("t1", "add"), _cap_edge("ws", "add")],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        await _run_to_outcome(session, user, flow)
        await _run_to_outcome(session, user, flow)

        worksets = (await session.execute(select(Workset))).scalars().all()
        assert len(worksets) == 2, "two runs must not share one corpus"
        assert all(w.item_count == 1 for w in worksets)


async def test_a_flow_scoped_workset_survives_between_runs():
    """The property a recurring report depends on: re-reading a window has to
    be cheaper than rebuilding it."""
    from app.models.workset import Workset

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(scope="flow"),
            _node(
                "add",
                "action.workset_add",
                title="R",
                content="A" * 400,
                external_ref="stable-1",
            ),
        ],
        "edges": [_edge("t1", "add"), _cap_edge("ws", "add")],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        _first, first_outcome = await _run_to_outcome(session, user, flow)
        second_run, second_outcome = await _run_to_outcome(session, user, flow)
        assert first_outcome.kind == "completed"
        assert second_outcome.kind == "completed"

        worksets = (await session.execute(select(Workset))).scalars().all()
        assert len(worksets) == 1, "the corpus must be reused across runs"
        # And the second pass recognised what it already had.
        assert worksets[0].item_count == 1
        steps = await _steps(session, second_run)
        add_step = next(s for s in steps if s.node_id == "add")
        assert add_step.output["result"] == "unchanged"


async def test_a_re_run_that_completes_an_auto_item_promotes_it_after_its_commit():
    """What an Add-to-workset node owes is queued after the node's commit, the
    automatic pass of an item no embedding job is coming for included: a re-run
    that gave an item its last value left it `pending` for good."""
    from app.services.workset import set_summary

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(scope="flow", processing="auto"),
            _node(
                "add",
                "action.workset_add",
                title="R",
                content="{{t1.payload.body}}",
                external_ref="stable-1",
                priority="{{t1.payload.priority}}",
            ),
        ],
        "edges": [_edge("t1", "add"), _cap_edge("ws", "add")],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        _run, first = await _run_to_outcome(
            session, user, flow, payload={"body": "A" * 400, "priority": ""}
        )
        assert first.kind == "completed", first.error
        item = (await session.execute(select(WorksetItem))).scalar_one()
        workset = (await session.execute(select(Workset))).scalar_one()
        # Its document embedded, and an agent wrote its summary.
        (await session.get(KnowledgeDocument, item.knowledge_document_id)).status = "ready"
        await set_summary(session, workset, item.id, "Read by an agent.")
        await session.commit()

        second_run, second = await _run_to_outcome(
            session, user, flow, payload={"body": "", "priority": "70"}
        )
        assert second.kind == "completed", second.error
        steps = await _steps(session, second_run)
        assert next(s for s in steps if s.node_id == "add").output["result"] == "updated"
        stored = await session.get(WorksetItem, item.id, populate_existing=True)
        assert stored.priority_score == 70
        assert stored.status == "ready"


async def test_seal_then_digest_composes():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(),
            _node(
                "add",
                "action.workset_add",
                title="A report",
                content="A" * 400,
                external_ref="r-1",
                priority="70",
            ),
            _node("seal", "action.workset_seal"),
            _node("digest", "action.workset_digest", top_n="5"),
        ],
        "edges": [
            _edge("t1", "add"),
            _cap_edge("ws", "add"),
            _edge("add", "seal"),
            _cap_edge("ws", "seal"),
            _edge("seal", "digest"),
            _cap_edge("ws", "digest"),
        ],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", outcome.error

        steps = {s.node_id: s for s in await _steps(session, run)}
        # Sealing is what lets a read call the corpus finished...
        assert "NOT sealed" not in steps["seal"].output["coverage"]
        digest = steps["digest"].output
        assert "A report" in digest["digest"]
        assert "cite: item=" in digest["digest"]
        assert digest["included"] == 1
        assert digest["omitted"] == 0


async def test_the_digest_node_can_refuse_a_half_processed_corpus():
    """`require_ready` is for a synthesis that claims to cover a whole window:
    better an error edge than a confident partial answer."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(),
            _node(
                "add",
                "action.workset_add",
                title="R",
                content="A" * 400,
                external_ref="r-1",
            ),
            _node(
                "digest", "action.workset_digest", require_ready=True
            ),
        ],
        "edges": [
            _edge("t1", "add"),
            _cap_edge("ws", "add"),
            _edge("add", "digest"),
            _cap_edge("ws", "digest"),
        ],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        _run, outcome = await _run_to_outcome(session, user, flow)
        # The item has no summary yet and the workset is not sealed.
        assert outcome.kind == "failed"
        assert "not finished processing" in (outcome.error or "")
        assert "Coverage:" in (outcome.error or "")


async def test_the_digest_node_reports_coverage_when_it_does_not_refuse():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(),
            _node(
                "add",
                "action.workset_add",
                title="R",
                content="A" * 400,
                external_ref="r-1",
            ),
            _node("digest", "action.workset_digest"),
        ],
        "edges": [
            _edge("t1", "add"),
            _cap_edge("ws", "add"),
            _edge("add", "digest"),
            _cap_edge("ws", "digest"),
        ],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        run, outcome = await _run_to_outcome(session, user, flow)
        assert outcome.kind == "completed", outcome.error
        steps = {s.node_id: s for s in await _steps(session, run)}
        digest = steps["digest"].output
        # Not refused, but never claiming to be whole either.
        assert digest["complete"] is False
        assert "NOT sealed" in digest["coverage"]


async def test_an_unparseable_date_is_undated_rather_than_today():
    """Mapping the wrong upstream field is the likely mistake; a wrong date
    silently files a document in the wrong window, a missing one is visible."""
    from app.models.workset import WorksetItem

    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _workset_node(),
            _node(
                "add",
                "action.workset_add",
                title="R",
                content="A" * 400,
                occurred_at="{{t1.payload.nope}}",
            ),
        ],
        "edges": [_edge("t1", "add"), _cap_edge("ws", "add")],
    }
    async with _temp_database() as session_factory, session_factory() as session:
        user, flow = await _seed(session, graph=graph)
        _run, outcome = await _run_to_outcome(
            session, user, flow, payload={"nope": "not a date"}
        )
        assert outcome.kind == "completed", outcome.error
        item = (await session.execute(select(WorksetItem))).scalar_one()
        assert item.occurred_at is None

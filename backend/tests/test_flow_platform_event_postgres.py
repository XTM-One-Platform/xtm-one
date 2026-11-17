"""A flow's platform-event trigger, through the real dispatcher and PostgreSQL.

``platform_events.dispatch_event`` end to end against a scratch database: the
flow selection, the scope, the advisory lock and the JSON dedupe of
``flow_runs.trigger_context``. Only the agentic gate is stubbed (it reads the
licence on the app's own engine). No worker walks the runs.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.flow_run import FlowRun
from app.services import platform_events
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_flow_runner_postgres import _seed, _temp_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


def _graph():
    return {
        "nodes": [
            {"id": "t1", "kind": "trigger.platform_event", "position": {"x": 0, "y": 0},
             "config": {"events": ["video.render.finished"], "filter": {"status": "ready"}}},
            {"id": "s1", "kind": "util.stop", "position": {"x": 0, "y": 0},
             "config": {"result": "{{t1.payload.file_id}}"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out", "target": "s1",
             "target_handle": "in", "kind": "data"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def _event(owner_id, event_id, **fields):
    return {
        "event_id": event_id,
        "status": "ready",
        "file_id": "f-1",
        "personal_owner_id": str(owner_id),
        "participant_agent_ids": [],
        "object_type": "video_render",
        "object_id": "r-1",
        **fields,
    }


async def _runs(session, flow_id):
    # An id, not the Flow: after expire_all, reading an attribute reloads
    # synchronously, outside the greenlet.
    session.expire_all()
    return (
        (await session.execute(select(FlowRun).where(FlowRun.flow_id == flow_id).order_by(FlowRun.created_at)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_one_event_starts_one_run_however_often_it_is_reported():
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=_graph())
        flow_id, user_id = flow.id, user.id
        flow.status = "active"
        await session.commit()

        await platform_events.dispatch_event("video.render.finished", _event(user_id, "ev-1"))
        await platform_events.dispatch_event("video.render.finished", _event(user_id, "ev-1"))
        assert len(await _runs(session, flow_id)) == 1

        await platform_events.dispatch_event("video.render.finished", _event(user_id, "ev-2"))
        runs = await _runs(session, flow_id)
        assert len(runs) == 2
        assert runs[1].trigger_type == "platform_event"
        assert runs[1].user_id == user_id
        assert runs[1].trigger_payload["payload"]["file_id"] == "f-1"
        assert runs[1].trigger_context["event_id"] == "ev-2"
        assert runs[1].trigger_context["trigger_node_id"] == "t1"


@pytest.mark.asyncio
async def test_another_users_object_and_a_filtered_event_start_nothing():
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=_graph())
        flow_id = flow.id
        flow.status = "active"
        await session.commit()

        await platform_events.dispatch_event("video.render.finished", _event(uuid.uuid4(), "ev-1"))
        await platform_events.dispatch_event("video.render.finished", _event(user.id, "ev-2", status="failed"))

        assert await _runs(session, flow_id) == []


@pytest.mark.asyncio
async def test_a_draft_flow_hears_nothing():
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=_graph())
        flow_id = flow.id

        await platform_events.dispatch_event("video.render.finished", _event(user.id, "ev-1"))

        assert await _runs(session, flow_id) == []


def _task_graph(agent_id):
    graph = _graph()
    graph["nodes"][0]["config"] = {"events": ["background_task.completed"]}
    graph["nodes"].append(
        {"id": "a1", "kind": "agent", "position": {"x": 0, "y": 0}, "config": {"agent_id": str(agent_id)}}
    )
    return graph


@pytest.mark.asyncio
@pytest.mark.parametrize("company_managed", [False, True], ids=["own-agent", "company-managed-agent"])
async def test_a_background_task_reaches_only_a_flow_whose_owner_manages_its_agent(company_managed):
    """A non-admin may place a company-managed agent they can only see; its
    tasks' results are other users' and never reach their flow."""
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=_graph())
        flow_id = flow.id
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Analyst", is_company_managed=company_managed)
        session.add(agent)
        await session.flush()
        flow.graph = _task_graph(agent.id)
        flow.status = "active"
        await session.commit()

        await platform_events.dispatch_event(
            "background_task.completed",
            {
                "event_id": "ev-task",
                "task_id": str(uuid.uuid4()),
                "agent_id": str(agent.id),
                "outcome_preview": "another user's result",
                "participant_agent_ids": [str(agent.id)],
                "object_type": "background_task",
                "object_id": "t-1",
            },
        )

        assert len(await _runs(session, flow_id)) == (0 if company_managed else 1)

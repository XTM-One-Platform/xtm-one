"""The evaluation read paths with a flow subject (#8257), on the real routes.

The Concierge links flows; an administrator sees its evaluations (no owner, not
shared). Every read names the flow as such: never an agent with no id, never a
run dropped from a list or a chart, never two flows on one key. What a flow
cannot take (an export package of agents) is refused with a 422.
"""

from __future__ import annotations

import contextlib
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.models.agent import Agent
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationRun
from app.models.flow import Flow
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_groups_or_terms(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(settings, "terms_of_use_enabled", False)


@contextlib.asynccontextmanager
async def _world():
    async with (
        scratch_database("evalflowread", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
        agent = Agent(name="Analyst", slug=f"a-{uuid.uuid4().hex[:6]}")
        flows = [Flow(name="Triage flow"), Flow(name="Other flow")]
        evaluation = Evaluation(name="Concierge", family="PROSE", concierge_built=True)
        s.add_all([admin, agent, *flows, evaluation])
        await s.flush()
        s.add_all(
            [
                EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id),
                *(EvaluationAgent(evaluation_id=evaluation.id, flow_id=f.id) for f in flows),
            ]
        )
        runs = [
            EvaluationRun(
                evaluation_id=evaluation.id,
                model_id="model-independent",
                trigger_reason="concierge",
                status="completed",
                quality_score=score,
                performance_score=score,
                **subject,
            )
            for subject, score in (
                ({"agent_id": agent.id}, 70.0),
                ({"flow_id": flows[0].id, "flow_version_number": 3}, 80.0),
                ({"flow_id": flows[1].id, "flow_version_number": 1}, 60.0),
            )
        ]
        s.add_all(runs)
        await s.commit()
        yield db.sessions, admin, agent, flows, evaluation, runs


@contextlib.asynccontextmanager
async def _client(sessions, admin):
    api = FastAPI()
    api.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

    async def _get_db():
        async with sessions() as db:
            yield db

    api.dependency_overrides[get_db] = _get_db
    api.dependency_overrides[deps.require_ai_enabled] = lambda: None
    token = create_access_token(str(admin.id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


async def test_the_detail_lists_flows_beside_agents():
    async with _world() as (sessions, admin, agent, flows, evaluation, _runs), _client(sessions, admin) as client:
        body = (await client.get(f"/api/v1/evaluations/{evaluation.id}")).json()

    assert body["agent_ids"] == [str(agent.id)]
    assert [a["id"] for a in body["agents"]] == [str(agent.id)]
    assert sorted((f["id"], f["name"]) for f in body["flows"]) == sorted((str(f.id), f.name) for f in flows)


async def test_every_run_names_its_subject():
    async with _world() as (sessions, admin, agent, flows, evaluation, _runs), _client(sessions, admin) as client:
        response = await client.get(f"/api/v1/evaluations/{evaluation.id}/runs")

    assert response.status_code == 200
    subjects = sorted((r["subject_kind"], r["agent_id"], r["flow_id"], r["agent_name"]) for r in response.json())
    assert subjects == sorted(
        [
            ("agent", str(agent.id), None, "Analyst"),
            ("flow", None, str(flows[0].id), "Triage flow"),
            ("flow", None, str(flows[1].id), "Other flow"),
        ]
    )


async def test_the_chart_keeps_every_subject_apart():
    async with _world() as (sessions, admin, agent, flows, evaluation, _runs), _client(sessions, admin) as client:
        body = (await client.get(f"/api/v1/evaluations/{evaluation.id}/chart")).json()

    points = [p for series in body["correlations"].values() for p in series]
    assert {p["series"] for p in points} == {
        "Analyst/model-independent",
        "Triage flow/model-independent",
        "Other flow/model-independent",
    }
    assert {(p["agent_id"], p["flow_id"]) for p in points} == {
        (str(agent.id), None),
        (None, str(flows[0].id)),
        (None, str(flows[1].id)),
    }


async def test_an_export_of_a_flow_subject_is_refused():
    async with _world() as (sessions, admin, _agent, _flows, evaluation, _runs), _client(sessions, admin) as client:
        response = await client.get(f"/api/v1/evaluations/{evaluation.id}/export")

    assert (response.status_code, response.json()["detail"]) == (
        422,
        "An evaluation with a flow subject cannot be exported.",
    )


async def test_a_copy_keeps_the_flow_subjects_and_drops_the_mark():
    async with _world() as (sessions, admin, agent, flows, evaluation, _runs), _client(sessions, admin) as client:
        response = await client.post(f"/api/v1/evaluations/{evaluation.id}/copy")
        copy_id = uuid.UUID(response.json()["id"])
        async with sessions() as s:
            copy = await s.get(Evaluation, copy_id)
            links = (await s.execute(select(EvaluationAgent).where(EvaluationAgent.evaluation_id == copy_id))).scalars()
            subjects = sorted(str(link.flow_id or link.agent_id) for link in links)

    assert response.status_code == 201
    assert copy.concierge_built is False
    assert subjects == sorted([str(agent.id), *(str(f.id) for f in flows)])


async def test_the_admin_agent_tools_read_flow_runs(monkeypatch):
    import json

    from app.services.builtin_tools import evaluations as tools

    async with _world() as (sessions, admin, _agent, flows, evaluation, _runs):
        monkeypatch.setattr(tools, "async_session", sessions)
        _defs, executor = tools.build_evaluation_tools(None, str(admin.id), include_withheld=True)  # type: ignore[arg-type]
        listed = await executor._handlers["list_evaluation_runs"](evaluation_id=str(evaluation.id))
        detail = await executor._handlers["get_evaluation"](evaluation_id=str(evaluation.id))

    assert {r["flow_id"] for r in json.loads(listed)["runs"]} == {None, *(str(f.id) for f in flows)}
    assert sorted(json.loads(detail)["flow_ids"]) == sorted(str(f.id) for f in flows)

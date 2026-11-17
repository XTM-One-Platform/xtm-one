"""A dashboard's data sources, against PostgreSQL: each read with the viewer's own access.

An ``assignment`` source draws the last completed run the viewer may see (a
run private to its owner is ``no_result`` to a colleague the dashboard is
shared with; a company-managed one is everyone's); a ``variable`` source the
value of a variable the viewer reads, never a secret one; an ``ask`` source
the answer of the question the viewer asked, to an agent they may chat with,
a few times a minute; a ``derived`` source its parent through its steps.
A writer may only point a source at what they reach themselves.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards
from app.core.database import get_db
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.custom_dashboard import CustomDashboard
from app.models.user import User
from app.models.variable import Variable
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]


@pytest.fixture(autouse=True)
def custom_variables_on(monkeypatch):
    """Custom variables on (their default depends on the platform mode); a test turns them off on top."""
    import app.core.security_options as security_options

    original = security_options.security_option
    monkeypatch.setattr(
        security_options, "security_option", lambda key: False if key == "disable_custom_variables" else original(key)
    )

ROWS = {
    "columns": [{"key": "sev", "label": "Severity"}],
    "rows": [{"sev": "high"}, {"sev": "low"}, {"sev": "high"}],
}


def _definition(sources: dict) -> dict:
    sid = next(iter(sources))
    return {
        "version": 2,
        "title": "Exposure",
        "dataSources": sources,
        "visualizations": {"v": {"type": "xtm.table", "dataSources": {"primary": sid}}},
        "layout": {
            "tabs": {"items": [{"layoutId": "l", "label": "L"}]},
            "layoutDefinitions": {"l": {"structure": [{"item": "v", "position": {"x": 0, "y": 0, "w": 6, "h": 6}}]}},
        },
    }


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    async def _signed_in(db: AsyncSession = Depends(get_db)) -> User:
        return await db.get(User, user_id)

    for dependency in (deps.get_current_user, deps.get_current_user_ungated):
        app.dependency_overrides[dependency] = _signed_in
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_sources", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            viewer = User(email="viewer@example.com", hashed_password="x", full_name="Viewer", role="user")
            stranger = User(email="stranger@example.com", hashed_password="x", full_name="Stranger", role="user")
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            db.add_all([owner, viewer, stranger, admin])
            await db.flush()
            share = {"shared_user_ids": [str(viewer.id)], "shared_user_permissions": {str(viewer.id): "read_only"}}
            agent = Agent(id=uuid.uuid4(), name="Analyst", user_id=owner.id, **share)
            muted = Agent(id=uuid.uuid4(), name="Muted", user_id=owner.id, disable_chat=True, **share)
            company = Agent(id=uuid.uuid4(), name="Company", user_id=None, is_company_managed=True)
            db.add_all([agent, muted, company])
            await db.flush()
            private_job = Assignment(id=uuid.uuid4(), agent_id=agent.id, user_id=owner.id, name="Weekly", trigger_type="manual", prompt="go")
            company_job = Assignment(id=uuid.uuid4(), agent_id=company.id, user_id=None, is_company_managed=True, name="KEV", trigger_type="manual", prompt="go")  # fmt: skip
            db.add_all([private_job, company_job])
            await db.flush()
            db.add_all(
                [
                    AssignmentRun(agent_id=agent.id, assignment_id=private_job.id, user_id=owner.id, status="completed", result_summary='{"open": 37}'),
                    AssignmentRun(agent_id=company.id, assignment_id=company_job.id, user_id=None, is_company_managed=True, status="completed", result_summary="12"),
                    AssignmentRun(agent_id=company.id, assignment_id=company_job.id, user_id=None, is_company_managed=True, status="failed", result_summary="99"),
                ]
            )  # fmt: skip
            plain = Variable(name="TARGET", value="95", user_id=owner.id, **share)
            secret = Variable(name="TOKEN", value="s3cr3t-value", is_secret=True, user_id=owner.id, **share)
            db.add_all([plain, secret])
            await db.flush()
            sources = {
                "priv": {"type": "assignment", "options": {"assignment_id": str(private_job.id), "shape": "number", "path": "open"}},
                "corp": {"type": "assignment", "options": {"assignment_id": str(company_job.id), "shape": "number"}},
                "var": {"type": "variable", "options": {"variable_id": str(plain.id), "shape": "number"}},
                "base": {"type": "static", "options": {"shape": "rows", "data": ROWS}},
                "top": {"type": "derived", "options": {"extends": "base", "steps": [{"op": "top", "field": "sev"}]}},
                "q": {"type": "ask", "options": {"agent_id": str(agent.id), "question": "How many open findings?", "shape": "number"}},
            }
            from app.services.dashboard_definition import validate_definition

            dashboard = CustomDashboard(
                user_id=owner.id, title="Exposure", widget_count=1, revision=1, spec=validate_definition(_definition(sources)), **share
            )  # fmt: skip
            db.add(dashboard)
            await db.commit()
            ids = SimpleNamespace(
                owner=owner.id, viewer=viewer.id, stranger=stranger.id, admin=admin.id, dashboard=dashboard.id,
                agent=agent.id, muted=muted.id, private_job=private_job.id, company_job=company_job.id,
                plain=plain.id, secret=secret.id,
            )  # fmt: skip
        yield scratch, ids


async def _read(scratch, user_id, dashboard_id, sid) -> dict:
    async with _client(scratch.sessions, user_id) as client:
        response = await client.post(f"/api/v1/custom-dashboards/{dashboard_id}/data-sources/{sid}/data", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    return {k: body[k] for k in ("status", "shape", "data", "code")}


async def test_an_assignment_source_draws_the_last_run_the_viewer_may_see(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        mine = await _read(scratch, ids.owner, ids.dashboard, "priv")
        theirs = await _read(scratch, ids.viewer, ids.dashboard, "priv")
        company = await _read(scratch, ids.viewer, ids.dashboard, "corp")
    assert mine == {"status": "ok", "shape": "number", "data": {"value": 37.0, "change": None}, "code": None}
    # The agent is shared with the viewer, its runs are not: the owner's private run is nobody else's.
    assert theirs == {"status": "error", "shape": None, "data": None, "code": "no_result"}
    # A company run is everyone's; the failed later run is not drawn.
    assert company["data"] == {"value": 12.0, "change": None}


async def test_a_variable_source_draws_a_readable_variable_and_never_a_secret(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        shared = await _read(scratch, ids.viewer, ids.dashboard, "var")
        async with scratch.sessions() as db:
            variable = await db.get(Variable, ids.plain)
            variable.is_secret = True
            await db.commit()
        turned_secret = await _read(scratch, ids.owner, ids.dashboard, "var")
    assert shared["data"] == {"value": 95.0, "change": None}
    assert turned_secret == {"status": "error", "shape": None, "data": None, "code": "access_denied"}


async def test_a_revoked_share_stops_the_data_at_once(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with scratch.sessions() as db:
            variable = await db.get(Variable, ids.plain)
            variable.shared_user_ids, variable.shared_user_permissions = [], {}
            await db.commit()
        revoked = await _read(scratch, ids.viewer, ids.dashboard, "var")
        owner = await _read(scratch, ids.owner, ids.dashboard, "var")
    assert revoked["code"] == "access_denied"
    assert owner["status"] == "ok"


async def test_a_derived_source_runs_its_steps_on_its_parent(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        derived = await _read(scratch, ids.viewer, ids.dashboard, "top")
    assert derived == {
        "status": "ok",
        "shape": "distribution",
        "data": {"items": [{"label": "high", "value": 2.0}, {"label": "low", "value": 1.0}]},
        "code": None,
    }


async def test_a_stranger_reads_no_source(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids.stranger) as client:
        response = await client.post(f"/api/v1/custom-dashboards/{ids.dashboard}/data-sources/corp/data", json={})
        unknown = await client.post(f"/api/v1/custom-dashboards/{ids.dashboard}/data-sources/ghost/data", json={})
    assert response.status_code == 404
    assert unknown.status_code == 404


class _Store:
    """An in-memory stand-in for Redis: what the ask route remembers and counts."""

    def __init__(self, broken: bool = False):
        self.values: dict[str, object] = {}
        self.broken = broken

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def incr(self, key):
        if self.broken:
            raise ConnectionError("down")
        self.values[key] = int(self.values.get(key) or 0) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        return True


@pytest.fixture
def ask_runtime(monkeypatch):
    """Sub-agent runs recorded instead of started, and their results answered from a table.

    The ask counter's minute is pinned: a test's asks never straddle a minute
    boundary, where the count would restart in the next window (#9005).
    """
    import app.core.redis as redis_module
    import app.services.dashboard_sources as dashboard_sources
    import app.services.sub_agent as sub_agent

    runtime = SimpleNamespace(store=_Store(), started=[], results={})

    async def _redis():
        return runtime.store

    async def _create(_db, **kwargs):
        task = SimpleNamespace(id=uuid.uuid4())
        runtime.started.append(kwargs)
        runtime.results[str(task.id)] = {"status": "running", "result": None, "completed_at": None}
        return task

    async def _result(_db, task_id, user_id):
        return runtime.results.get(task_id)

    monkeypatch.setattr(redis_module, "redis_client", _redis)
    monkeypatch.setattr(dashboard_sources, "_ask_window", lambda: 29_000_000)
    monkeypatch.setattr(sub_agent, "create_sub_agent_task", _create)
    monkeypatch.setattr(sub_agent, "get_task_result", _result)
    return runtime


async def test_a_question_is_asked_as_the_viewer_and_its_answer_drawn_once_ready(dashboard_studio_enabled, ask_runtime) -> None:
    async with _world() as (scratch, ids):
        before = await _read(scratch, ids.viewer, ids.dashboard, "q")
        async with _client(scratch.sessions, ids.viewer) as client:
            asked = await client.post(f"/api/v1/custom-dashboards/{ids.dashboard}/data-sources/q/ask")
        running = await _read(scratch, ids.viewer, ids.dashboard, "q")
        other = await _read(scratch, ids.owner, ids.dashboard, "q")
        task_id = next(iter(ask_runtime.results))
        ask_runtime.results[task_id] = {"status": "completed", "result": "37", "completed_at": "2026-10-25T10:00:00+00:00"}
        answered = await _read(scratch, ids.viewer, ids.dashboard, "q")
    assert before["code"] == "not_run"
    assert asked.json()["code"] == "running"
    assert [(s["user_id"], s["target_agent_id"], s["task_description"]) for s in ask_runtime.started] == [
        (str(ids.viewer), str(ids.agent), "How many open findings?")
    ]
    assert running["code"] == "running"
    # The viewer's question is theirs: the owner has asked nothing.
    assert other["code"] == "not_run"
    assert answered["data"] == {"value": 37.0, "change": None}


async def test_asking_is_bounded_per_person_and_refused_when_the_count_cannot_be_kept(dashboard_studio_enabled, ask_runtime) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids.viewer) as client:
        url = f"/api/v1/custom-dashboards/{ids.dashboard}/data-sources/q/ask"
        codes = [(await client.post(url)).json()["code"] for _ in range(7)]
        ask_runtime.store.broken = True
        ask_runtime.store.values.clear()
        broken = (await client.post(url)).json()["code"]
    assert codes == ["running"] * 6 + ["rate_limited"]
    assert broken == "rate_limited"
    assert len(ask_runtime.started) == 6


async def test_an_agent_the_viewer_cannot_chat_with_is_not_asked(dashboard_studio_enabled, ask_runtime) -> None:
    async with _world() as (scratch, ids):
        async with scratch.sessions() as db:
            agent = await db.get(Agent, ids.agent)
            agent.shared_user_ids, agent.shared_user_permissions = [], {}
            await db.commit()
        async with _client(scratch.sessions, ids.viewer) as client:
            refused = await client.post(f"/api/v1/custom-dashboards/{ids.dashboard}/data-sources/q/ask")
            not_ask = await client.post(f"/api/v1/custom-dashboards/{ids.dashboard}/data-sources/var/ask")
    assert refused.json()["code"] == "access_denied"
    assert not_ask.json()["code"] == "unsupported"
    assert ask_runtime.started == []


async def _write(scratch, user_id, dashboard_id, sid, source) -> httpx.Response:
    async with _client(scratch.sessions, user_id) as client:
        current = (await client.get(f"/api/v1/custom-dashboards/{dashboard_id}")).json()
        return await client.patch(
            f"/api/v1/custom-dashboards/{dashboard_id}",
            json={"revision": current["revision"], "operations": [{"op": "set", "path": f"dataSources.{sid}", "value": source}]},
        )


@pytest.mark.parametrize(
    ("make_source", "fragment"),
    [
        (lambda ids: {"type": "variable", "options": {"variable_id": str(ids.secret)}}, "a secret one never is"),
        (lambda ids: {"type": "ask", "options": {"agent_id": str(ids.muted), "question": "q"}}, "not an agent you can chat with"),
        (lambda ids: {"type": "assignment", "options": {"assignment_id": str(uuid.uuid4())}}, "not an assignment you can see"),
    ],
)
async def test_a_writer_points_a_source_only_at_what_they_reach(dashboard_studio_enabled, make_source, fragment) -> None:
    async with _world() as (scratch, ids):
        response = await _write(scratch, ids.owner, ids.dashboard, "extra", make_source(ids))
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


async def test_an_admin_writer_gets_no_bypass(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with scratch.sessions() as db:
            dashboard = CustomDashboard(
                user_id=ids.admin, title="Admin's", widget_count=1, revision=1,
                spec=(await db.get(CustomDashboard, ids.dashboard)).spec,
            )  # fmt: skip
            db.add(dashboard)
            await db.commit()
            admin_dashboard = dashboard.id
        refused = await _write(
            scratch, ids.admin, admin_dashboard, "extra", {"type": "variable", "options": {"variable_id": str(ids.plain)}}
        )
    assert refused.status_code == 422
    assert "not a variable you can read" in refused.json()["detail"]


async def test_with_the_studio_off_no_reference_source_is_added(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    source = {"type": "variable", "options": {"variable_id": "x"}}
    async with _world() as (scratch, ids), _client(scratch.sessions, ids.owner) as client:
        read = await client.get(f"/api/v1/custom-dashboards/{ids.dashboard}")
        written = await client.patch(
            f"/api/v1/custom-dashboards/{ids.dashboard}",
            json={"revision": 1, "operations": [{"op": "set", "path": "dataSources.extra", "value": source}]},
        )
    # The whole dashboards API is the studio's: off, its routes are unknown.
    assert (read.status_code, written.status_code) == (404, 404)


async def test_with_custom_variables_off_no_variable_is_read_or_added(dashboard_studio_enabled, monkeypatch) -> None:
    import app.core.security_options as security_options

    original = security_options.security_option
    monkeypatch.setattr(
        security_options, "security_option", lambda key: True if key == "disable_custom_variables" else original(key)
    )
    async with _world() as (scratch, ids):
        read = await _read(scratch, ids.owner, ids.dashboard, "var")
        written = await _write(scratch, ids.owner, ids.dashboard, "extra", {"type": "variable", "options": {"variable_id": str(ids.plain)}})
    assert read == {"status": "error", "shape": None, "data": None, "code": "unsupported"}
    assert written.status_code == 422
    assert "custom variables are disabled" in written.json()["detail"]


async def test_the_schema_route_reads_only_through_the_callers_own_integrations(dashboard_studio_enabled) -> None:
    from app.models.integration import Integration

    async with _world() as (scratch, ids):
        async with scratch.sessions() as db:
            theirs = Integration(name="theirs", provider="opencti", auth_type="api_key", user_id=ids.stranger)
            db.add(theirs)
            await db.commit()
            theirs_id = theirs.id
        async with _client(scratch.sessions, ids.owner) as client:
            url = "/api/v1/custom-dashboards/platform-schema"
            refused = await client.post(url, json={"source": "opencti", "integration_id": str(theirs_id), "kind": "entity_types"})
            no_key = await client.post(url, json={"source": "opencti", "integration_id": str(theirs_id), "kind": "values"})
            bad_key = await client.post(url, json={"source": "opencti", "integration_id": str(theirs_id), "kind": "values", "key": "a b"})
            other = await client.post(url, json={"source": "splunk", "integration_id": str(theirs_id), "kind": "entity_types"})
    assert refused.json() == {"status": "error", "items": [], "code": "integration_unavailable"}
    assert [r.status_code for r in (no_key, bad_key, other)] == [422, 422, 422]


async def test_the_schema_route_is_the_studios(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    async with _world() as (scratch, ids), _client(scratch.sessions, ids.owner) as client:
        response = await client.post(
            "/api/v1/custom-dashboards/platform-schema",
            json={"source": "opencti", "integration_id": str(uuid.uuid4()), "kind": "entity_types"},
        )
    assert response.status_code == 404


async def test_a_read_puts_the_page_s_tokens_into_its_source_or_waits(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        written = await _write(
            scratch, ids.owner, ids.dashboard, "picked",
            {"type": "derived", "options": {"extends": "base", "steps": [{"op": "where", "field": "sev", "operator": "eq", "value": "$sev$"}, {"op": "stats", "agg": "count"}]}},
        )  # fmt: skip
        url = f"/api/v1/custom-dashboards/{ids.dashboard}/data-sources/picked/data"
        async with _client(scratch.sessions, ids.viewer) as client:
            high = (await client.post(url, json={"tokens": {"sev": "high"}})).json()
            low = (await client.post(url, json={"tokens": {"sev": "LOW"}})).json()
            waiting = (await client.post(url, json={})).json()
            refused = await client.post(url, json={"tokens": {"sev": "x" * 501}})
            nested = await client.post(url, json={"tokens": {"sev": {"$ne": 1}}})
    assert written.status_code == 200, written.text
    assert (high["data"], low["data"]) == ({"value": 2.0, "change": None}, {"value": 1.0, "change": None})
    assert waiting["code"] == "waiting"
    assert (refused.status_code, nested.status_code) == (422, 422)

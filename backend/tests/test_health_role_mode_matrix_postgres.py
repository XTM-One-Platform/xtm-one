"""Platform Health role x mode matrix (#101 S13, #794) on PostgreSQL.

dev-docs/platform-health.md section 8 "Access matrix": every read route of
``/api/v1/health`` answered for the four roles (administrator, group manager,
agent owner, plain user) in the three modes (Copilot, XTM One, ``ai_disabled``).
One table drives the test, so a route that opens or closes for a role or a
mode changes one line here and its row of the doc in the same pull request.

Pinned besides the status: what an open answer holds for that reader (the
module cards of the mode for an administrator, the generic cards of their
own scope for everyone else) and that the one AI action of the page, the
analysis run, is refused with ``ai_disabled`` while AI is off.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import (
    platform_health,
    platform_health_analysis,
    platform_health_companion,
    platform_health_cycles,
    platform_health_findings,
)
from app.core.config import settings
from app.core.database import get_db
from app.models.group import Group, GroupManager, GroupMember
from app.services import agentic_gate, ai_state
from app.services.health.engine import CheckResult, record_results
from tests.health_support import health_database, make_agent, make_user

pytestmark = pytest.mark.integration

ROLES = ("admin", "group_manager", "agent_owner", "plain_user")
MODES = ("copilot", "xtm_one", "ai_disabled")
GENERIC = ["agents", "fleets"]
EVERY_MODULE = ["agents", "fleets", "xtm_one", "opencti", "openaev", "opencrq"]

#: Route -> the status each non-admin role gets (an administrator gets 200 on every one, every mode).
#: 403: an administrator's route (require_admin). 404: a cross-target list a non-admin
#: reads only for one target they manage (``_require_target_reader``).
READS: dict[str, int] = {
    "/name": 200,
    "/me/opt-out": 200,
    "/overview": 200,
    "/findings": 200,
    "/history?days=7": 200,
    "/improvements": 404,
    "/cycles": 404,
    "/signals": 404,
    "/settings": 403,
    "/budget": 403,
    "/schedules": 403,
    "/analysis": 403,
    "/me/companion": 403,
    "/me/activity": 403,
    "/pets": 200,
}


@pytest.fixture
def mode(request, monkeypatch) -> str:
    """The platform mode, with AI resolved off in ``ai_disabled`` and licensed elsewhere."""
    value: str = request.param
    monkeypatch.setattr(settings, "platform_mode", value)

    async def _ai(_db) -> bool:
        return value != "ai_disabled"

    async def _verdict(_db):
        return agentic_gate.AgenticVerdict(refusal=None)

    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    monkeypatch.setattr(agentic_gate, "agentic_verdict", _verdict)
    return value


@asynccontextmanager
async def _client(sessions, user) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    for router in (
        platform_health.router,
        platform_health_cycles.router,
        platform_health_companion.router,
        platform_health_findings.router,
        platform_health_analysis.router,
    ):
        api.include_router(router, prefix="/api/v1/health")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


def _failing(check_id: str, module: str, subject: dict[str, str]) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        module=module,
        category="availability",
        severity="warning",
        failing=True,
        subject=subject,
        evidence={"values": {"x": 1}},
        recommendation="Do it.",
    )


async def _people(sessions) -> dict[str, object]:
    """The four roles, an agent of the owner with a finding, and an OpenCTI finding."""
    people = {
        "admin": make_user("admin", admin=True),
        "group_manager": make_user("manager"),
        "agent_owner": make_user("owner"),
        "plain_user": make_user("plain"),
    }
    async with sessions() as db:
        db.add_all(list(people.values()))
        await db.flush()
        team = Group(id=uuid.uuid4(), name="SOC")
        db.add(team)
        await db.flush()
        db.add_all(
            [
                GroupMember(group_id=team.id, user_id=people["agent_owner"].id),
                GroupManager(group_id=team.id, user_id=people["group_manager"].id, assigned_by=people["admin"].id),
            ]
        )
        agent = make_agent(people["agent_owner"], "triage")
        db.add(agent)
        await db.flush()
        await record_results(
            db,
            [
                _failing("agents.tool_errors", "agents", {"agent_id": str(agent.id)}),
                _failing("opencti.heartbeat", "opencti", {"registration_id": "r1"}),
            ],
            now=datetime.now(UTC),
        )
        await db.commit()
    return people


def _expected(role: str, route: str) -> int:
    return 200 if role == "admin" else READS[route]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES, indirect=True)
async def test_every_read_route_answers_each_role_as_the_matrix_says(mode: str) -> None:
    async with health_database() as sessions:
        people = await _people(sessions)
        answers: dict[tuple[str, str], int] = {}
        for role in ROLES:
            async with _client(sessions, people[role]) as client:
                for route in READS:
                    answers[(role, route)] = (await client.get(f"/api/v1/health{route}")).status_code

    expected = {(role, route): _expected(role, route) for role in ROLES for route in READS}
    wrong = {key: (answers[key], want) for key, want in expected.items() if answers[key] != want}
    assert wrong == {}, f"{mode}: (role, route) -> (answered, expected): {wrong}"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES, indirect=True)
async def test_the_overview_shows_each_role_the_cards_of_its_scope(mode: str) -> None:
    async with health_database() as sessions:
        people = await _people(sessions)
        seen: dict[str, tuple[list[str], str, int]] = {}
        for role in ROLES:
            async with _client(sessions, people[role]) as client:
                overview = (await client.get("/api/v1/health/overview")).json()
                findings = (await client.get("/api/v1/health/findings")).json()
            seen[role] = ([card["module"] for card in overview["modules"]], overview["scope"], findings["total"])

    admin_cards = GENERIC if mode == "copilot" else EVERY_MODULE
    # The administrator reads every finding of the mode: the agent's, plus OpenCTI's outside Copilot.
    assert seen["admin"] == (admin_cards, "all", 1 if mode == "copilot" else 2)
    # The owner manages their agent; a group manager does not manage its members' agents (cycle_access rule).
    assert seen["agent_owner"] == (GENERIC, "managed", 1)
    assert seen["group_manager"] == (GENERIC, "managed", 0)
    assert seen["plain_user"] == (GENERIC, "managed", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ai_disabled"], indirect=True)
async def test_the_analysis_run_is_refused_while_ai_is_off(mode: str) -> None:
    async with health_database() as sessions:
        people = await _people(sessions)
        async with _client(sessions, people["admin"]) as client:
            refused = await client.post("/api/v1/health/analysis/opencti/run", json={})

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == ai_state.AI_DISABLED_CODE

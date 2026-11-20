"""Platform Health page API (#101 S8, dev-docs/platform-health.md 8) on PostgreSQL.

Pinned here: the overview has one card per module of the mode (Copilot-like:
agents and fleets only), an administrator lists every open finding most severe
first, a non-admin sees only the findings of the agents they manage and no
module finding, a module finding answers 404 in Copilot mode, a dismissal needs
a reason and is audited, and the history answers the 7, 30 and 90 day windows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import platform_health_findings as routes
from app.core.config import settings
from app.core.database import get_db
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.health_check import STATUS_DISMISSED, HealthCheckFinding
from app.models.user import User
from app.services.health.engine import CheckResult, record_results
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def _user(role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"hf-{uuid.uuid4().hex[:8]}@example.com",
        full_name=f"Health {role}",
        hashed_password="x",
        role=role,
    )


def _result(check_id: str, module: str, severity: str, subject: dict[str, str]) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        module=module,
        category="availability",
        severity=severity,
        failing=True,
        subject=subject,
        evidence={"values": {"x": 1}},
        recommendation="Do it.",
    )


@asynccontextmanager
async def _scratch(prefix: str):
    async with scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as s:
        yield s.sessions


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/health")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _seed(sessions):
    """An admin, an owner with an agent, a stranger with another agent, and findings on both plus OpenCTI."""
    admin, owner, stranger = _user("admin"), _user(), _user()
    async with sessions() as db:
        db.add_all([admin, owner, stranger])
        await db.flush()
        mine = Agent(id=uuid.uuid4(), name="Mine", user_id=owner.id, persona="p")
        theirs = Agent(id=uuid.uuid4(), name="Theirs", user_id=stranger.id, persona="p")
        db.add_all([mine, theirs])
        await db.flush()
        await record_results(
            db,
            [
                _result("agents.tool_errors", "agents", "warning", {"agent_id": str(mine.id)}),
                _result("agents.tool_errors", "agents", "critical", {"agent_id": str(theirs.id)}),
                _result("opencti.heartbeat", "opencti", "critical", {"registration_id": "r1"}),
            ],
            now=NOW,
        )
        await db.commit()
    return admin, owner, mine


@pytest.fixture
def xtm_one_mode(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")


@pytest.mark.asyncio
async def test_an_admin_sees_every_module_card_and_every_finding_most_severe_first(xtm_one_mode):
    async with _scratch("hf_admin") as sessions:
        admin, _owner, _mine = await _seed(sessions)
        async with _client(sessions, admin) as client:
            overview = (await client.get("/api/v1/health/overview")).json()
            listing = (await client.get("/api/v1/health/findings")).json()

    cards = {card["module"]: card for card in overview["modules"]}
    assert list(cards) == ["agents", "fleets", "xtm_one", "opencti", "openaev", "opencrq"]
    assert (cards["agents"]["critical"], cards["agents"]["warning"], cards["agents"]["score"]) == (1, 1, 67)
    assert cards["opencti"]["score"] == 75
    assert overview["overall"] == 67
    assert overview["scope"] == "all"
    assert listing["total"] == 3
    assert [item["severity"] for item in listing["items"]] == ["critical", "critical", "warning"]
    assert {item["subject_name"] for item in listing["items"] if item["module"] == "agents"} == {"Mine", "Theirs"}


@pytest.mark.asyncio
async def test_a_manager_sees_only_the_findings_of_the_agents_they_manage(xtm_one_mode):
    async with _scratch("hf_owner") as sessions:
        _admin, owner, _mine = await _seed(sessions)
        async with _client(sessions, owner) as client:
            overview = (await client.get("/api/v1/health/overview")).json()
            listing = (await client.get("/api/v1/health/findings")).json()
            module_only = (await client.get("/api/v1/health/findings?module=opencti")).json()
            history = (await client.get("/api/v1/health/history?days=7")).json()

    assert [card["module"] for card in overview["modules"]] == ["agents", "fleets"]
    assert overview["scope"] == "managed"
    assert [(item["subject_name"], item["severity"]) for item in listing["items"]] == [("Mine", "warning")]
    assert module_only == {"items": [], "total": 0}
    assert history == []


@pytest.mark.asyncio
async def test_module_findings_do_not_exist_in_copilot_mode(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    async with _scratch("hf_copilot") as sessions:
        admin, _owner, _mine = await _seed(sessions)
        async with sessions() as db:
            opencti = (
                await db.execute(select(HealthCheckFinding).where(HealthCheckFinding.module == "opencti"))
            ).scalar_one()
        async with _client(sessions, admin) as client:
            overview = (await client.get("/api/v1/health/overview")).json()
            listing = (await client.get("/api/v1/health/findings")).json()
            missing = await client.get(f"/api/v1/health/findings/{opencti.id}")

    assert [card["module"] for card in overview["modules"]] == ["agents", "fleets"]
    assert {item["module"] for item in listing["items"]} == {"agents"}
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_a_dismissal_needs_a_reason_is_audited_and_hides_the_finding(xtm_one_mode):
    async with _scratch("hf_dismiss") as sessions:
        _admin, owner, mine = await _seed(sessions)
        async with sessions() as db:
            row = (
                await db.execute(
                    select(HealthCheckFinding).where(HealthCheckFinding.subject["agent_id"].astext == str(mine.id))
                )
            ).scalar_one()
        async with _client(sessions, owner) as client:
            empty = await client.post(f"/api/v1/health/findings/{row.id}/dismiss", json={"reason": ""})
            done = await client.post(
                f"/api/v1/health/findings/{row.id}/dismiss", json={"reason": "Known, accepted", "days": 7}
            )
            again = await client.post(f"/api/v1/health/findings/{row.id}/dismiss", json={"reason": "twice"})
            listing = (await client.get("/api/v1/health/findings")).json()
        async with sessions() as db:
            audits = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "health_finding"))).scalars().all()
            )

    assert empty.status_code == 422
    assert done.status_code == 200
    assert done.json()["status"] == STATUS_DISMISSED
    assert done.json()["dismissal"]["reason"] == "Known, accepted"
    assert again.status_code == 409
    assert listing["total"] == 0
    assert [(a.action, a.details["finding_id"]) for a in audits] == [("dismiss", row.id)]


@pytest.mark.asyncio
async def test_a_stranger_cannot_read_or_dismiss_a_finding_of_an_agent_they_do_not_manage(xtm_one_mode):
    async with _scratch("hf_stranger") as sessions:
        _admin, _owner, mine = await _seed(sessions)
        outsider = _user()
        async with sessions() as db:
            db.add(outsider)
            await db.commit()
            row = (
                await db.execute(
                    select(HealthCheckFinding).where(HealthCheckFinding.subject["agent_id"].astext == str(mine.id))
                )
            ).scalar_one()
        async with _client(sessions, outsider) as client:
            read = await client.get(f"/api/v1/health/findings/{row.id}")
            dismiss = await client.post(f"/api/v1/health/findings/{row.id}/dismiss", json={"reason": "no"})

    assert (read.status_code, dismiss.status_code) == (404, 404)


@pytest.mark.asyncio
async def test_a_dismissed_finding_is_restored_one_by_one_and_audited(xtm_one_mode):
    """UX round 1 B6 on #634: a dismissal holds and is undone, never a dead end."""
    async with _scratch("hf_restore") as sessions:
        _admin, owner, mine = await _seed(sessions)
        async with sessions() as db:
            row = (
                await db.execute(
                    select(HealthCheckFinding).where(HealthCheckFinding.subject["agent_id"].astext == str(mine.id))
                )
            ).scalar_one()
        async with _client(sessions, owner) as client:
            early = await client.post(f"/api/v1/health/findings/{row.id}/restore")
            await client.post(f"/api/v1/health/findings/{row.id}/dismiss", json={"reason": "Known"})
            restored = await client.post(f"/api/v1/health/findings/{row.id}/restore")
            listing = (await client.get("/api/v1/health/findings")).json()
        async with sessions() as db:
            audits = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "health_finding"))).scalars().all()
            )

    assert early.status_code == 409
    assert restored.status_code == 200
    assert (restored.json()["status"], restored.json()["dismissal"], restored.json()["can_dismiss"]) == (
        "open",
        None,
        True,
    )
    assert listing["total"] == 1
    assert sorted(a.action for a in audits) == ["dismiss", "restore"]


@pytest.mark.asyncio
async def test_dismiss_all_and_restore_all_act_on_what_the_reader_may_manage_only(xtm_one_mode):
    async with _scratch("hf_bulk") as sessions:
        admin, owner, mine = await _seed(sessions)
        async with sessions() as db:
            rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        ids = [row.id for row in rows]
        mine_id = next(row.id for row in rows if (row.subject or {}).get("agent_id") == str(mine.id))
        async with _client(sessions, owner) as client:
            owned = (
                await client.post("/api/v1/health/findings/dismiss", json={"ids": ids, "reason": "Batch", "days": 30})
            ).json()
        async with _client(sessions, admin) as client:
            empty = await client.post("/api/v1/health/findings/dismiss", json={"ids": ids, "reason": ""})
            rest = (
                await client.post("/api/v1/health/findings/dismiss", json={"ids": [*ids, "nope"], "reason": "Batch"})
            ).json()
            dismissed = (await client.get("/api/v1/health/findings?status=dismissed")).json()
            back = (await client.post("/api/v1/health/findings/restore", json={"ids": ids})).json()
            still_open = (await client.get("/api/v1/health/findings")).json()
        async with sessions() as db:
            audits = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "health_finding"))).scalars().all()
            )

    assert [item["id"] for item in owned["items"]] == [mine_id]
    assert sorted(owned["skipped"]) == sorted(i for i in ids if i != mine_id)
    assert empty.status_code == 422
    assert len(rest["items"]) == 2
    assert sorted(rest["skipped"]) == sorted([mine_id, "nope"])
    assert dismissed["total"] == 3
    assert (len(back["items"]), back["skipped"]) == (3, [])
    assert still_open["total"] == 3
    assert sorted(a.action for a in audits) == ["dismiss"] * 3 + ["restore"] * 3


@pytest.mark.asyncio
async def test_the_history_answers_the_three_windows_only(xtm_one_mode):
    async with _scratch("hf_history") as sessions:
        admin, _owner, _mine = await _seed(sessions)
        async with _client(sessions, admin) as client:
            answers = {
                days: (await client.get(f"/api/v1/health/history?days={days}")).status_code for days in (7, 30, 90, 14)
            }

    assert answers == {7: 200, 30: 200, 90: 200, 14: 422}

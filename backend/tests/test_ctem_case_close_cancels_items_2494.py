"""A case closed by a person or an agent cancels its pending gates, loop items and timers (#2494).

The single PATCH, the bulk endpoint and the ``update_ctem_case`` tool all close a case
through ``ctem_cases_store.update_case``; a move from an open state into a closed one
now cancels what still waits on the case, with the helper a delete and a retired-asset
resolve use. A reopen (an expired acceptance) leaves the cancelled items as they are.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.ctem_case import CtemCase
from app.models.organization import Organization
from app.models.platform_timer import TIMER_ARMED, TIMER_CANCELLED, PlatformTimer
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import ctem_cases_store as store

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[Any]]:
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("ctemcaseclose", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _world(session: Any, *, state: str = "waiting_decision", run_holds: bool = False) -> dict[str, Any]:
    org = Organization(id=uuid.uuid4(), name="Org", role="primary")
    person = User(id=uuid.uuid4(), email=f"p-{uuid.uuid4().hex[:8]}@test.local", full_name="Owner", hashed_password="x")
    session.add_all([org, person])
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Loop", user_id=person.id)
    session.add(agent)
    case = CtemCase(
        organization_id=org.id,
        vertical="cyber",
        title="Upgrade Tomcat",
        remediation_kind="upgrade",
        keys=["upgrade:apache/tomcat@9.0.98"],
        pinned_issues=[],
        state=state,
        state_since=NOW,
        owner_user_id=person.id,
    )
    other = CtemCase(
        organization_id=org.id,
        vertical="cyber",
        title="Upgrade nginx",
        remediation_kind="upgrade",
        keys=["upgrade:nginx/nginx@1.27"],
        pinned_issues=[],
        state="waiting_decision",
        state_since=NOW,
    )
    session.add_all([case, other])
    await session.flush()
    gate = WorkItem(
        user_id=person.id,
        title="Approve the validation",
        correlation_key=f"ctem:validation:{case.id}",
        status="blocked",
        needs_human=True,
    )
    loop_item = WorkItem(
        user_id=person.id,
        title="Case loop",
        correlation_key=f"ctem:f1:case:{case.id}",
        status="in_progress" if run_holds else "open",
        claimed_by_run_id=uuid.uuid4() if run_holds else None,
    )
    done = WorkItem(user_id=person.id, title="old", correlation_key=f"ctem:review:{case.id}", status="done")
    kept = WorkItem(user_id=person.id, title="other", correlation_key=f"ctem:validation:{other.id}", status="blocked")
    timer = PlatformTimer(
        user_id=person.id, agent_id=agent.id, correlation_key=f"ctem-loop-reminder:{case.id}", due_at=NOW
    )
    other_timer = PlatformTimer(
        user_id=person.id, agent_id=agent.id, correlation_key=f"ctem-loop-reminder:{other.id}", due_at=NOW
    )
    session.add_all([gate, loop_item, done, kept, timer, other_timer])
    await session.commit()
    return {
        "person": str(person.id),
        "agent": str(agent.id),
        "owner": person.id,
        "case": case.id,
        "gate": gate.id,
        "loop": loop_item.id,
        "done": done.id,
        "kept": kept.id,
        "timer": timer.id,
        "other_timer": other_timer.id,
    }


async def _assert_closed_work(session: Any, world: dict[str, Any], state: str) -> None:
    gate = await session.get(WorkItem, world["gate"])
    loop_item = await session.get(WorkItem, world["loop"])
    for item in (gate, loop_item):
        assert item.status == "cancelled" and item.needs_human is False and item.completed_at is not None
        assert f"moved to {state}" in item.result and str(world["case"]) in item.result
    assert (await session.get(WorkItem, world["done"])).status == "done"
    assert (await session.get(WorkItem, world["kept"])).status == "blocked"
    timer = await session.get(PlatformTimer, world["timer"])
    assert timer.state == TIMER_CANCELLED and state in timer.outcome
    assert (await session.get(PlatformTimer, world["other_timer"])).state == TIMER_ARMED


async def test_a_person_accepting_risk_cancels_the_gate_the_loop_item_and_the_timer() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session)
        async with sessions() as session:
            case = await session.get(CtemCase, world["case"])
            await store.update_case(
                session,
                case,
                actor=world["person"],
                actor_kind="user",
                state="accepted_risk",
                owner_user_id=world["owner"],
                reason="WAF rule in place",
                accepted_until=NOW + timedelta(days=30),
                now=NOW,
            )
            await session.commit()
        async with sessions() as session:
            await _assert_closed_work(session, world, "accepted_risk")
            gate = await session.get(WorkItem, world["gate"])
            assert f"a person ({world['person']})" in gate.result


async def test_a_person_resolving_cancels_the_same_work() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session)
        async with sessions() as session:
            case = await session.get(CtemCase, world["case"])
            await store.update_case(
                session,
                case,
                actor=world["person"],
                actor_kind="user",
                state="resolved",
                evidence="CHG-1234 deployed",
                now=NOW,
            )
            await session.commit()
        async with sessions() as session:
            await _assert_closed_work(session, world, "resolved")


async def test_an_agent_closing_keeps_the_item_its_run_holds_and_cancels_the_rest() -> None:
    """The update_ctem_case path: the run answering a gate completes its own held item."""
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, run_holds=True)
        async with sessions() as session:
            case = await session.get(CtemCase, world["case"])
            await store.update_case(
                session,
                case,
                actor=world["agent"],
                actor_kind="agent",
                state="resolved",
                evidence="CHG-1234 deployed",
                now=NOW,
            )
            await session.commit()
        async with sessions() as session:
            gate = await session.get(WorkItem, world["gate"])
            assert gate.status == "cancelled" and gate.needs_human is False and "agent" in gate.result
            assert (await session.get(WorkItem, world["loop"])).status == "in_progress"
            assert (await session.get(PlatformTimer, world["timer"])).state == TIMER_CANCELLED


async def test_a_move_between_open_states_cancels_nothing() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session)
        async with sessions() as session:
            case = await session.get(CtemCase, world["case"])
            await store.update_case(
                session, case, actor=world["person"], actor_kind="user", state="remediating", now=NOW
            )
            await session.commit()
        async with sessions() as session:
            assert (await session.get(WorkItem, world["gate"])).status == "blocked"
            assert (await session.get(WorkItem, world["loop"])).status == "open"
            assert (await session.get(PlatformTimer, world["timer"])).state == TIMER_ARMED


async def test_an_expired_acceptance_reopens_the_case_and_a_new_item_stays_open() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session)
        async with sessions() as session:
            case = await session.get(CtemCase, world["case"])
            await store.update_case(
                session,
                case,
                actor=world["person"],
                actor_kind="user",
                state="accepted_risk",
                owner_user_id=world["owner"],
                reason="WAF rule in place",
                accepted_until=NOW + timedelta(days=1),
                now=NOW,
            )
            await session.commit()
        async with sessions() as session:
            assert await store.expire_acceptances(session, now=NOW + timedelta(days=2)) == 1
            await session.commit()
            fresh = WorkItem(user_id=world["owner"], title="Case loop", correlation_key=f"ctem:f1:case:{world['case']}")
            session.add(fresh)
            await session.commit()
            fresh_id = fresh.id
        async with sessions() as session:
            assert (await session.get(CtemCase, world["case"])).state == "new"
            assert (await session.get(WorkItem, fresh_id)).status == "open"
            assert (await session.get(WorkItem, world["gate"])).status == "cancelled"

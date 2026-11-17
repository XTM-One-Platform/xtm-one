"""A deactivated owner's assignments are paused unless company-managed, against
PostgreSQL.

The rule has a SQL form the discovery queries filter on and an instance form
the trigger funnels ask, and nothing is written when it applies: only a real
database can show that the two forms agree, that the scheduler's own queries
apply it, and that reactivating the account resumes everything as it was.

The cast, seeded fresh in each test:

* ``owner``: a non-admin whose PRIVATE agent is shared with a group as
  ``config``; deactivated by each test.
* ``company_owner``: the admin who created a COMPANY-MANAGED agent; also
  deactivated, which must change nothing for it.
* ``member``: an active non-admin in that group; ``admin``: an active admin;
  ``other``: an active non-admin with a private agent of their own.

Same scratch-database recipe as ``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.assignment_pause import (
    OWNER_DEACTIVATED_CODE,
    OWNER_DEACTIVATED_MESSAGE,
    OWNER_DEACTIVATED_RESUME_ERROR,
    OWNER_DEACTIVATED_RUN_ERROR,
    AssignmentOwnerDeactivatedError,
    is_paused_by_deactivated_owner,
    owner_not_deactivated,
    paused_entity_ids,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "ownerpause",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@dataclass
class World:
    users: dict[str, User]
    agents: dict[str, Agent]


async def _seed(sessionmaker: async_sessionmaker) -> World:
    async with sessionmaker() as db:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=name,
                role="admin" if name in ("admin", "company_owner") else "user",
            )
            for name in ("owner", "company_owner", "member", "admin", "other")
        }
        db.add_all(users.values())
        await db.flush()
        group = Group(name=f"team-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=users["member"].id))
        agents = {
            "private": Agent(
                name="Private agent",
                slug=f"private-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["owner"].id,
                shared_group_ids=[str(group.id)],
                shared_group_permissions={str(group.id): "config"},
            ),
            "company": Agent(
                name="Company agent",
                slug=f"company-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["company_owner"].id,
                is_company_managed=True,
            ),
            "other": Agent(
                name="Other agent",
                slug=f"other-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["other"].id,
            ),
            # Shared with a person rather than a group: paused all the same,
            # only company-managed automations keep running.
            "personal": Agent(
                name="Agent shared with a person",
                slug=f"personal-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["owner"].id,
                shared_user_ids=[str(users["member"].id)],
                shared_user_permissions={str(users["member"].id): "config"},
            ),
        }
        db.add_all(agents.values())
        await db.commit()
    return World(users=users, agents=agents)


async def _add_assignment(
    sessionmaker: async_sessionmaker, agent: Agent, **overrides
) -> Assignment:
    fields = {
        "agent_id": agent.id,
        # Owner and visibility mirror the parent agent, as every creation path
        # writes them.
        "user_id": agent.user_id,
        "is_company_managed": agent.is_company_managed,
        "name": f"A-{uuid.uuid4().hex[:6]}",
        "trigger_type": "manual",
        "trigger_config": {},
        "prompt": "Do the thing.",
        "enabled": True,
    }
    fields.update(overrides)
    assignment = Assignment(**fields)
    async with sessionmaker() as db:
        db.add(assignment)
        await db.commit()
    return assignment


async def _set_active(sessionmaker, world: World, active: bool, *names: str) -> None:
    async with sessionmaker() as db:
        for name in names:
            user = await db.get(User, world.users[name].id)
            user.is_active = active
        await db.commit()


async def _reload(sessionmaker, model, pk):
    async with sessionmaker() as db:
        return await db.get(model, pk)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [],
            "client": ("127.0.0.1", 1),
        }
    )


def _due_schedule(**overrides) -> dict:
    return {
        "trigger_type": "schedule",
        "trigger_config": {"cron": "* * * * *"},
        # Last fired an hour ago: every minute since is a due window.
        "last_triggered_at": datetime.now(UTC) - timedelta(hours=1),
        **overrides,
    }


def _assert_refused(excinfo) -> None:
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == OWNER_DEACTIVATED_CODE


# ===================================================================
# The rule itself: the three forms agree
# ===================================================================


async def test_the_sql_and_instance_forms_of_the_rule_agree():
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        cases = {
            "private of a deactivated owner": (
                await _add_assignment(sessionmaker, world.agents["private"]),
                True,
            ),
            "shared with a person, deactivated owner": (
                await _add_assignment(sessionmaker, world.agents["personal"]),
                True,
            ),
            "private of an active owner": (
                await _add_assignment(sessionmaker, world.agents["other"]),
                False,
            ),
            "company-managed of a deactivated admin": (
                await _add_assignment(sessionmaker, world.agents["company"]),
                False,
            ),
            "private without an owner": (
                await _add_assignment(sessionmaker, world.agents["private"], user_id=None),
                False,
            ),
        }
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async with sessionmaker() as db:
            runnable = set(
                (
                    await db.execute(
                        select(Assignment.id).where(owner_not_deactivated())
                    )
                )
                .scalars()
                .all()
            )
            rows = (
                (
                    await db.execute(
                        select(Assignment).where(
                            Assignment.id.in_([a.id for a, _ in cases.values()])
                        )
                    )
                )
                .scalars()
                .all()
            )
            paused = await paused_entity_ids(db, rows)
            for row in rows:
                label = next(k for k, (a, _) in cases.items() if a.id == row.id)
                expected = cases[label][1]
                assert (row.id not in runnable) is expected, label
                assert await is_paused_by_deactivated_owner(db, row) is expected, label
                assert (row.id in paused) is expected, label


# ===================================================================
# Automatic paths: the scheduler's own queries
# ===================================================================


async def test_the_schedule_tick_skips_only_the_paused_assignment_until_reactivation(
    monkeypatch,
):
    from app.services import autonomous

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(
            sessionmaker, world.agents["private"], **_due_schedule()
        )
        other = await _add_assignment(sessionmaker, world.agents["other"], **_due_schedule())
        company = await _add_assignment(
            sessionmaker, world.agents["company"], **_due_schedule()
        )
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        triggered: list[uuid.UUID] = []

        async def _record(*, assignment, **_kwargs):
            triggered.append(assignment.id)
            return str(uuid.uuid4())

        monkeypatch.setattr(autonomous, "trigger_assignment", _record)

        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)
        assert set(triggered) == {other.id, company.id}
        reloaded = await _reload(sessionmaker, Assignment, paused.id)
        assert reloaded.enabled is True
        # Nothing is stamped while paused: the window is still open afterwards.
        assert reloaded.last_triggered_at == paused.last_triggered_at

        await _set_active(sessionmaker, world, True, "owner")
        triggered.clear()
        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)
        # The window it missed fires once, as after a resume.
        assert triggered.count(paused.id) == 1
        for a in (paused, other, company):
            assert (await _reload(sessionmaker, Assignment, a.id)).enabled is True


async def test_the_work_item_inbox_leaves_the_paused_assignments_items_unclaimed(
    monkeypatch,
):
    from app.services import autonomous, work_item

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(
            sessionmaker, world.agents["private"], trigger_type="work_item"
        )
        company = await _add_assignment(
            sessionmaker, world.agents["company"], trigger_type="work_item"
        )
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        listed: list[str] = []

        async def _list(*, owner_agent_id, **_kwargs):
            listed.append(owner_agent_id)
            return [{"id": f"item-{owner_agent_id}", "correlation_key": None}]

        monkeypatch.setattr(work_item, "list_dispatchable_work_items", _list)
        monkeypatch.setattr(
            work_item, "claim_work_item", AsyncMock(return_value={"claimed": True})
        )
        monkeypatch.setattr(work_item, "release_work_item_claim", AsyncMock())
        triggered: list[uuid.UUID] = []

        async def _record(*, assignment, **_kwargs):
            triggered.append(assignment.id)
            return str(uuid.uuid4())

        monkeypatch.setattr(autonomous, "trigger_assignment", _record)

        async with sessionmaker() as db:
            await autonomous._process_work_item_assignments(db)
        # The paused inbox is not even asked for items, so none is claimed.
        assert listed == [str(world.agents["company"].id)]
        assert triggered == [company.id]

        await _set_active(sessionmaker, world, True, "owner")
        triggered.clear()
        async with sessionmaker() as db:
            await autonomous._process_work_item_assignments(db)
        assert paused.id in triggered


def _deactivating_trigger(sessionmaker, world: World):
    """The real funnel, called after the owner is deactivated: the race
    between a discovery query that saw the owner active and the trigger."""
    from app.services import assignment_engine

    real = assignment_engine.trigger_assignment

    async def _trigger(**kwargs):
        await _set_active(sessionmaker, world, False, "owner")
        return await real(**kwargs)

    return _trigger


async def test_the_schedule_tick_absorbs_an_owner_deactivated_after_its_query(
    monkeypatch, caplog
):
    from app.services import autonomous

    _stub_scheduling(monkeypatch)
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(
            sessionmaker, world.agents["private"], **_due_schedule()
        )
        monkeypatch.setattr(
            autonomous, "trigger_assignment", _deactivating_trigger(sessionmaker, world)
        )

        with caplog.at_level(logging.INFO):
            async with sessionmaker() as db:
                assert await autonomous._process_scheduled_assignments(db) == 0
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        reloaded = await _reload(sessionmaker, Assignment, paused.id)
        # The window stays open, as when the query itself skipped it.
        assert reloaded.last_triggered_at == paused.last_triggered_at
        assert reloaded.enabled is True
        async with sessionmaker() as db:
            runs = await db.execute(
                select(AssignmentRun).where(AssignmentRun.assignment_id == paused.id)
            )
            assert runs.scalars().all() == []


async def test_the_work_item_inbox_gives_back_an_item_claimed_before_the_deactivation(
    monkeypatch, caplog
):
    from app.services import autonomous, work_item
    from app.services.company_managed_writes import agent_writer

    _stub_scheduling(monkeypatch)
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        await _add_assignment(sessionmaker, world.agents["private"], trigger_type="work_item")
        monkeypatch.setattr(
            work_item,
            "list_dispatchable_work_items",
            AsyncMock(return_value=[{"id": "item-1", "correlation_key": None}]),
        )
        monkeypatch.setattr(
            work_item, "claim_work_item", AsyncMock(return_value={"claimed": True})
        )
        release = AsyncMock()
        monkeypatch.setattr(work_item, "release_work_item_claim", release)
        monkeypatch.setattr(
            autonomous, "trigger_assignment", _deactivating_trigger(sessionmaker, world)
        )

        with caplog.at_level(logging.INFO):
            async with sessionmaker() as db:
                assert await autonomous._process_work_item_assignments(db) == 0
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        # Released as the agent that claimed it.
        release.assert_awaited_once_with(
            work_item_id="item-1",
            user_id=str(world.users["owner"].id),
            writer=agent_writer(
                is_company_managed=False, agent_id=world.agents["private"].id
            ),
        )


# ===================================================================
# The trigger funnel and the paths that come through it
# ===================================================================


def _stub_scheduling(monkeypatch):
    from app.services import assignment_engine

    monkeypatch.setattr(assignment_engine, "_refuse_if_ai_disabled", AsyncMock())
    monkeypatch.setattr(assignment_engine, "_schedule_run_execution", AsyncMock())


async def test_the_trigger_funnel_refuses_before_taking_the_overlap_lock(monkeypatch):
    """Webhooks, events, chaining and delegation call it with skip_if_active."""
    from app.services import assignment_engine

    _stub_scheduling(monkeypatch)
    lock = AsyncMock()
    monkeypatch.setattr(assignment_engine, "_acquire_trigger_overlap_lock", lock)
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"])
        company = await _add_assignment(sessionmaker, world.agents["company"])
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async with sessionmaker() as db:
            with pytest.raises(AssignmentOwnerDeactivatedError) as excinfo:
                await assignment_engine.trigger_assignment(
                    paused, world.agents["private"], db=db, skip_if_active=True
                )
            _assert_refused(excinfo)
            lock.assert_not_awaited()
            run_id = await assignment_engine.trigger_assignment(
                company, world.agents["company"], db=db, skip_if_active=True
            )
            # The stubbed scheduling step is what commits a run.
            await db.commit()
        assert run_id is not None
        async with sessionmaker() as db:
            runs = (
                (
                    await db.execute(
                        select(AssignmentRun.assignment_id).where(
                            AssignmentRun.assignment_id.in_([paused.id, company.id])
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert runs == [company.id]


@pytest.mark.parametrize("caller", ["member", "admin"])
async def test_a_manual_trigger_is_refused_until_the_owner_is_reactivated(
    monkeypatch, caller
):
    """A group member and an admin alike, although the run would be theirs."""
    from app.api.v1 import assignments as assignments_api

    _stub_scheduling(monkeypatch)
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"])
        await _set_active(sessionmaker, world, False, "owner")
        user = world.users[caller]

        async with sessionmaker() as db:
            with pytest.raises(HTTPException) as excinfo:
                await assignments_api.trigger_assignment_manual(
                    str(paused.id), _request(), None, user, db, None
                )
        _assert_refused(excinfo)

        await _set_active(sessionmaker, world, True, "owner")
        async with sessionmaker() as db:
            response = await assignments_api.trigger_assignment_manual(
                str(paused.id), _request(), None, user, db, None
            )
        assert response.assignment_id == str(paused.id)
        assert (await _reload(sessionmaker, Assignment, paused.id)).enabled is True


async def test_an_admin_still_triggers_a_company_managed_assignment_of_a_deactivated_admin(
    monkeypatch,
):
    from app.api.v1 import assignments as assignments_api

    _stub_scheduling(monkeypatch)
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        company = await _add_assignment(sessionmaker, world.agents["company"])
        await _set_active(sessionmaker, world, False, "company_owner")

        async with sessionmaker() as db:
            response = await assignments_api.trigger_assignment_manual(
                str(company.id), _request(), None, world.users["admin"], db, None
            )
        assert response.assignment_id == str(company.id)


async def test_a_pending_question_cannot_be_answered_until_reactivation(monkeypatch):
    """The answer would resume the run; the question stays pending instead."""
    from app.api.v1 import assignments as assignments_api
    from app.api.v1.schemas.assignment import RespondToHumanInputRequest
    from app.services import assignment_engine

    resume = AsyncMock()
    monkeypatch.setattr(assignment_engine, "resume_assignment_run", resume)
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"])
        run = AssignmentRun(
            assignment_id=paused.id,
            agent_id=paused.agent_id,
            user_id=world.users["member"].id,
            is_company_managed=False,
            status="awaiting_human",
            human_input_question="Send it?",
        )
        async with sessionmaker() as db:
            db.add(run)
            await db.commit()
        await _set_active(sessionmaker, world, False, "owner")

        async with sessionmaker() as db:
            with pytest.raises(HTTPException) as excinfo:
                await assignments_api.respond_to_human_input(
                    str(run.id),
                    RespondToHumanInputRequest(response="yes"),
                    world.users["member"],
                    db,
                )
        _assert_refused(excinfo)
        resume.assert_not_awaited()
        still = await _reload(sessionmaker, AssignmentRun, run.id)
        assert (still.status, still.human_input_response) == ("awaiting_human", None)

        await _set_active(sessionmaker, world, True, "owner")
        async with sessionmaker() as db:
            await assignments_api.respond_to_human_input(
                str(run.id),
                RespondToHumanInputRequest(response="yes"),
                world.users["member"],
                db,
            )
        resume.assert_awaited_once()


# ===================================================================
# Runs that existed before the deactivation
# ===================================================================


class _ReachedTheQuota(BaseException):
    """Stops the engine where the quota would be charged: the gate let it by."""


async def test_a_run_created_before_the_deactivation_is_skipped_when_it_would_start(
    monkeypatch,
):
    from app.services import agentic_gate, assignment_engine, quota

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"])
        company = await _add_assignment(sessionmaker, world.agents["company"])
        runs = {}
        async with sessionmaker() as db:
            for key, a in (("paused", paused), ("company", company)):
                runs[key] = AssignmentRun(
                    assignment_id=a.id,
                    agent_id=a.agent_id,
                    user_id=a.user_id,
                    is_company_managed=a.is_company_managed,
                    status="delayed",
                )
                db.add(runs[key])
            await db.commit()
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        monkeypatch.setattr(assignment_engine, "async_session", sessionmaker)
        monkeypatch.setattr(
            agentic_gate,
            "agentic_verdict",
            AsyncMock(return_value=agentic_gate.AgenticVerdict(None)),
        )
        charge = AsyncMock(side_effect=_ReachedTheQuota)
        monkeypatch.setattr(quota, "check_agentic_quota", charge)

        assert await assignment_engine.execute_assignment_run(str(runs["paused"].id)) is None
        skipped = await _reload(sessionmaker, AssignmentRun, runs["paused"].id)
        assert skipped.status == "skipped"
        assert skipped.error == OWNER_DEACTIVATED_RUN_ERROR
        assert skipped.completed_at is not None
        assert skipped.failure_cause is None
        charge.assert_not_awaited()
        assert (await _reload(sessionmaker, Assignment, paused.id)).enabled is True

        with pytest.raises(_ReachedTheQuota):
            await assignment_engine.execute_assignment_run(str(runs["company"].id))


async def test_a_run_that_had_started_is_not_reported_as_never_run(monkeypatch):
    """A resumed run (an A2A partner's callback, a human answer accepted just
    before the deactivation, a reclaimed lease) already did part of its work:
    its record says it was stopped before resuming, not that it never ran."""
    from app.services import agentic_gate, assignment_engine

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"])
        async with sessionmaker() as db:
            run = AssignmentRun(
                assignment_id=paused.id,
                agent_id=paused.agent_id,
                user_id=paused.user_id,
                is_company_managed=False,
                status="awaiting_human",
                started_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            db.add(run)
            await db.commit()
        await _set_active(sessionmaker, world, False, "owner")
        monkeypatch.setattr(assignment_engine, "async_session", sessionmaker)
        monkeypatch.setattr(
            agentic_gate,
            "agentic_verdict",
            AsyncMock(return_value=agentic_gate.AgenticVerdict(None)),
        )

        assert await assignment_engine.execute_assignment_run(str(run.id)) is None
        stopped = await _reload(sessionmaker, AssignmentRun, run.id)
        assert (stopped.status, stopped.error) == (
            "skipped",
            OWNER_DEACTIVATED_RESUME_ERROR,
        )


@pytest.mark.parametrize("dispatched", [True, False], ids=["inbox", "payload_only"])
async def test_a_skipped_inbox_run_hands_its_work_item_back(monkeypatch, dispatched):
    """An inbox run holds its item's claim and the reaper never recovers a
    claimed item: ending the run skipped must give the item back, with no
    failed attempt counted against it. Only the item the dispatcher named:
    a payload alone, as a webhook body can write, hands nothing back."""
    from app.services import assignment_engine, work_item
    from app.services.company_managed_writes import agent_writer

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(
            sessionmaker, world.agents["private"], trigger_type="work_item"
        )
        owner_id = str(world.users["owner"].id)
        async with sessionmaker() as db:
            run = AssignmentRun(
                assignment_id=paused.id,
                agent_id=paused.agent_id,
                user_id=paused.user_id,
                is_company_managed=False,
                status="delayed",
                trigger_payload={
                    "trigger_type": "work_item",
                    "work_item_id": "item-7",
                    "work_item": {"user_id": owner_id},
                },
                trigger_context=(
                    {"type": "work_item", "work_item_id": "item-7"} if dispatched else {}
                ),
            )
            db.add(run)
            await db.commit()
        await _set_active(sessionmaker, world, False, "owner")
        monkeypatch.setattr(assignment_engine, "async_session", sessionmaker)
        release = AsyncMock()
        monkeypatch.setattr(work_item, "release_work_item_claim", release)

        assert await assignment_engine.execute_assignment_run(str(run.id)) is None
        assert (await _reload(sessionmaker, AssignmentRun, run.id)).status == "skipped"
        if not dispatched:
            release.assert_not_awaited()
            return
        release.assert_awaited_once_with(
            work_item_id="item-7",
            user_id=owner_id,
            failed=False,
            error=None,
            writer=agent_writer(is_company_managed=False, agent_id=paused.agent_id),
        )


async def test_a_queued_delivery_is_ended_with_the_reason(monkeypatch):
    from app.services import assignment_engine

    schedule = AsyncMock()
    monkeypatch.setattr(assignment_engine, "_schedule_run_execution", schedule)
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(
            sessionmaker, world.agents["private"], trigger_type="webhook"
        )
        queued = AssignmentRun(
            assignment_id=paused.id,
            agent_id=paused.agent_id,
            user_id=paused.user_id,
            is_company_managed=False,
            status=assignment_engine.QUEUED_RUN_STATUS,
            trigger_payload={"alert": 1},
        )
        async with sessionmaker() as db:
            db.add(queued)
            await db.commit()
        await _set_active(sessionmaker, world, False, "owner")

        async with sessionmaker() as db:
            assert await assignment_engine._drain_one_assignment(db, paused.id) == 0
        ended = await _reload(sessionmaker, AssignmentRun, queued.id)
        assert (ended.status, ended.error) == ("skipped", OWNER_DEACTIVATED_RUN_ERROR)
        assert ended.trigger_payload == {"alert": 1}
        schedule.assert_not_awaited()


# ===================================================================
# What the API says about it
# ===================================================================


async def test_the_admin_schedule_reads_the_whole_platform_without_a_paused_assignment():
    """The admin's Schedule tab lists every enabled assignment, a
    colleague's private one included, and still announces no run for one its
    deactivated owner has paused."""
    from app.api.v1 import assignments as assignments_api

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"], **_due_schedule())
        other = await _add_assignment(sessionmaker, world.agents["other"], **_due_schedule())
        company = await _add_assignment(sessionmaker, world.agents["company"], **_due_schedule())
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async def _scheduled() -> set[str]:
            async with sessionmaker() as db:
                rows = await assignments_api.list_assignment_schedule(world.users["admin"], db)
            return {row.assignment_id for row in rows}

        assert await _scheduled() == {str(other.id), str(company.id)}

        await _set_active(sessionmaker, world, True, "owner")
        assert await _scheduled() == {str(paused.id), str(other.id), str(company.id)}


@pytest.mark.parametrize("viewer", ["member", "admin"])
async def test_the_list_and_detail_say_the_assignment_is_paused(viewer):
    from app.api.v1 import assignments as assignments_api

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"])
        await _add_assignment(sessionmaker, world.agents["company"])
        await _set_active(sessionmaker, world, False, "owner", "company_owner")
        user = world.users[viewer]

        async with sessionmaker() as db:
            listed = await assignments_api.list_assignments(
                str(world.agents["private"].id), user, db
            )
            company = await assignments_api.list_assignments(
                str(world.agents["company"].id), user, db
            )
            detail = await assignments_api.get_assignment(str(paused.id), user, db)
        assert [a.paused_by_owner_deactivation for a in listed] == [True]
        assert [a.paused_by_owner_deactivation for a in company] == [False]
        assert detail.paused_by_owner_deactivation is True
        # The owner's own switch is untouched: it reads as it was left.
        assert detail.enabled is True

        await _set_active(sessionmaker, world, True, "owner")
        async with sessionmaker() as db:
            detail = await assignments_api.get_assignment(str(paused.id), user, db)
        assert detail.paused_by_owner_deactivation is False


async def test_the_agent_builder_read_tools_say_the_assignment_is_paused(monkeypatch):
    """An assistant asked about it reads the pause, not a bare "enabled"."""
    from app.core import database
    from app.services.builtin_tools import build_agent_builder_tool
    from app.tools.base import ToolCall

    async with _temp_database() as sessionmaker:
        monkeypatch.setattr(database, "async_session", sessionmaker)
        world = await _seed(sessionmaker)
        paused = await _add_assignment(sessionmaker, world.agents["private"])
        await _set_active(sessionmaker, world, False, "owner")

        async def _tool(name: str, **arguments: str) -> str:
            _, executor = build_agent_builder_tool(None, str(world.users["member"].id))
            result = await executor.execute_tool(
                ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
            )
            return result.content

        listed = await _tool("list_assignments", agent_id=str(paused.agent_id))
        detail = await _tool("get_assignment", assignment_id=str(paused.id))
        assert "enabled, paused: its owner's account is deactivated" in listed
        assert f"Enabled: Yes ({OWNER_DEACTIVATED_MESSAGE})" in detail

        await _set_active(sessionmaker, world, True, "owner")
        assert "paused" not in await _tool(
            "list_assignments", agent_id=str(paused.agent_id)
        )
        assert OWNER_DEACTIVATED_MESSAGE not in await _tool(
            "get_assignment", assignment_id=str(paused.id)
        )


async def test_the_deactivated_users_page_does_not_count_paused_ones_as_active():
    """The page that says the account's automations are paused does not call
    them active in the same breath; company-managed ones still count."""
    from app.api.v1 import users as users_api

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        await _add_assignment(sessionmaker, world.agents["private"])
        await _add_assignment(sessionmaker, world.agents["personal"])
        await _add_assignment(sessionmaker, world.agents["company"])
        await _add_assignment(sessionmaker, world.agents["company"])

        async def _active(name: str) -> int:
            async with sessionmaker() as db:
                stats = await users_api.get_user_stats(
                    str(world.users[name].id), world.users["admin"], db
                )
            return stats.active_assignment_count

        assert (await _active("owner"), await _active("company_owner")) == (2, 2)
        await _set_active(sessionmaker, world, False, "owner", "company_owner")
        assert (await _active("owner"), await _active("company_owner")) == (0, 2)
        await _set_active(sessionmaker, world, True, "owner")
        assert await _active("owner") == 2

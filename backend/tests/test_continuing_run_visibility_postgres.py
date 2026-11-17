"""A follow-up run is never more visible than the run it follows up, against
PostgreSQL.

``POST /assignments/runs/{id}/followup`` creates a run carrying the result of
the run it follows up. When a private agent is made company-managed after one
of its runs, following that run up used to create a company-managed run every
user could open, holding the private run's result and id. The follow-up now
takes the run's visibility, capped by the assignment's
(``continuing_run_is_company_managed``), and every other case is created as
before. A chained run follows the same rule, and is not started at all when
the user it would run as cannot open the run it continues
(``can_open_continued_run``). Driven as a NON-ADMIN owner, read back as a
non-admin stranger through the run page and the run list - an admin's bypass
would prove nothing about either. Same scratch-database recipe as
``test_run_parent_visibility_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1.assignments import followup_run, get_run_detail, list_all_runs
from app.api.v1.helpers import ContinuedRunNotOpenableError
from app.api.v1.schemas.assignment import FollowupRunRequest
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from app.services import assignment_engine
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

PRIVATE_RESULT = "The private run's result."


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("followupvis", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.fixture(autouse=True)
def _stub_scheduling(monkeypatch):
    """The run is created, never scheduled nor executed."""
    monkeypatch.setattr(assignment_engine, "_refuse_if_ai_disabled", AsyncMock())
    monkeypatch.setattr(assignment_engine, "_schedule_run_execution", AsyncMock())


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": [], "client": ("127.0.0.1", 1)})


async def _seed(session) -> dict[str, User]:
    users = {
        name: User(
            email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
            full_name=name,
            hashed_password="x",
            role="admin" if name == "admin" else "user",
        )
        for name in ("owner", "stranger", "admin")
    }
    session.add_all(users.values())
    await session.flush()
    return users


async def _assignment(session, owner: User, *, company: bool) -> Assignment:
    agent = Agent(
        name="Reporter",
        slug=f"reporter-{uuid.uuid4().hex[:8]}",
        persona="p",
        user_id=owner.id,
        is_company_managed=company,
    )
    session.add(agent)
    await session.flush()
    assignment = Assignment(
        agent_id=agent.id,
        user_id=owner.id,
        is_company_managed=company,
        name=f"Weekly report {uuid.uuid4().hex[:6]}",
        trigger_type="manual",
        trigger_config={},
        prompt="Write the report.",
        enabled=True,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def _finished_run(session, assignment: Assignment, user: User, *, company: bool):
    run = AssignmentRun(
        assignment_id=assignment.id,
        agent_id=assignment.agent_id,
        user_id=user.id,
        is_company_managed=company,
        status="completed",
        trigger_payload={},
        result_summary=PRIVATE_RESULT,
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def _set_company_managed(session, assignment: Assignment, company: bool) -> None:
    """What the company-managed toggle writes: the agent and its assignments."""
    agent = await session.get(Agent, assignment.agent_id)
    agent.is_company_managed = company
    assignment.is_company_managed = company
    await session.flush()


async def _follow_up(sessionmaker, run: AssignmentRun, user: User) -> AssignmentRun:
    async with sessionmaker() as db:
        response = await followup_run(
            str(run.id), FollowupRunRequest(prompt="And the week before?"), _request(), user, db, None
        )
    async with sessionmaker() as db:
        created = await db.get(AssignmentRun, uuid.UUID(response.id))
    # The answer says what the row holds.
    assert response.is_company_managed is created.is_company_managed
    assert created.trigger_payload["parent_run_id"] == str(run.id)
    assert created.trigger_payload["parent_result"] == PRIVATE_RESULT
    return created


async def _opens(sessionmaker, user: User, run: AssignmentRun) -> bool:
    async with sessionmaker() as db:
        try:
            await get_run_detail(str(run.id), user, db)
        except HTTPException as exc:
            assert exc.status_code == 404
            return False
        return True


async def _lists(sessionmaker, user: User, run: AssignmentRun) -> bool:
    async with sessionmaker() as db:
        rows = await list_all_runs(
            limit=500,
            offset=0,
            status=None,
            search=None,
            notifications=False,
            digests=False,
            ownership=None,
            agent_id=None,
            agent_ids=None,
            parent_run_id="none",
            a2a_integration_id=None,
            scope="visible",
            user=user,
            db=db,
        )
    return str(run.id) in {row.id for row in rows}


async def _seen_by(sessionmaker, user: User, run: AssignmentRun) -> bool:
    opens, lists = await _opens(sessionmaker, user, run), await _lists(sessionmaker, user, run)
    assert opens == lists
    return opens


async def test_a_private_run_is_followed_up_privately_after_its_agent_became_company_managed():
    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            assignment = await _assignment(session, users["owner"], company=False)
            source = await _finished_run(session, assignment, users["owner"], company=False)
            await _set_company_managed(session, assignment, True)
            await session.commit()
        owner, stranger, admin = users["owner"], users["stranger"], users["admin"]

        # The owner, not an admin, follows it up.
        followed = await _follow_up(sessionmaker, source, owner)
        assert followed.is_company_managed is False
        assert followed.user_id == owner.id
        assert not await _seen_by(sessionmaker, stranger, followed)
        assert await _seen_by(sessionmaker, owner, followed)
        assert await _opens(sessionmaker, admin, followed)

        # An admin following it up gets a run of their own, private too.
        by_admin = await _follow_up(sessionmaker, source, admin)
        assert by_admin.is_company_managed is False
        assert by_admin.user_id == admin.id
        assert not await _seen_by(sessionmaker, stranger, by_admin)

        # The assignment itself is company-managed: a run it starts from
        # nothing is, as before.
        async with sessionmaker() as db:
            fresh = await assignment_engine.trigger_assignment(
                await db.get(Assignment, assignment.id),
                await db.get(Agent, assignment.agent_id),
                payload={},
                db=db,
            )
            await db.commit()
        async with sessionmaker() as db:
            assert (await db.get(AssignmentRun, uuid.UUID(fresh))).is_company_managed is True


async def test_a_private_run_of_a_private_assignment_is_followed_up_as_before():
    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            assignment = await _assignment(session, users["owner"], company=False)
            source = await _finished_run(session, assignment, users["owner"], company=False)
            await session.commit()

        followed = await _follow_up(sessionmaker, source, users["owner"])
        assert followed.is_company_managed is False
        assert followed.user_id == users["owner"].id
        assert await _seen_by(sessionmaker, users["owner"], followed)
        assert not await _seen_by(sessionmaker, users["stranger"], followed)


async def test_a_company_managed_run_is_followed_up_company_managed_as_before():
    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            assignment = await _assignment(session, users["admin"], company=True)
            source = await _finished_run(session, assignment, users["admin"], company=True)
            await session.commit()

        # Only an admin may follow up a company-managed run.
        with pytest.raises(HTTPException) as refused:
            await _follow_up(sessionmaker, source, users["stranger"])
        assert refused.value.status_code == 403

        followed = await _follow_up(sessionmaker, source, users["admin"])
        assert followed.is_company_managed is True
        # Every user reads it, as they read the run it follows up.
        assert await _seen_by(sessionmaker, users["stranger"], source)
        assert await _seen_by(sessionmaker, users["stranger"], followed)


async def test_a_company_managed_run_of_an_assignment_made_private_is_followed_up_privately_as_before():
    """The assignment's visibility stays a ceiling: the follow-up is not made
    company-managed again because the run it follows up still is."""
    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            assignment = await _assignment(session, users["admin"], company=True)
            source = await _finished_run(session, assignment, users["admin"], company=True)
            await _set_company_managed(session, assignment, False)
            await session.commit()

        followed = await _follow_up(sessionmaker, source, users["admin"])
        assert followed.is_company_managed is False
        assert not await _seen_by(sessionmaker, users["stranger"], followed)


async def test_a_chained_run_is_owned_only_by_someone_who_can_open_the_run_it_continues():
    """A chain starts the next assignment - possibly another agent's -
    as that assignment's run-as user, through ``trigger_assignment(continues_run=...)``.
    A private run's result never reaches a run owned by someone who cannot open
    it; nothing is created instead."""
    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            private = await _finished_run(
                session,
                await _assignment(session, users["owner"], company=False),
                users["owner"],
                company=False,
            )
            shared = await _finished_run(
                session,
                await _assignment(session, users["admin"], company=True),
                users["admin"],
                company=True,
            )
            # Another agent's company-managed assignment, run as each user.
            targets = {}
            for name in ("stranger", "owner", "admin"):
                targets[name] = await _assignment(session, users["admin"], company=True)
                targets[name].run_as_user_id = users[name].id
            await session.commit()

        async def _chain(source: AssignmentRun, target: Assignment) -> AssignmentRun:
            async with sessionmaker() as db:
                run_id = await assignment_engine.trigger_assignment(
                    await db.get(Assignment, target.id),
                    await db.get(Agent, target.agent_id),
                    payload={"previous_result": source.result_summary},
                    db=db,
                    skip_if_active=True,
                    continues_run=await db.get(AssignmentRun, source.id),
                )
                await db.commit()
            async with sessionmaker() as db:
                return await db.get(AssignmentRun, uuid.UUID(run_id))

        with pytest.raises(ContinuedRunNotOpenableError):
            await _chain(private, targets["stranger"])
        async with sessionmaker() as db:
            started = await db.execute(
                select(AssignmentRun.id).where(AssignmentRun.assignment_id == targets["stranger"].id)
            )
            assert started.first() is None

        # Its own user and an admin open it: a private run of theirs.
        for name in ("owner", "admin"):
            chained = await _chain(private, targets[name])
            assert chained.is_company_managed is False
            assert chained.user_id == users[name].id
        assert not await _seen_by(sessionmaker, users["stranger"], chained)

        # Everyone opens a company-managed run: the chain runs as before.
        chained = await _chain(shared, targets["stranger"])
        assert chained.is_company_managed is True
        assert chained.user_id == users["stranger"].id

        # A deactivated identity opens nothing, its own run and as an admin
        # alike (authentication refuses it) - asked before the overlap guard,
        # which would otherwise skip these busy assignments quietly.
        async with sessionmaker() as db:
            for name in ("owner", "admin"):
                (await db.get(User, users[name].id)).is_active = False
            await db.commit()
        for name in ("owner", "admin"):
            with pytest.raises(ContinuedRunNotOpenableError):
                await _chain(private, targets[name])


class _Channels:
    """Stands in for Redis: records every channel a notice is published to."""

    def __init__(self):
        self.published: list[str] = []

    async def publish(self, channel, _payload):
        self.published.append(channel)


@pytest.fixture
def channels(monkeypatch):
    import app.core.redis as redis_module

    recorder = _Channels()

    async def _client():
        return recorder

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return recorder


async def _reported(sessionmaker):
    from app.models.activity import Activity
    from app.models.audit_log import AuditLog

    async with sessionmaker() as db:
        audits = (await db.execute(select(AuditLog).where(AuditLog.action == "continuation_refused"))).scalars().all()
        notices = (
            (await db.execute(select(Activity).where(Activity.activity_type == "assignment_continuation_refused")))
            .scalars()
            .all()
        )
    return audits, notices


def _names_no_run(run: AssignmentRun, *records) -> None:
    for record in records:
        written = repr(
            [getattr(record, field, None) for field in ("summary", "title", "details", "extra_data", "entity_id")]
        )
        assert str(run.id) not in written
        assert PRIVATE_RESULT not in written


@pytest.mark.parametrize("company", [False, True])
async def test_a_refused_chain_is_told_to_the_assignment_owner_alone(company, channels):
    """No silent drop, and no broadcast: an audit entry and an Activity
    for the owner of the assignment whose chain stopped, published to that
    owner's channel only - a company-managed assignment keeps its owner, and
    its refusal must not reach ``activity:company``. None of it names the run
    or carries its result."""
    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            source_assignment = await _assignment(session, users["owner"], company=company)
            private = await _finished_run(session, source_assignment, users["owner"], company=False)
            target = await _assignment(session, users["admin"], company=True)
            target.run_as_user_id = users["stranger"].id
            await session.commit()
            agent = await session.get(Agent, source_assignment.agent_id)

        await assignment_engine._report_refused_continuation(
            kind="chain",
            assignment=source_assignment,
            agent=agent,
            identity=users["stranger"].id,
            next_assign=target,
        )

        (audit,), (notice,) = await _reported(sessionmaker)
        assert channels.published == [f"activity:{users['owner'].id}"]
        assert audit.user_id == users["owner"].id
        assert audit.user_email.startswith("system:")
        assert audit.entity_id == source_assignment.id
        assert notice.user_id == users["owner"].id
        assert notice.extra_data == {
            "code": "continued_run_not_openable",
            "kind": "chain",
            "assignment_id": str(source_assignment.id),
            "next_assignment_id": str(target.id),
        }
        assert source_assignment.name in notice.title
        _names_no_run(private, audit, notice)


async def test_a_retry_as_a_deactivated_run_as_user_is_refused_and_told(channels):
    """A company-managed assignment runs as a user deactivated since, while its
    owner stays active: the failed run's retry would run as that user, so the
    identity check refuses it and the owner alone is told."""
    from app.api.v1.helpers import can_open_continued_run

    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            assignment = await _assignment(session, users["admin"], company=True)
            assignment.run_as_user_id = users["stranger"].id
            failed = await _finished_run(session, assignment, users["stranger"], company=True)
            failed.status = "failed"
            await session.commit()
            agent = await session.get(Agent, assignment.agent_id)

        async with sessionmaker() as db:
            assert await can_open_continued_run(db, failed, failed.user_id) is True
            (await db.get(User, users["stranger"].id)).is_active = False
            await db.commit()
        async with sessionmaker() as db:
            assert await can_open_continued_run(db, failed, failed.user_id) is False

        await assignment_engine._report_refused_continuation(
            kind="retry", assignment=assignment, agent=agent, identity=failed.user_id
        )
        (audit,), (notice,) = await _reported(sessionmaker)
        assert channels.published == [f"activity:{users['admin'].id}"]
        assert audit.user_id == notice.user_id == users["admin"].id
        assert notice.extra_data["kind"] == "retry"
        assert "next_assignment_id" not in notice.extra_data
        _names_no_run(failed, audit, notice)


async def test_an_ownerless_private_run_is_continued_only_by_an_agent_owner_who_opens_it():
    """A legacy run with no ``user_id`` acts as its agent's owner
    (``_acting_identity``): a private one, read by admins only, is not retried
    or chained as a non-admin owner, and still is as an admin."""
    from app.api.v1.helpers import can_open_continued_run

    async with _temp_database() as sessionmaker:
        async with sessionmaker() as session:
            users = await _seed(session)
            runs = {}
            for name in ("owner", "admin"):
                assignment = await _assignment(session, users[name], company=False)
                run = await _finished_run(session, assignment, users[name], company=False)
                run.user_id = None
                runs[name] = (run, await session.get(Agent, assignment.agent_id))
            await session.commit()

        async with sessionmaker() as db:
            for name, allowed in (("owner", False), ("admin", True)):
                run, agent = runs[name]
                identity = assignment_engine._acting_identity(run.user_id, agent)
                assert identity == users[name].id
                assert await can_open_continued_run(db, run, identity) is allowed

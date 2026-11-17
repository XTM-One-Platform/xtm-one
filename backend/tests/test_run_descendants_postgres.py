"""The run page's delegation tree, as ``GET /runs/{id}/descendants`` walks it.

The run page and the Activity tab rebuild the tree from the rows this route
returns (``orderRunTree``), so the walk must end on any ``parent_run_id`` chain
- the column is a plain foreign key, and nothing stops a corrupted chain from
looping back to its root - and list only the rows the caller could open.

Which rows a recursive query reaches is decided by running it, so it is run
here. Same scratch-database recipe as ``test_a2a_cancel_child_runs_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.api.v1.assignments import get_run_descendants
from app.models.assignment import AssignmentRun
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("rundesc", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_user(session, name: str):
    from app.models.agent import Agent
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name,
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name=f"{name} agent", user_id=user.id)
    session.add(agent)
    await session.flush()
    user.agent_id = agent.id
    return user


async def _run(session, user, *, parent=None, company=False):
    row = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=user.agent_id,
        user_id=user.id,
        parent_run_id=parent,
        status="running",
        is_company_managed=company,
        started_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


def _caller(user, *, admin: bool = False):
    return SimpleNamespace(id=user.id, is_admin=admin)


async def _listed_counts(caller, session, *, parent_run_id: str = "none") -> dict[str, int]:
    """``child_run_count`` of every run ``GET /runs/all`` lists *caller*."""
    from app.api.v1.assignments import list_all_runs

    rows = await list_all_runs(
        limit=50,
        offset=0,
        status=None,
        search=None,
        notifications=False,
        digests=False,
        ownership=None,
        agent_id=None,
        agent_ids=None,
        parent_run_id=parent_run_id,
        a2a_integration_id=None,
        scope="visible",
        user=caller,
        db=session,
    )
    return {row.id: row.child_run_count for row in rows}


async def _assert_badges_count_the_list(caller, session, counts: dict[str, int]) -> None:
    """Both badges of a run count the rows ``/descendants`` lists the caller."""
    from app.api.v1.assignments import get_run_detail

    for run_id, listed in counts.items():
        rows = await get_run_descendants(run_id, caller, session)
        detail = await get_run_detail(run_id, caller, session)
        assert listed == detail.child_run_count == len(rows), run_id


async def test_a_chain_that_loops_back_to_its_root_ends_and_never_lists_the_root():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session, "owner")
        root = await _run(session, user)
        child = await _run(session, user, parent=root.id)
        grandchild = await _run(session, user, parent=child.id)
        await session.execute(
            update(AssignmentRun)
            .where(AssignmentRun.id == root.id)
            .values(parent_run_id=grandchild.id)
        )
        await session.commit()
        # A walk that never ends is a statement the database has to cut.
        await session.execute(text("SET statement_timeout = '10s'"))

        rows = await get_run_descendants(str(root.id), _caller(user), session)

        assert {row.id for row in rows} == {str(child.id), str(grandchild.id)}
        assert len(rows) == 2


async def test_a_caller_is_listed_only_the_rows_they_could_open():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session, "owner")
        stranger = await _seed_user(session, "stranger")
        root = await _run(session, owner, company=True)
        shared = await _run(session, owner, parent=root.id, company=True)
        private = await _run(session, owner, parent=shared.id)
        below_private = await _run(session, owner, parent=private.id, company=True)
        await session.commit()

        seen_by_stranger = await get_run_descendants(
            str(root.id), _caller(stranger), session
        )
        seen_by_owner = await get_run_descendants(str(root.id), _caller(owner), session)
        seen_by_admin = await get_run_descendants(
            str(root.id), _caller(stranger, admin=True), session
        )

        everything = {str(shared.id), str(private.id), str(below_private.id)}
        assert {row.id for row in seen_by_stranger} == {str(shared.id), str(below_private.id)}
        assert {row.id for row in seen_by_owner} == everything
        assert {row.id for row in seen_by_admin} == everything
        # The hidden run is not even named as a parent: no id, no agent.
        orphan = next(row for row in seen_by_stranger if row.id == str(below_private.id))
        assert orphan.parent_run_id is None
        assert orphan.parent_agent_name is None and orphan.parent_agent_icon is None
        named = next(row for row in seen_by_owner if row.id == str(below_private.id))
        assert named.parent_run_id == str(private.id)
        assert named.parent_agent_name == "owner agent"

        # The badges of the run page and of the run list count what the list
        # shows each of them: the hidden run is not counted.
        for caller, expected in (
            (_caller(stranger), 2),
            (_caller(owner), 3),
            (_caller(stranger, admin=True), 3),
        ):
            counts = await _listed_counts(caller, session)
            assert counts == {str(root.id): expected}
            await _assert_badges_count_the_list(caller, session, counts)

        # Listed with the runs below it, each run is counted what is listed
        # under it; the stranger is never listed the hidden run.
        by_stranger = await _listed_counts(_caller(stranger), session, parent_run_id="")
        assert by_stranger == {str(root.id): 2, str(shared.id): 1, str(below_private.id): 0}
        await _assert_badges_count_the_list(_caller(stranger), session, by_stranger)
        by_owner = await _listed_counts(_caller(owner), session, parent_run_id="")
        assert by_owner == {
            str(root.id): 3,
            str(shared.id): 2,
            str(private.id): 1,
            str(below_private.id): 0,
        }
        await _assert_badges_count_the_list(_caller(owner), session, by_owner)


async def test_the_badges_count_a_looping_tree_once():
    """Both badges count the descendants the page lists: a chain that loops
    back to its root must end, and the root is not counted."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session, "owner")
        root = await _run(session, user)
        child = await _run(session, user, parent=root.id)
        grandchild = await _run(session, user, parent=child.id)
        await session.execute(
            update(AssignmentRun)
            .where(AssignmentRun.id == root.id)
            .values(parent_run_id=grandchild.id)
        )
        await session.commit()
        await session.execute(text("SET statement_timeout = '10s'"))

        # No run of a loop is top-level: listed without the parent filter,
        # each of the three is counted the two others.
        for caller in (_caller(user), _caller(user, admin=True)):
            counts = await _listed_counts(caller, session, parent_run_id="")
            assert counts == {str(root.id): 2, str(child.id): 2, str(grandchild.id): 2}
            await _assert_badges_count_the_list(caller, session, counts)


async def test_a_tree_larger_than_a_statement_can_bind_is_listed_whole():
    """More descendants than one statement can carry parameters (asyncpg stops
    at 32,767): the list is read in one walk, never as an ``IN`` of every id,
    and says what the badge counts."""
    from app.api.v1.assignments import get_run_detail

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session, "owner")
        root = await _run(session, user)
        await session.execute(
            text("""
                INSERT INTO assignment_runs
                SELECT copy.* FROM assignment_runs t, generate_series(1, 33000) AS n,
                LATERAL jsonb_populate_record(t, jsonb_build_object(
                    'id', md5(t.id::text || n)::uuid, 'parent_run_id', t.id
                )) AS copy
                WHERE t.id = :root
            """),
            {"root": root.id},
        )
        await session.commit()

        rows = await get_run_descendants(str(root.id), _caller(user), session)
        detail = await get_run_detail(str(root.id), _caller(user), session)

        assert len(rows) == detail.child_run_count == 33000

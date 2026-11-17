"""Cancelling a run reaches the runs it handed the turn to.

A mandatory transfer creates a child ``AssignmentRun`` for the next agent.
``mark_sub_agent_tasks_cancelled_for_runs`` matches ``SubAgentTask`` and
``spawn_run_cancel`` matches ``A2ADelegation``; a transfer child is neither, so
without this it executes under a parent that has stopped — and transfers again
of its own accord, which is why the walk is recursive.

The descendants are also what the other two cascade arms are given: both are
scoped by run id, so a child's own sub-agents and delegations are out of reach
until the tree is known.

Which rows a recursive query reaches is decided by running it, so it is run
here. Same scratch-database recipe as ``test_a2a_hooks_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.models.assignment import AssignmentRun
from app.services.orchestration_engine import cancel_child_runs
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("a2akids", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_user(session):
    """A user and an agent: ``agent_id`` is NOT NULL on a run."""
    from app.models.agent import Agent
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Cancel Children Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Runner", user_id=user.id)
    session.add(agent)
    await session.flush()
    user.agent_id = agent.id
    return user


async def _run(session, user, *, parent=None, status="running"):
    row = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=user.agent_id,
        user_id=user.id,
        parent_run_id=parent,
        status=status,
        started_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def _status(session, row_id):
    return (
        await session.execute(
            select(AssignmentRun.status)
            .where(AssignmentRun.id == row_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


class TestTheChainIsReached:
    async def test_a_child_and_its_own_child_are_cancelled(self):
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            parent = await _run(session, user)
            child = await _run(session, user, parent=parent.id)
            grandchild = await _run(session, user, parent=child.id)
            await session.commit()

            assert set(await cancel_child_runs(session, [parent.id])) == {
                child.id,
                grandchild.id,
            }
            await session.commit()

            assert await _status(session, child.id) == "cancelled"
            assert await _status(session, grandchild.id) == "cancelled", (
                "one level is not the chain"
            )

    async def test_a_finished_child_is_left_as_it_ended(self):
        """Cancelling must not rewrite how work that already finished ended."""
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            parent = await _run(session, user)
            done = await _run(session, user, parent=parent.id, status="completed")
            failed = await _run(session, user, parent=parent.id, status="failed")
            await session.commit()

            # Reported, not cancelled: the caller's other cascade arms are
            # scoped by run id, and a finished child may still have left a
            # partner working.
            assert set(await cancel_child_runs(session, [parent.id])) == {
                done.id,
                failed.id,
            }
            await session.commit()

            assert await _status(session, done.id) == "completed"
            assert await _status(session, failed.id) == "failed"

    async def test_another_run_s_children_are_not_touched(self):
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            parent = await _run(session, user)
            stranger = await _run(session, user)
            theirs = await _run(session, user, parent=stranger.id)
            await session.commit()

            await cancel_child_runs(session, [parent.id])
            await session.commit()

            assert await _status(session, theirs.id) == "running"

    async def test_nothing_to_cancel_is_not_an_error(self):
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            lonely = await _run(session, user)
            await session.commit()
            assert await cancel_child_runs(session, [lonely.id]) == []
            assert await cancel_child_runs(session, []) == []


class TestTheCascadeFeedsTheTreeToItsOtherArms:
    """The contract the ordering comment in ``cascade_cancel`` rests on.

    ``cancel_child_runs`` is covered above in isolation, and the other arms are
    covered with it stubbed. Neither pins the join: that the descendants it
    returns are what the sub-agent arm is then given. A grandchild's
    ``SubAgentTask`` is the end of that chain, so it is asserted here against a
    real database rather than against a stub agreeing with itself.
    """

    async def _task_for(self, session, user, run, status="running"):
        from app.models.sub_agent_task import SubAgentTask

        row = SubAgentTask(
            id=uuid.uuid4(),
            user_id=user.id,
            parent_agent_id=user.agent_id,
            target_agent_id=user.agent_id,
            task_description="work a grandchild started",
            status=status,
            metadata_json={"parent_run_id": str(run.id)},
        )
        session.add(row)
        await session.flush()
        return row

    async def test_a_grandchild_s_background_task_is_cancelled(self):
        from app.a2a.tasks import cascade_cancel
        from app.models.sub_agent_task import SubAgentTask

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            parent = await _run(session, user)
            child = await _run(session, user, parent=parent.id)
            grandchild = await _run(session, user, parent=child.id)
            deep = await self._task_for(session, user, grandchild)
            await session.commit()

            reached = await cascade_cancel(session, [parent.id])
            await session.commit()

            assert set(reached) == {parent.id, child.id, grandchild.id}, (
                "the tree the partner arm is handed is not the whole tree"
            )
            assert (
                await session.execute(
                    select(SubAgentTask.status)
                    .where(SubAgentTask.id == deep.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one() == "cancelled"

    async def test_another_run_s_background_task_is_untouched(self):
        from app.a2a.tasks import cascade_cancel
        from app.models.sub_agent_task import SubAgentTask

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            parent = await _run(session, user)
            stranger = await _run(session, user)
            theirs = await self._task_for(session, user, stranger)
            await session.commit()

            await cascade_cancel(session, [parent.id])
            await session.commit()

            assert (
                await session.execute(
                    select(SubAgentTask.status)
                    .where(SubAgentTask.id == theirs.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one() == "running"


class TestTheWalkIsBounded:
    """``UNION`` stops a cycle, but nothing constrains how deep
    ``parent_run_id`` may chain — and the reaper walks this for a whole
    batch. A pathological tree should degrade, not pin a connection.

    The cap counts GENERATIONS, not rows, so both shapes are held: a chain
    longer than the cap is cut at it, and a generation wider than the cap is
    not cut at all.
    """

    async def test_a_chain_deeper_than_the_cap_stops_at_it(self):
        from app.services.orchestration_engine import _MAX_RUN_TREE_DEPTH

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            root = await _run(session, user)
            parent = root
            for _ in range(_MAX_RUN_TREE_DEPTH + 5):
                parent = await _run(session, user, parent=parent.id)
            await session.commit()

            reached = await cancel_child_runs(session, [root.id])
            await session.commit()

            # One run per generation on a linear chain, so the row count is
            # the depth here — which is exactly why the wide case below is
            # needed as well.
            assert len(reached) == _MAX_RUN_TREE_DEPTH

    async def test_a_generation_wider_than_the_cap_is_not_truncated(self):
        """The cap bounds depth. A wide tree is shallow, so every sibling is
        reached however many there are — asserting a row count against the cap
        would have made this look like a truncation."""
        from app.services.orchestration_engine import _MAX_RUN_TREE_DEPTH

        width = _MAX_RUN_TREE_DEPTH + 5
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            root = await _run(session, user)
            for _ in range(width):
                await _run(session, user, parent=root.id)
            await session.commit()

            assert len(await cancel_child_runs(session, [root.id])) == width

    async def test_a_chain_within_the_cap_is_reached_whole(self):
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            root = await _run(session, user)
            parent = root
            made = []
            for _ in range(5):
                parent = await _run(session, user, parent=parent.id)
                made.append(parent.id)
            await session.commit()

            assert set(await cancel_child_runs(session, [root.id])) == set(made)

    async def test_hitting_the_cap_is_said_out_loud(self, caplog):
        """Runs below the cap keep going and their partners are never told, so
        the task reports CANCELED over work that is still running. Truncating
        is the right trade; doing it in silence is not."""
        import logging

        from app.services.orchestration_engine import _MAX_RUN_TREE_DEPTH

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            root = await _run(session, user)
            parent = root
            for _ in range(_MAX_RUN_TREE_DEPTH + 1):
                parent = await _run(session, user, parent=parent.id)
            await session.commit()

            with caplog.at_level(logging.WARNING):
                await cancel_child_runs(session, [root.id])
            assert "depth cap" in caplog.text

    async def test_a_tree_inside_the_cap_says_nothing(self, caplog):
        import logging

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user = await _seed_user(session)
            root = await _run(session, user)
            await _run(session, user, parent=root.id)
            await session.commit()

            with caplog.at_level(logging.WARNING):
                await cancel_child_runs(session, [root.id])
            assert "depth cap" not in caplog.text

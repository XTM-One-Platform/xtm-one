"""Deleting a live A2A run queues behind a cancel or a claim of it.

A cancel, a claim and the terminal write take a task row before its run row.
Deleting the run - one run, in bulk, a purge, the agent, or the user it runs
as - writes the run and then the task inside one DELETE statement (the task's
``run_id`` is ``ON DELETE SET NULL``, and an agent's or a user's tasks go with
their exposures and contexts), so without its own lock on the tasks first a
deletion racing one of them on the same live task deadlocked, and PostgreSQL
aborted one side.

There is no point between those two writes to hold the deletion at, so the
other writer is held instead: right after it has taken the task row, a real
deletion route is started against the same run, and only once PostgreSQL
reports it queued on a lock is the writer released. Same scratch-database
recipe as ``test_a2a_cancel_lock_order_postgres.py``.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.selectable import Select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.a2a import tasks as task_svc
from app.api.v1 import agents as agents_module
from app.api.v1 import assignments as assignments_api
from app.api.v1 import users as users_module
from app.api.v1.schemas.assignment import BulkDeleteRunsRequest
from app.models.a2a import A2AContext, A2AExposure, A2ATask
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.user import User
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_database

pytestmark = pytest.mark.integration

_CLIENT_ID = "partner-delete-lock-order"
#: Several times PostgreSQL's default ``deadlock_timeout`` (1 s), so a lock
#: inversion is settled by the deadlock detector well before a test gives up.
_SETTLE_SECONDS = 30


@dataclass(frozen=True)
class _Seeded:
    user_id: uuid.UUID
    admin_id: uuid.UUID
    agent_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    exposure_id: uuid.UUID
    context_id: uuid.UUID


@asynccontextmanager
async def _temp_database() -> AsyncIterator[ScratchDatabase]:
    async with scratch_database("a2adel", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db


async def _seed(session, *, run_status: str) -> _Seeded:
    """An owner's exposed agent, a partner's task mirroring one run, and an admin."""
    admin = User(
        id=uuid.uuid4(),
        email=f"a2a-admin-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Acting Admin",
        hashed_password="x",
        role="admin",
    )
    user = User(
        id=uuid.uuid4(),
        email=f"a2a-owner-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Delete Lock Order Test",
        hashed_password="x",
    )
    session.add_all([admin, user])
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Exposed", user_id=user.id)
    session.add(agent)
    await session.flush()
    exposure = A2AExposure(agent_id=agent.id, card_token=secrets.token_urlsafe(24), run_as_user_id=user.id)
    session.add(exposure)
    await session.flush()
    context = A2AContext(client_id=_CLIENT_ID, responsible_agent_id=agent.id, exposure_id=exposure.id)
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=agent.id,
        user_id=user.id,
        status=run_status,
        started_at=datetime.now(UTC) if run_status == "running" else None,
        trigger_payload={"message": "Say hello."},
        trigger_context={"source": "a2a", "client_id": _CLIENT_ID},
    )
    session.add_all([context, run])
    await session.flush()
    task = A2ATask(
        exposure_id=exposure.id,
        client_id=_CLIENT_ID,
        run_id=run.id,
        message_id=f"e2e-{uuid.uuid4().hex}",
        inbound_parts=[{"kind": "text", "text": "Say hello."}],
        lease_heartbeat_at=datetime.now(UTC),
        context_id=context.id,
    )
    session.add(task)
    await session.commit()
    return _Seeded(user.id, admin.id, agent.id, run.id, task.id, exposure.id, context.id)


async def _add_task(session, seeded: _Seeded) -> tuple[uuid.UUID, uuid.UUID]:
    """Another live task of the same partner and agent. Returns ``(task_id, run_id)``."""
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=seeded.agent_id,
        user_id=seeded.user_id,
        status="running",
        started_at=datetime.now(UTC),
        trigger_payload={"message": "Say it again."},
        trigger_context={"source": "a2a", "client_id": _CLIENT_ID},
    )
    session.add(run)
    await session.flush()
    task = A2ATask(
        exposure_id=seeded.exposure_id,
        client_id=_CLIENT_ID,
        run_id=run.id,
        message_id=f"e2e-{uuid.uuid4().hex}",
        inbound_parts=[{"kind": "text", "text": "Say it again."}],
        lease_heartbeat_at=datetime.now(UTC),
        context_id=seeded.context_id,
    )
    session.add(task)
    await session.commit()
    return task.id, run.id


class _Gate:
    def __init__(self) -> None:
        self.holding = asyncio.Event()
        self.release = asyncio.Event()


class _HeldAfterItsFirstLock:
    """A session whose transaction stops, still open, once it holds a row.

    The first row it locks - with ``SELECT ... FOR ...``, an UPDATE, or a flush
    of a changed object - then stays locked by an open transaction.
    Everything else is the real session's.
    """

    def __init__(self, session, gate: _Gate) -> None:
        self._session = session
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def _hold_once(self) -> None:
        if not self._gate.holding.is_set():
            self._gate.holding.set()
            await self._gate.release.wait()

    async def flush(self, *args, **kwargs):
        wrote = bool(self._session.new or self._session.dirty or self._session.deleted)
        await self._session.flush(*args, **kwargs)
        if wrote:
            await self._hold_once()

    async def execute(self, statement, *args, **kwargs):
        result = await self._session.execute(statement, *args, **kwargs)
        locked = isinstance(statement, Select) and statement._for_update_arg is not None
        if locked or isinstance(statement, UpdateBase):
            await self._hold_once()
        return result


async def _nothing(*_args, **_kwargs) -> None:
    return None


async def _queued_backend(db: ScratchDatabase) -> int | None:
    """The backend of this database that comes to wait on a lock, if any does."""
    async with db.engine.connect() as observer:
        for _ in range(150):
            pid = (
                await observer.execute(
                    text(
                        "SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE NOT l.granted AND a.datname = current_database() LIMIT 1"
                    )
                )
            ).scalar_one_or_none()
            # pg_stat_activity is read once per transaction: a backend that
            # connected after the first poll would never join a later one.
            await observer.rollback()
            if pid is not None:
                return pid
            await asyncio.sleep(0.1)
    return None


async def _has_updated_runs(db: ScratchDatabase, pid: int) -> bool:
    """Whether backend *pid*'s open transaction holds a write lock on assignment_runs."""
    async with db.engine.connect() as observer:
        return bool(
            (
                await observer.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE pid = :pid AND granted "
                        "AND locktype = 'relation' AND mode = 'RowExclusiveLock' "
                        "AND relation = 'assignment_runs'::regclass"
                    ),
                    {"pid": pid},
                )
            ).scalar_one()
        )


async def _holds_run(db: ScratchDatabase, pid: int, run_id: uuid.UUID) -> bool:
    """Whether backend *pid*'s open transaction has locked or deleted the run row."""
    async with db.engine.connect() as observer:
        return bool(
            (
                await observer.execute(
                    text(
                        "SELECT (SELECT xmax::text FROM assignment_runs WHERE id = :run) "
                        "= (SELECT backend_xid::text FROM pg_stat_activity WHERE pid = :pid)"
                    ),
                    {"run": run_id, "pid": pid},
                )
            ).scalar_one()
        )


async def _cancel(db: ScratchDatabase, task_id: uuid.UUID, gate: _Gate) -> bool:
    """What the ``tasks/cancel`` route does, held once it has the task row."""
    async with db.sessions() as partner:
        task = await task_svc.get_task(partner, task_id, _CLIENT_ID)
        cancelled = await task_svc.request_cancel(_HeldAfterItsFirstLock(partner, gate), task)
        await partner.commit()
        return cancelled


# -- the deletion routes, each as its route runs it ------------------------------


async def _delete_one_run(db: ScratchDatabase, seeded: _Seeded) -> None:
    async with db.sessions() as session:
        owner = await session.get(User, seeded.user_id)
        await assignments_api.delete_run(run_id=str(seeded.run_id), user=owner, db=session)


async def _delete_in_bulk(db: ScratchDatabase, seeded: _Seeded) -> None:
    async with db.sessions() as session:
        owner = await session.get(User, seeded.user_id)
        await assignments_api.bulk_delete_runs(
            BulkDeleteRunsRequest(run_ids=[str(seeded.run_id)]), user=owner, db=session
        )
        # ``get_db`` commits this route's transaction on teardown.
        await session.commit()


async def _purge(db: ScratchDatabase, seeded: _Seeded) -> None:
    async with db.sessions() as session:
        owner = await session.get(User, seeded.user_id)
        await assignments_api.purge_all_runs(user=owner, db=session)


async def _delete_the_agent(db: ScratchDatabase, seeded: _Seeded) -> None:
    async with db.sessions() as session:
        owner = await session.get(User, seeded.user_id)
        await agents_module.delete_agent(
            agent_id=str(seeded.agent_id), request=None, user=owner, db=session, _ai_gate=None
        )


async def _delete_the_user(db: ScratchDatabase, seeded: _Seeded) -> None:
    async with db.sessions() as session:
        admin = await session.get(User, seeded.admin_id)
        await users_module.delete_user(user_id=str(seeded.user_id), request=None, admin=admin, db=session)


_DELETIONS = {
    "one run": _delete_one_run,
    "in bulk": _delete_in_bulk,
    "purge": _purge,
    "the agent": _delete_the_agent,
    "the user": _delete_the_user,
}


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """The memory clean-up after a deletion commits is Redis's, not this test's."""
    monkeypatch.setattr("app.services.agent_memory.clear_deleted_agent_memory", _nothing)
    monkeypatch.setattr(users_module, "_clear_deleted_user_memory", _nothing)
    monkeypatch.setattr("app.services.quota.invalidate_membership_quota_cache", _nothing)
    monkeypatch.setattr(task_svc, "announce_state", _nothing)
    monkeypatch.setattr(task_svc, "_TERMINAL_WRITE_BACKOFF_SECONDS", 0)


async def _race(db: ScratchDatabase, gate: _Gate, writer, deletion):
    """Start *writer*, and *deletion* once the writer holds its first row.

    Returns ``(queued, deletion wrote the runs first, writer outcome, deletion outcome)``.
    """
    writing = asyncio.create_task(writer)
    deleting = None
    queued = wrote_runs_first = False
    try:
        await asyncio.wait_for(gate.holding.wait(), _SETTLE_SECONDS)
        deleting = asyncio.create_task(deletion)
        pid = await _queued_backend(db)
        queued = pid is not None
        wrote_runs_first = queued and await _has_updated_runs(db, pid)
    finally:
        gate.release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(*(t for t in (writing, deleting) if t is not None), return_exceptions=True),
            _SETTLE_SECONDS,
        )
    if deleting is None:
        deletion.close()
    return queued, wrote_runs_first, *outcomes


class TestADeletionRacingACancel:
    @pytest.mark.parametrize("route", sorted(_DELETIONS))
    async def test_the_deletion_waits_for_the_cancel_instead_of_deadlocking_with_it(self, route):
        async with _temp_database() as db:
            async with db.sessions() as seed:
                seeded = await _seed(seed, run_status="running")
            gate = _Gate()
            queued, wrote_runs_first, answered, deleted = await _race(
                db, gate, _cancel(db, seeded.task_id, gate), _DELETIONS[route](db, seeded)
            )

            assert queued, "the deletion never waited for the cancel it raced"
            assert not wrote_runs_first, (
                "the deletion wrote the run row before it had the task row, the reverse of the cancel's order"
            )
            assert not isinstance(answered, BaseException), f"the cancel failed: {answered!r}"
            assert not isinstance(deleted, BaseException), f"the deletion failed: {deleted!r}"
            assert answered is True, "the cancel held the task first; it reaches the live run"
            async with db.sessions() as check:
                run = await check.get(AssignmentRun, seeded.run_id)
                assert run is None, "the deletion did not delete the run once the cancel was done"


class TestATaskCreatedDuringAPurge:
    async def test_it_is_held_first_too(self):
        """A purge deletes by filter, so it also removes a run created after it
        took its first lock. That run's task is taken again before the DELETE,
        so a cancel of it arriving meanwhile is waited for, not deadlocked with."""
        async with _temp_database() as db:
            async with db.sessions() as seed:
                seeded = await _seed(seed, run_status="running")
            purge_gate, cancel_gate = _Gate(), _Gate()

            async def _purge_held() -> None:
                async with db.sessions() as session:
                    owner = await session.get(User, seeded.user_id)
                    await assignments_api.purge_all_runs(user=owner, db=_HeldAfterItsFirstLock(session, purge_gate))

            purging = asyncio.create_task(_purge_held())
            cancelling = None
            late_run_id = None
            queued = holds_late_run = False
            try:
                await asyncio.wait_for(purge_gate.holding.wait(), _SETTLE_SECONDS)
                async with db.sessions() as seed:
                    late_task_id, late_run_id = await _add_task(seed, seeded)
                cancelling = asyncio.create_task(_cancel(db, late_task_id, cancel_gate))
                await asyncio.wait_for(cancel_gate.holding.wait(), _SETTLE_SECONDS)
                purge_gate.release.set()
                pid = await _queued_backend(db)
                queued = pid is not None
                holds_late_run = queued and await _holds_run(db, pid, late_run_id)
            finally:
                purge_gate.release.set()
                cancel_gate.release.set()
                outcomes = await asyncio.wait_for(
                    asyncio.gather(*(t for t in (purging, cancelling) if t is not None), return_exceptions=True),
                    _SETTLE_SECONDS,
                )

            assert queued, "the purge never waited for the cancel of the late task"
            assert not holds_late_run, "the purge took the late run's row before its task's"
            purged, answered = outcomes
            assert not isinstance(purged, BaseException), f"the purge failed: {purged!r}"
            assert not isinstance(answered, BaseException), f"the cancel failed: {answered!r}"
            assert answered is True, "the cancel held the late task first; it reaches its live run"
            async with db.sessions() as check:
                assert await check.get(AssignmentRun, seeded.run_id) is None
                assert await check.get(AssignmentRun, late_run_id) is None, "the late run survived the purge"


class TestADeletionRacingTheClaim:
    async def test_the_deletion_waits_for_the_claim_instead_of_deadlocking_with_it(self, monkeypatch):
        """A run deleted the moment the executor claims it."""
        deletion_returned = asyncio.Event()

        async def _answers(**_kwargs):
            # Not before the deletion has returned: the terminal write takes the
            # task row too, and which of the two got it first would otherwise
            # decide the outcome instead of the claim.
            await asyncio.wait_for(deletion_returned.wait(), _SETTLE_SECONDS)
            return "The answer.", None

        monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _answers)
        monkeypatch.setattr(task_svc, "_supervise_run", _nothing)

        async with _temp_database() as db:
            async with db.sessions() as seed:
                seeded = await _seed(seed, run_status="pending")
            gate = _Gate()
            opened = 0

            @asynccontextmanager
            async def _executor_session():
                async with db.sessions() as session:
                    yield _HeldAfterItsFirstLock(session, gate)

            def _sessions():
                # The executor's own session is the first one opened; a retry
                # of its terminal write would open plain ones after it.
                nonlocal opened
                opened += 1
                return _executor_session() if opened == 1 else db.sessions()

            monkeypatch.setattr("app.core.database.async_session", _sessions)

            async def _delete_then_answer() -> None:
                try:
                    await _delete_one_run(db, seeded)
                finally:
                    deletion_returned.set()

            queued, wrote_runs_first, executed, deleted = await _race(
                db, gate, task_svc.execute_task_run(seeded.task_id), _delete_then_answer()
            )
            deletion_returned.set()

            assert queued, "the deletion never waited for the claim it raced"
            assert not wrote_runs_first, (
                "the deletion wrote the run row before it had the task row, the reverse of the claim's order"
            )
            assert not isinstance(executed, BaseException), f"the executor raised: {executed!r}"
            assert not isinstance(deleted, BaseException), f"the deletion failed: {deleted!r}"
            async with db.sessions() as check:
                assert await check.get(AssignmentRun, seeded.run_id) is None
                task = (await check.execute(select(A2ATask).where(A2ATask.id == seeded.task_id))).scalar_one()
                assert task.run_id is None

"""A task cancel and the run it cancels take the two rows in one order.

``tasks/cancel`` writes two rows: the run (``status='cancelled'``) and the task
(``cancel_requested_at``). The executor writes the same two when it claims the
run and when it records the run's terminal state. The terminal write and the
reaper take the task row first; a cancel that took the run row first and then
waited for the task deadlocked with a run finalising at that moment, and
PostgreSQL aborted one of the two: the partner got a 500 for its cancel, or the
run lost the answer it had already paid for.

Which transaction waits for which is decided by the database, so it is run
here: one writer is held right after its first write, the other is started
against the same task, and only once it is queued on a lock is the first one
released. Same scratch-database recipe as
``test_a2a_cancel_child_runs_postgres.py``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.sql.dml import UpdateBase

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.a2a import tasks as task_svc
from app.models.a2a import A2AContext, A2AExposure, A2ATask
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.user import User
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_database

pytestmark = pytest.mark.integration

_CLIENT_ID = "partner-lock-order"
#: Several times PostgreSQL's default ``deadlock_timeout`` (1 s), so a lock
#: inversion is settled by the deadlock detector well before a test gives up.
_SETTLE_SECONDS = 30
_TERMINAL_WRITE_FAILED = "A2A terminal write failed"


@asynccontextmanager
async def _temp_database() -> AsyncIterator[ScratchDatabase]:
    async with scratch_database("a2alock", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db


async def _seed(session, *, run_status: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A partner's task mirroring one run. Returns ``(task_id, run_id)``."""
    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Cancel Lock Order Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Exposed", user_id=user.id)
    session.add(agent)
    await session.flush()
    exposure = A2AExposure(
        agent_id=agent.id,
        card_token=secrets.token_urlsafe(24),
        run_as_user_id=user.id,
    )
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
    return task.id, run.id


class _Gate:
    def __init__(self) -> None:
        self.holding = asyncio.Event()
        self.release = asyncio.Event()


class _HeldAfterItsFirstWrite:
    """A session whose transaction stops, still open, once it has written a row.

    Whichever row the writer writes first - the task or the run - is then held
    by an open transaction: the moment a writer taking the other row first
    would deadlock with it. Everything else is the real session's.
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
        if isinstance(statement, UpdateBase):
            await self._hold_once()
        return result


async def _nothing(*_args, **_kwargs) -> None:
    return None


async def _backend_pid(session) -> int:
    return (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()


async def _queued_on_a_lock(db: ScratchDatabase, pid: int) -> bool:
    """Whether backend *pid* comes to wait on a lock another transaction holds."""
    async with db.engine.connect() as observer:
        for _ in range(150):
            waiting = (
                await observer.execute(
                    text("SELECT count(*) FROM pg_locks WHERE pid = :pid AND NOT granted"),
                    {"pid": pid},
                )
            ).scalar_one()
            if waiting:
                return True
            await asyncio.sleep(0.1)
    return False


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


async def _cancel(session, task_id: uuid.UUID) -> bool:
    """What the ``tasks/cancel`` route does with the database: read, cancel, commit."""
    task = await task_svc.get_task(session, task_id, _CLIENT_ID)
    assert task is not None
    cancelled = await task_svc.request_cancel(session, task)
    await session.commit()
    return cancelled


async def _stored(db: ScratchDatabase, task_id: uuid.UUID, run_id: uuid.UUID):
    async with db.sessions() as check:
        run = (await check.execute(select(AssignmentRun).where(AssignmentRun.id == run_id))).scalar_one()
        task = (await check.execute(select(A2ATask).where(A2ATask.id == task_id))).scalar_one()
        return run, task


class TestACancelRacingTheTerminalWrite:
    async def test_the_cancel_waits_for_the_run_instead_of_deadlocking_with_it(self, monkeypatch, caplog):
        """A cancel landing while the run finalises.

        The terminal write is held between its two writes, with the task row
        written and the run row not yet. The cancel must then queue behind it
        holding nothing, see the run finished, and say so.
        """
        caplog.set_level(logging.WARNING, logger="app.a2a.tasks")
        # After the commit, and not about the two rows: the frames have their
        # own files.
        monkeypatch.setattr(task_svc, "announce_state", _nothing)
        # Only reached when the terminal write was aborted; not worth a wait.
        monkeypatch.setattr(task_svc, "_TERMINAL_WRITE_BACKOFF_SECONDS", 0)

        async with _temp_database() as db:
            async with db.sessions() as seed:
                task_id, run_id = await _seed(seed, run_status="running")

            gate = _Gate()
            async with db.sessions() as executor, db.sessions() as partner:
                executing_task = (await executor.execute(select(A2ATask).where(A2ATask.id == task_id))).scalar_one()
                finalising = asyncio.create_task(
                    task_svc._finalise_run(
                        _HeldAfterItsFirstWrite(executor, gate),
                        executing_task,
                        run_id,
                        "completed",
                        "The answer.",
                        None,
                    )
                )
                cancelling = None
                queued = updated_runs_first = False
                try:
                    await asyncio.wait_for(gate.holding.wait(), _SETTLE_SECONDS)
                    pid = await _backend_pid(partner)
                    cancelling = asyncio.create_task(_cancel(partner, task_id))
                    queued = await _queued_on_a_lock(db, pid)
                    updated_runs_first = await _has_updated_runs(db, pid)
                finally:
                    gate.release.set()
                    outcomes = await asyncio.wait_for(
                        asyncio.gather(
                            *(t for t in (finalising, cancelling) if t is not None),
                            return_exceptions=True,
                        ),
                        _SETTLE_SECONDS,
                    )

            assert queued, "the cancel never waited for the terminal write it raced"
            assert not updated_runs_first, (
                "the cancel wrote the run row before it had the task row, the reverse of the terminal write's order"
            )
            finalised, answered = outcomes
            assert not isinstance(answered, BaseException), f"the cancel failed instead of answering: {answered!r}"
            assert not isinstance(finalised, BaseException), f"the terminal write raised: {finalised!r}"
            assert answered is False, "the run finished first; the cancel is too late"
            assert _TERMINAL_WRITE_FAILED not in caplog.text, "the terminal write was aborted and had to be retried"

            run, task = await _stored(db, task_id, run_id)
            assert run.status == "completed"
            assert run.result_summary == "The answer.", "the answer was lost to the cancel"
            assert task.cancel_requested_at is None, "a too-late cancel stamped the task"


class TestACancelRacingTheClaim:
    async def test_the_cancel_waits_for_the_claim_instead_of_deadlocking_with_it(self, monkeypatch, caplog):
        """A partner cancelling right after it submitted meets the executor's
        claim, which writes the same two rows. Held after its first write,
        whichever row that is, the claim must still let the cancel through."""
        caplog.set_level(logging.WARNING, logger="app.a2a.tasks")
        cancel_returned = asyncio.Event()

        async def _answers(**_kwargs):
            # Not before the cancel has returned: the terminal write takes the
            # task row too, and which of the two got it first would otherwise
            # decide the outcome instead of the claim.
            await asyncio.wait_for(cancel_returned.wait(), _SETTLE_SECONDS)
            return "The answer.", None

        async def _cancel_then_answer(session, task_id: uuid.UUID) -> bool:
            try:
                return await _cancel(session, task_id)
            finally:
                cancel_returned.set()

        monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _answers)
        monkeypatch.setattr(task_svc, "_supervise_run", _nothing)
        monkeypatch.setattr(task_svc, "announce_state", _nothing)
        monkeypatch.setattr(task_svc, "_TERMINAL_WRITE_BACKOFF_SECONDS", 0)

        async with _temp_database() as db:
            async with db.sessions() as seed:
                task_id, run_id = await _seed(seed, run_status="pending")

            gate = _Gate()
            opened = 0

            @asynccontextmanager
            async def _executor_session():
                async with db.sessions() as session:
                    yield _HeldAfterItsFirstWrite(session, gate)

            def _sessions():
                # The executor's own session is the first one opened; a retry
                # of its terminal write would open plain ones after it.
                nonlocal opened
                opened += 1
                return _executor_session() if opened == 1 else db.sessions()

            monkeypatch.setattr("app.core.database.async_session", _sessions)

            async with db.sessions() as partner:
                executing = asyncio.create_task(task_svc.execute_task_run(task_id))
                cancelling = None
                queued = False
                try:
                    await asyncio.wait_for(gate.holding.wait(), _SETTLE_SECONDS)
                    pid = await _backend_pid(partner)
                    cancelling = asyncio.create_task(_cancel_then_answer(partner, task_id))
                    queued = await _queued_on_a_lock(db, pid)
                finally:
                    gate.release.set()
                    if cancelling is None:
                        cancel_returned.set()
                    outcomes = await asyncio.wait_for(
                        asyncio.gather(
                            *(t for t in (executing, cancelling) if t is not None),
                            return_exceptions=True,
                        ),
                        _SETTLE_SECONDS,
                    )

            assert queued, "the cancel never waited for the claim it raced"
            executed, answered = outcomes
            assert not isinstance(answered, BaseException), f"the cancel failed instead of answering: {answered!r}"
            assert not isinstance(executed, BaseException), f"the executor raised: {executed!r}"
            assert answered is True, "the claimed run was live; the cancel must reach it"
            assert _TERMINAL_WRITE_FAILED not in caplog.text

            run, task = await _stored(db, task_id, run_id)
            assert run.status == "cancelled", "the executor's write clobbered the cancel"
            assert task.cancel_requested_at is not None

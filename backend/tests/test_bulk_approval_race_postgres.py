"""The bulk approval paths under a real concurrent decision.

FR-030 and SC-023 want one decision per proposal "including with two surfaces
open at once", and FR-039c extends that to releasing. The single-run endpoints
got their row lock in the per-tool approval work; the three bulk paths kept
reading the pause with a bare ``SELECT``, so two reviewers could both observe
``awaiting_human``, both write, and leave a stored verdict that need not be the
one the worker consumed.

Against real PostgreSQL because the property under test *is* the database's:
``FOR UPDATE`` blocking a second reader until the first transaction commits, and
the re-read then seeing the committed state. A fake session has no lock manager,
so a test on one would pass against code with no locking at all — which is how
the source-grep test in ``test_approval_exposure_and_locking.py`` can only prove
the call is written, never that it works.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest import mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User

pytestmark = pytest.mark.integration


def _base_url() -> str:
    # CRLF-safe: a .env dragged through Windows leaves \r on the value, which
    # asyncpg faithfully turns into a database named "copilot\r".
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    ).strip()


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    test_url = parsed._replace(path=f"/{db_name}").geturl()

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        orig = getattr(exc, "orig", None)
        pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if pgcode == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        # A machine with no Postgres raises OperationalError, which is a
        # DBAPIError — so it arrives here, not in the arm below. Skipping on a
        # missing ``pgcode`` keeps this suite from failing where the server
        # simply is not there, which is what the bare arm used to promise and
        # could never deliver.
        if pgcode is None:
            pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
        raise
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as exc:
                pytest.skip(f"pgvector unavailable: {exc}")
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                # A cancelled task can still hold a connection here, and the
                # resulting 55006 would replace whatever the test actually
                # asserted. Dropped best-effort: a leftover database in the test
                # server is cheaper than a masked failure.
                with contextlib.suppress(Exception):
                    await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


async def _seed_paused_run(db) -> tuple[uuid.UUID, User]:
    """One admin and one company-managed run paused on a single proposal."""
    admin = User(
        id=uuid.uuid4(),
        email=f"a-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Admin",
        role="admin",
    )
    db.add(admin)
    await db.flush()

    agent = Agent(id=uuid.uuid4(), name="agent", user_id=admin.id)
    db.add(agent)
    await db.flush()

    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="assignment",
        trigger_type="manual",
        prompt="do the thing",
        user_id=admin.id,
    )
    db.add(assignment)
    await db.flush()

    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        user_id=admin.id,
        is_company_managed=True,
        status="awaiting_human",
        pending_approvals=[
            {
                "tool_call_id": "call-1",
                "tool_name": "delete_everything",
                "input": {"scope": "all"},
            }
        ],
    )
    db.add(run)
    await db.commit()
    return run.id, admin


@pytest.mark.asyncio
async def test_the_second_reader_waits_and_then_sees_the_answered_run():
    """The race itself. B asks for the same pause while A holds it, and must not
    get a row back until A commits — then must read A's decision rather than the
    ``awaiting_human`` it would have read a moment earlier. That second half is
    what makes the caller's status check a transition instead of an observation.
    """
    from app.api.v1.assignments import _locked_run

    async with _temp_database("hitl_race") as session:
        async with session() as setup:
            run_id, _ = await _seed_paused_run(setup)

        b_started = asyncio.Event()
        b_got_row_while_a_held = None

        async def reviewer_b():
            async with session() as db:
                b_started.set()
                run = await _locked_run(db, run_id)
                # Reached only after A commits; if the lock were missing this
                # would return the untouched row while A was still deciding.
                return run.status, bool(run.approval_decisions)

        async with session() as a:
            locked = await _locked_run(a, run_id)
            assert locked is not None

            task = asyncio.create_task(reviewer_b())
            await b_started.wait()
            # Give B a real chance to come back early. It cannot: it is queued
            # behind A's row lock.
            await asyncio.sleep(0.5)
            b_got_row_while_a_held = task.done()

            # Exactly what answering does: decisions written, status left alone.
            # ``resume_assignment_run`` only emits an event, so the run stays
            # ``awaiting_human`` until the worker drains it.
            locked.approval_decisions = {
                "call-1": {"tool_call_id": "call-1", "decision": "approve"}
            }
            await a.commit()

            status, answered = await asyncio.wait_for(task, timeout=10)

    assert b_got_row_while_a_held is False, (
        "the second reviewer read the pause while the first still held it — "
        "the row lock is not being taken"
    )
    assert (status, answered) == ("awaiting_human", True), (
        "the second reviewer read stale state after the lock was released"
    )


@pytest.mark.asyncio
async def test_the_status_alone_cannot_tell_an_answered_run_from_a_fresh_pause():
    """Why the lock needed a companion check.

    Answering does not move the run out of ``awaiting_human`` — the resume is an
    outbox event the worker drains later. So a second reviewer who takes the
    lock and tests only the status reads exactly what the first one read and
    overwrites their verdict. ``approval_decisions`` is the field that changes,
    and refusing on it is what actually closes the race.
    """
    from app.api.v1.assignments import _locked_run, _refuse_already_answered

    async with _temp_database("hitl_guard") as session:
        async with session() as first:
            run_id, _ = await _seed_paused_run(first)
            run = await _locked_run(first, run_id)
            run.approval_decisions = {
                "call-1": {"tool_call_id": "call-1", "decision": "approve"}
            }
            await first.commit()

        async with session() as second:
            run = await _locked_run(second, run_id)
            assert run.status == "awaiting_human", (
                "the fixture no longer reproduces the case this test is about"
            )
            with pytest.raises(HTTPException) as exc:
                _refuse_already_answered(run)
            assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_a_run_answered_mid_sweep_leaves_the_by_tool_selection():
    """``approve_by_tool`` picks its own members, then re-reads each under the
    lock before deciding it. A run answered in between must drop out — and the
    selection itself must not lock, or the first answer's commit would release
    every lock it took and leave the rest of the sweep on a stale snapshot.
    """
    from app.api.v1.assignments import _answerable_pending_runs, _locked_run

    async with _temp_database("hitl_sweep") as session:
        async with session() as setup:
            run_id, admin = await _seed_paused_run(setup)

        async with session() as db:
            candidates = await _answerable_pending_runs(db, admin)
            assert [r.id for r in candidates] == [run_id]

            async with session() as rival:
                answered = await _locked_run(rival, run_id)
                answered.approval_decisions = {
                    "call-1": {"tool_call_id": "call-1", "decision": "reject"}
                }
                await rival.commit()

            # The re-read the endpoint performs before acting on a candidate.
            run = await _locked_run(db, candidates[0].id)
            assert run.approval_decisions, (
                "the candidate was decided from the pre-lock snapshot"
            )


@pytest.mark.asyncio
async def test_the_paused_run_is_offered_when_nobody_else_answered_it():
    """The control for the test above: without a competing decision the same run
    is selected, so a green result there means the lock dropped it, not that the
    fixture never produced an answerable run.
    """
    from app.api.v1.assignments import _answerable_pending_runs

    async with _temp_database("hitl_sweep_ok") as session:
        async with session() as setup:
            run_id, admin = await _seed_paused_run(setup)
        async with session() as db:
            runs = await _answerable_pending_runs(db, admin)

    assert [r.id for r in runs] == [run_id]


@pytest.mark.asyncio
async def test_the_run_is_read_fresh_when_a_request_names_it_twice():
    """A bulk body may name the same run twice. The second pass has to see the
    first pass's write, which the identity map would otherwise hide behind the
    attributes loaded before the lock.
    """
    from app.api.v1.assignments import _locked_run

    async with _temp_database("hitl_dup") as session:
        async with session() as setup:
            run_id, _ = await _seed_paused_run(setup)

        async with session() as db:
            first = await _locked_run(db, run_id)
            assert first.status == "awaiting_human"
            # The lock is released by this commit, so another reviewer can and
            # does answer the run before the duplicate id comes round again.
            await db.commit()

            async with session() as rival:
                answered = await _locked_run(rival, run_id)
                answered.approval_decisions = {
                    "call-1": {"tool_call_id": "call-1", "decision": "approve"}
                }
                await rival.commit()

            second = await _locked_run(db, run_id)
            assert second is first, "expected the identity map to return the same object"
            assert second.approval_decisions, (
                "the second pass read the attributes loaded before the lock — "
                "populate_existing is not in effect"
            )


@pytest.mark.asyncio
async def test_a_refused_member_does_not_break_the_one_after_it():
    """Partial success across a *real* session (FR-033).

    Pins the reason the refusal paths end no transaction. Releasing the row
    there looked free, but ``rollback`` expires the whole identity map —
    including the ``user`` the request was authenticated with — so the next
    member's ``require_run_access`` touched an expired instance and the batch
    500s, losing every result already collected. The unit suites cannot see it:
    their fake sessions answer ``rollback`` with a counter.

    Driven through the endpoint because that is the only level where the
    session, the user and the loop are the same objects the request uses.
    """
    from app.api.v1.assignments import batch_respond_to_approvals
    from app.api.v1.schemas.assignment import (
        BatchApprovalDecision,
        BatchApprovalRequest,
    )

    async with _temp_database("hitl_partial") as session, session() as db:
        run_id, admin = await _seed_paused_run(db)
        # The user must be live in this session, as ``get_current_user`` leaves it.
        admin = (
            await db.execute(select(User).where(User.id == admin.id))
        ).scalar_one()

        resumed: list = []

        async def _resume(_db, run):
            resumed.append(run.id)
            await _db.commit()

        with mock.patch(
            "app.services.assignment_engine.resume_assignment_run", _resume
        ):
            out = await batch_respond_to_approvals(
                BatchApprovalRequest(
                    decisions=[
                        # Refused: not a run id at all.
                        BatchApprovalDecision(
                            run_id="not-a-uuid",
                            tool_call_id="call-0",
                            decision="approve",
                        ),
                        # Must still be answered.
                        BatchApprovalDecision(
                            run_id=str(run_id),
                            tool_call_id="call-1",
                            decision="approve",
                        ),
                    ]
                ),
                user=admin,
                db=db,
            )

    by_status = {r.status for r in out.results}
    assert by_status == {"refused", "applied"}, (
        f"the good member did not survive the refused one: {out.results}"
    )
    assert resumed == [run_id]

"""A partner cancel this side owes survives a hard crash.

Cancelling a run tree ends the local half in the caller's transaction and
tells the partners afterwards, detached - reaching N partners is N network
calls. Between the two the obligation used to exist only in memory: a SIGKILL
after the commit lost it, and the floor sweep could only abandon the row,
which cancels nothing, so the partner kept working and billing until its own
lifetime bound.

The obligation is now a column written with the decision, re-sent by the
client sweep while the row is outstanding, and kept once it concludes as the
record that the caller was cancelled. What a WHERE clause and a
commit reach is decided by running them, so this runs against PostgreSQL -
same scratch-database recipe as ``test_a2a_client_cancel_scope_postgres.py``.
The partner call itself is stubbed: ``delegation.cancel`` records who it was
asked to tell.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.a2a.client import delegation as delegation_mod
from app.a2a.client import sweep
from app.models.a2a import MECHANISM_ASKED, OUTCOME_CANCELLED, A2ADelegation
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "a2aowed",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


async def _seed(session):
    """A user, an agent, a running run, and one partner registration."""
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.integration import Integration
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="A2A Owed Cancel Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Caller", user_id=user.id)
    partner = Integration(
        id=uuid.uuid4(),
        provider="oauth_a2a",
        name=f"acme-{uuid.uuid4().hex[:6]}",
        user_id=user.id,
    )
    session.add_all([agent, partner])
    await session.flush()
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=agent.id,
        user_id=user.id,
        status="running",
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return user, agent, partner, run


async def _outstanding(session, *, user, agent, partner, run=None, conversation=None, **kw):
    row = A2ADelegation(
        id=uuid.uuid4(),
        integration_id=partner.id,
        agent_id=agent.id,
        user_id=user.id,
        run_ref=run.id if run is not None else None,
        conversation_ref=conversation,
        partner_task_id=f"task-{uuid.uuid4().hex[:8]}",
        protocol_version="0.3",
        signature_state="unsigned",
        submitted_at=datetime.now(UTC) - timedelta(hours=1),
        **kw,
    )
    session.add(row)
    await session.flush()
    return row


async def _read(session, row_id):
    return (
        await session.execute(
            select(A2ADelegation)
            .where(A2ADelegation.id == row_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


@pytest.fixture
def told(monkeypatch):
    """Stub the partner: every cancel lands here, and every target resolves."""
    calls: list[str] = []

    async def _target_for(_db, _target_id):
        return SimpleNamespace(id=uuid.uuid4())

    async def _cancel(_integration, _target, task_id):
        calls.append(task_id)

    async def _enabled(_db):
        return True

    monkeypatch.setattr(delegation_mod, "_target_for", _target_for)
    monkeypatch.setattr(delegation_mod, "cancel", _cancel)
    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _enabled)
    return calls


def _long_ago():
    return datetime.now(UTC) - timedelta(days=1)


async def test_the_cascade_commits_the_obligation_with_the_decision():
    from app.a2a.tasks import cascade_cancel

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        await session.commit()
        run_id, row_id = run.id, row.id

        await cascade_cancel(session, [run_id])
        await session.rollback()
        assert (await _read(session, row_id)).cancel_pending_at is None, (
            "the obligation outlived the decision it belongs to"
        )

        await cascade_cancel(session, [run_id])
        await session.commit()
        stored = await _read(session, row_id)
        assert stored.cancel_pending_at is not None
        assert stored.outcome is None, "nothing has told the partner yet"


async def test_a_cancel_lost_to_a_hard_kill_is_resent_once(told):
    """The post-crash state: the decision committed, the partner never told."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            run=run,
            cancel_pending_at=_long_ago(),
        )
        await session.commit()

        assert await sweep.reissue_owed_cancels(session) == 1
        assert told == [row.partner_task_id]
        stored = await _read(session, row.id)
        assert stored.outcome == OUTCOME_CANCELLED
        assert stored.mechanism == MECHANISM_ASKED
        assert stored.cancel_pending_at is not None, (
            "the record that the caller was cancelled went with the telling"
        )

        assert await sweep.reissue_owed_cancels(session) == 0
        assert told == [row.partner_task_id], "a cancelled row was sent again"


async def test_the_re_send_is_teardown_and_ignores_chat_availability(
    told, monkeypatch
):
    """The fast path tells the partner whatever the licence or the AI toggle
    says, so a crash must not make the re-send wait for chat to come back."""

    async def _disabled(_db):
        return False

    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _disabled)

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            run=run,
            cancel_pending_at=_long_ago(),
        )
        await session.commit()

        assert await sweep.reissue_owed_cancels(session) == 1
        assert told == [row.partner_task_id]


async def test_a_fresh_obligation_is_left_to_the_fast_path(told):
    """A cancel owed seconds ago is still being sent by the process that
    decided it; re-sending now would race it."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            run=run,
            cancel_pending_at=datetime.now(UTC),
        )
        await session.commit()

        assert await sweep.reissue_owed_cancels(session) == 0
        assert told == []
        assert (await _read(session, row.id)).outcome is None


async def test_the_fast_path_discharges_the_obligation(told):
    """``spawn_run_cancel``'s own call concludes the row, so the sweep has
    nothing left to send for a cancel that completed normally - and the stamp
    stays, as the record that the caller was cancelled."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            run=run,
            cancel_pending_at=_long_ago(),
        )
        await session.commit()

        await delegation_mod.cancel_outstanding_for(session, run_ref=run.id)
        await session.commit()
        told.clear()

        stored = await _read(session, row.id)
        assert stored.outcome == OUTCOME_CANCELLED
        assert stored.cancel_pending_at is not None
        assert await sweep.reissue_owed_cancels(session) == 0
        assert told == []


async def test_the_floor_does_not_conflate_an_owed_cancel_with_abandonment(
    told, monkeypatch
):
    """Abandonment cancels nothing, by design; an owed cancel is the opposite
    obligation. A row owed a cancel is neither collected nor abandoned by the
    floor, whatever its age - and an equally old row without one still is."""
    from app.a2a.client import callbacks

    abandoned: list = []
    collected: list = []

    async def _conclude(_db, row, **_kw):
        abandoned.append(row.id)

    async def _collect(_db, row, **_kw):
        collected.append(row.id)

    monkeypatch.setattr(callbacks, "conclude_abandoned", _conclude)
    monkeypatch.setattr(callbacks, "on_partner_finished", _collect)

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        ancient = datetime.now(UTC) - timedelta(days=30)
        owed = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            run=run,
            cancel_pending_at=_long_ago(),
        )
        owed.submitted_at = ancient
        plain = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        plain.submitted_at = ancient
        await session.commit()

        await sweep.collect_overdue(session)
        assert owed.id not in abandoned and owed.id not in collected
        assert plain.id in abandoned, "the floor still ends a row nobody owes a cancel"

        assert await sweep.reissue_owed_cancels(session) == 1
        assert told == [owed.partner_task_id]


async def test_a_callback_cannot_resume_a_caller_owed_a_cancel():
    """The run was cancelled; a partner's callback on the row must not route
    a collect that would resume it."""
    from app.a2a.client.callbacks import addressed_delegation

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        owed = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            run=run,
            cancel_pending_at=_long_ago(),
        )
        plain = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        await session.commit()

        assert await addressed_delegation(session, str(owed.id)) is None
        assert (await addressed_delegation(session, str(plain.id))) is not None


async def test_a_callback_admitted_before_the_cancel_loses_the_claim():
    """``addressed_delegation`` may have admitted the callback just before the
    cancel committed; the claim itself re-checks the marker, so the detached
    collect that follows never acts on the row."""
    from app.a2a.client.callbacks import claim

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        await session.commit()
        admitted = await _read(session, row.id)

        row.cancel_pending_at = datetime.now(UTC)
        await session.commit()

        assert await claim(session, admitted) is False
        assert (await _read(session, row.id)).callback_claimed_at is None


async def test_a_floor_batch_does_not_abandon_a_row_cancelled_mid_batch(monkeypatch):
    """The floor's SELECT tests the marker once for a batch walked row by row;
    the abandonment write re-tests it, so a cancel committed meanwhile wins."""
    from app.a2a.client import callbacks

    resumed: list = []

    async def _resume(_db, row, *_a, **_k):
        resumed.append(row.id)

    monkeypatch.setattr(callbacks, "_resume_caller", _resume)

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        await session.commit()
        selected = await _read(session, row.id)

        row.cancel_pending_at = datetime.now(UTC)
        await session.commit()

        await callbacks.conclude_abandoned(session, selected, mechanism=MECHANISM_ASKED)
        stored = await _read(session, row.id)
        assert stored.outcome is None, "an owed cancel was abandoned"
        assert resumed == []


async def test_a_collect_that_raced_the_cancel_does_not_resume_the_caller(monkeypatch):
    """A collect already under way when the cancel committed still records the
    partner's answer, but the caller it would resume was cancelled."""
    from app.a2a.client import callbacks

    resumed: list = []

    async def _resume_run(_db, run):
        resumed.append(run.id)

    monkeypatch.setattr(
        "app.services.assignment_engine.resume_assignment_run", _resume_run
    )

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        owed = await _outstanding(
            session, user=user, agent=agent, partner=partner, run=run,
            cancel_pending_at=datetime.now(UTC),
        )
        plain = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        await session.commit()
        result = SimpleNamespace(succeeded=True, result_text="done", detail=None, outcome="completed")

        await callbacks._resume_caller(session, owed, result, partner_name="Acme")
        assert resumed == [], "a cancelled caller was resumed"

        await callbacks._resume_caller(session, plain, result, partner_name="Acme")
        assert resumed == [run.id], "an ordinary delegation still resumes its caller"


async def test_a_callback_claimed_after_the_partner_was_told_still_loses(told):
    """The fast path tells the partner seconds after the cancel commits, well
    inside a collect. A callback admitted before the cancel that reaches its
    claim only after that must lose it all the same: sessions do not expire on
    commit, so a winner would act on a row it still reads as open."""
    from app.a2a.client.callbacks import claim
    from app.a2a.tasks import cascade_cancel

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        await session.commit()

        await cascade_cancel(session, [run.id])
        await session.commit()
        await delegation_mod.cancel_outstanding_for(session, run_ref=run.id)
        await session.commit()
        assert told == [row.partner_task_id]

        assert await claim(session, row) is False
        assert (await _read(session, row.id)).callback_claimed_at is None


async def test_a_collect_that_outlasted_the_telling_does_not_resume_the_caller(
    told, monkeypatch
):
    """A collect under way when the cancel committed usually finishes AFTER the
    fast path told the partner, the telling being one quick call. The stamp is
    what it reads before resuming, so the stamp has to outlive the telling."""
    from app.a2a.client import callbacks
    from app.a2a.tasks import cascade_cancel

    resumed: list = []

    async def _resume_run(_db, run):
        resumed.append(run.id)

    monkeypatch.setattr(
        "app.services.assignment_engine.resume_assignment_run", _resume_run
    )

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        row = await _outstanding(session, user=user, agent=agent, partner=partner, run=run)
        await session.commit()

        await cascade_cancel(session, [run.id])
        await session.commit()
        await delegation_mod.cancel_outstanding_for(session, run_ref=run.id)
        await session.commit()

        result = SimpleNamespace(
            succeeded=True, result_text="done", detail=None, outcome="completed"
        )
        await callbacks._resume_caller(session, row, result, partner_name="Acme")
        assert resumed == [], "a cancelled caller was resumed"


async def test_the_conversation_cascade_records_the_obligation_first(monkeypatch):
    """The chat cancel cascade runs detached and commits its own session only
    once every partner call returned; the obligation must be durable before
    the first partner call starts."""
    from app.models.chat import Conversation
    from app.services import sub_agent

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, _run = await _seed(session)
        conversation = Conversation(id=uuid.uuid4(), user_id=user.id, title="Owed")
        session.add(conversation)
        await session.flush()
        row = await _outstanding(
            session, user=user, agent=agent, partner=partner, conversation=conversation.id
        )
        await session.commit()

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        seen_during_call: list = []

        async def _crash_mid_cancel(db, **_kw):
            async with sessionmaker() as probe:
                seen_during_call.append(
                    (await _read(probe, row.id)).cancel_pending_at
                )
            raise RuntimeError("process killed before the partner was told")

        monkeypatch.setattr(delegation_mod, "cancel_outstanding_for", _crash_mid_cancel)

        async with sessionmaker() as cascade_db:
            await sub_agent._cancel_partner_tasks(
                cascade_db, conversation.id, str(conversation.id)
            )

        assert seen_during_call and seen_during_call[0] is not None, (
            "the obligation was not durable when the partner call started"
        )
        stored = await _read(session, row.id)
        assert stored.cancel_pending_at is not None
        assert stored.outcome is None


async def test_a_backlog_above_one_batch_is_re_sent_in_one_cycle(told):
    """A crash can leave more owed cancels than the old fixed batch of 25.
    Capping a cycle there made the 26th wait a whole sweep interval, breaking
    the documented bound: a cycle now drains the backlog."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        rows = [
            await _outstanding(
                session,
                user=user,
                agent=agent,
                partner=partner,
                run=run,
                cancel_pending_at=_long_ago() - timedelta(seconds=i),
            )
            for i in range(30)
        ]
        await session.commit()

        assert await sweep.reissue_owed_cancels(session) == 30
        assert sorted(told) == sorted(r.partner_task_id for r in rows)
        assert await sweep.reissue_owed_cancels(session) == 0


async def test_the_backlog_is_read_once_per_batch_not_once_per_row(told):
    """Nothing indexes the owed rows, so a read of the backlog scans the
    delegation history. A cycle reads it once per batch it claims from, not
    once per row it claims."""
    from sqlalchemy import event

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        for i in range(30):
            await _outstanding(
                session,
                user=user,
                agent=agent,
                partner=partner,
                run=run,
                cancel_pending_at=_long_ago() - timedelta(seconds=i),
            )
        await session.commit()

        reads: list[str] = []

        def _count(_conn, _cursor, statement, *_args):
            if "ORDER BY a2a_delegations.cancel_pending_at" in statement:
                reads.append(statement)

        engine = session.bind.sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            assert await sweep.reissue_owed_cancels(session) == 30
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        # Two batches, and the read that finds nothing left.
        assert len(reads) == 3, f"{len(reads)} reads of the backlog for 30 rows"
        assert len(told) == 30


async def test_a_spent_budget_leaves_the_rest_to_the_next_cycle_oldest_first(told):
    """The budget bounds one cycle; what is left is still owed and goes first
    next cycle, oldest stamp first."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, partner, run = await _seed(session)
        newest, middle, oldest = [
            await _outstanding(
                session,
                user=user,
                agent=agent,
                partner=partner,
                run=run,
                cancel_pending_at=_long_ago() - timedelta(hours=age),
            )
            for age in (0, 1, 2)
        ]
        await session.commit()

        assert await sweep.reissue_owed_cancels(session, budget_seconds=0) == 1
        assert told == [oldest.partner_task_id], "a cycle stopped before its first row"
        assert await sweep.reissue_owed_cancels(session, budget_seconds=0) == 1
        assert told[-1] == middle.partner_task_id
        assert await sweep.reissue_owed_cancels(session) == 1
        assert told[-1] == newest.partner_task_id

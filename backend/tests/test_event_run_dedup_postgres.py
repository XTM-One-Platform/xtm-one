"""One run per inbound event on an agent, against real PostgreSQL.

The query behind the skip (``assignment_engine.duplicate_event_run``: the
JSON path on ``trigger_context``, the window, the exclusion of refused runs)
and the agent-wide advisory lock that makes check and insert atomic across
two assignments of one agent are the point, so they run here, not against a
fake session. Same scratch-database recipe as
``test_assignment_queue_drain_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from app.services import assignment_engine as ae
from app.services import event_run_dedup as dedup
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

MAIL_CONTEXT = {"type": "integration_event", "provider": "microsoft", "event": "new_email"}


def _mail(message_id: str, graph_id: str) -> dict:
    return {
        "from": "person@example.org",
        "subject": "Interview",
        "received_at": "2026-10-30T17:25:24Z",
        "message_id": graph_id,
        "conversation_id": f"thread-of-{graph_id}",
        "internet_message_id": message_id,
        "trigger_type": "integration_event",
        "event": "new_email",
        "provider": "microsoft",
    }


async def _seed(session, *, assignments: tuple[str, ...]) -> tuple[Agent, dict[str, Assignment]]:
    user = User(id=uuid.uuid4(), email=f"dedup-{uuid.uuid4().hex[:8]}@test.local", full_name="Dedup", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Executive Assistant", user_id=user.id)
    session.add(agent)
    await session.flush()
    rows = {}
    for name in assignments:
        row = Assignment(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            name=name,
            trigger_type="integration_event",
            prompt="p",
            trigger_config={},
        )
        session.add(row)
        rows[name] = row
    await session.commit()
    return agent, rows


@pytest.fixture
def _engine(monkeypatch):
    """AI on, no orchestration execution: the run rows are what is asserted."""
    from app.services import agentic_gate

    scheduled: list[uuid.UUID] = []

    async def _no_refusal(_db):
        return None

    async def _schedule(db, assignment, _agent, run, *, trigger_context=None, extra_delay_seconds=0):
        run.status = "pending"
        await db.commit()
        scheduled.append(run.id)

    monkeypatch.setattr(agentic_gate, "agentic_refusal", _no_refusal)
    monkeypatch.setattr(ae, "_schedule_run_execution", _schedule)
    monkeypatch.setattr(ae.settings, "assignment_event_dedup_window_seconds", 600)
    return scheduled


async def _runs(db) -> list[AssignmentRun]:
    async with db.sessions() as session:
        return list((await session.execute(select(AssignmentRun).order_by(AssignmentRun.created_at))).scalars())


async def test_one_mail_in_two_mailboxes_runs_once_across_the_agents_assignments(_engine):
    """The 30 Oct scenario: the service account's and the person's mailbox each
    have an assignment on the same agent; one mail reaches both."""
    async with scratch_database("dedup", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, rows = await _seed(session, assignments=("Service mailbox", "Person mailbox"))

        async with db.sessions() as session:
            first = await ae.trigger_assignment(
                rows["Service mailbox"], agent, _mail("<knox@mail.example>", "AAMk-service"),
                db=session, trigger_context={**MAIL_CONTEXT, "message_id": "AAMk-service"},
            )
        async with db.sessions() as session:
            second = await ae.trigger_assignment(
                rows["Person mailbox"], agent, _mail("<KNOX@mail.example>", "AAMk-person"),
                db=session, trigger_context={**MAIL_CONTEXT, "message_id": "AAMk-person"},
            )

        runs = {str(run.id): run for run in await _runs(db)}
        assert runs[first].status == "pending" and runs[first].assignment_id == rows["Service mailbox"].id
        assert runs[second].status == "skipped" and runs[second].assignment_id == rows["Person mailbox"].id
        assert runs[second].result_summary.startswith(f"{dedup.DUPLICATE_SKIP_PREFIX} {first}")
        assert runs[second].trigger_context[dedup.DUPLICATE_OF_CONTEXT] == first
        assert runs[first].trigger_context[dedup.DEDUP_KEY_CONTEXT] == runs[second].trigger_context[dedup.DEDUP_KEY_CONTEXT]
        assert _engine == [uuid.UUID(first)], "only the first run was scheduled"

        async with db.sessions() as session:
            counted = (
                await session.execute(select(Assignment.events_filtered_count).where(Assignment.id == rows["Person mailbox"].id))
            ).scalar_one()
        assert counted == 1


async def test_a_second_mail_of_the_thread_and_another_agents_run_are_not_duplicates(_engine):
    async with scratch_database("dedup", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, rows = await _seed(session, assignments=("Inbox",))
            other_agent, other_rows = await _seed(session, assignments=("Other inbox",))

        async with db.sessions() as session:
            await ae.trigger_assignment(rows["Inbox"], agent, _mail("<one@x>", "A1"), db=session, trigger_context=dict(MAIL_CONTEXT))
        async with db.sessions() as session:
            await ae.trigger_assignment(rows["Inbox"], agent, _mail("<two@x>", "A2"), db=session, trigger_context=dict(MAIL_CONTEXT))
        async with db.sessions() as session:
            await ae.trigger_assignment(
                other_rows["Other inbox"], other_agent, _mail("<one@x>", "B1"), db=session, trigger_context=dict(MAIL_CONTEXT)
            )

        assert [run.status for run in await _runs(db)] == ["pending", "pending", "pending"]


async def test_the_window_is_the_bound_and_a_refused_run_is_never_the_original(_engine):
    async with scratch_database("dedup", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, rows = await _seed(session, assignments=("Inbox",))
            session.add(
                AssignmentRun(
                    assignment_id=rows["Inbox"].id,
                    agent_id=agent.id,
                    user_id=rows["Inbox"].user_id,
                    status="completed",
                    trigger_context={**MAIL_CONTEXT, dedup.DEDUP_KEY_CONTEXT: "mail:old@x"},
                    created_at=datetime.now(UTC) - timedelta(seconds=601),
                )
            )
            await session.commit()

        async with db.sessions() as session:
            # Past the window: not a duplicate.
            assert await ae.duplicate_event_run(session, agent.id, "mail:old@x", window_seconds=600) is None
            assert await ae.duplicate_event_run(session, agent.id, "mail:old@x", window_seconds=3600) is not None
            # The window off: never a duplicate.
            assert await ae.duplicate_event_run(session, agent.id, "mail:old@x", window_seconds=0) is None

        async with db.sessions() as session:
            # A run that failed gave the mail no answer: the next copy may run.
            session.add(
                AssignmentRun(
                    assignment_id=rows["Inbox"].id,
                    agent_id=agent.id,
                    user_id=rows["Inbox"].user_id,
                    status="failed",
                    trigger_context={**MAIL_CONTEXT, dedup.DEDUP_KEY_CONTEXT: "mail:failed@x"},
                )
            )
            await session.commit()
            assert await ae.duplicate_event_run(session, agent.id, "mail:failed@x", window_seconds=600) is None

        async with db.sessions() as session:
            run_id = await ae.trigger_assignment(rows["Inbox"], agent, _mail("<old@x>", "A3"), db=session, trigger_context=dict(MAIL_CONTEXT))
        async with db.sessions() as session:
            skipped = await ae.trigger_assignment(rows["Inbox"], agent, _mail("<old@x>", "A4"), db=session, trigger_context=dict(MAIL_CONTEXT))
        async with db.sessions() as session:
            # The original the third one is refused for is the run that ran, not the refusal.
            assert str(await ae.duplicate_event_run(session, agent.id, "mail:old@x", window_seconds=600)) == run_id
        runs = {str(run.id): run for run in await _runs(db)}
        assert runs[skipped].trigger_context[dedup.DUPLICATE_OF_CONTEXT] == run_id


async def test_concurrent_deliveries_of_one_mail_to_two_assignments_insert_one_run(_engine):
    """A Graph notification on one mailbox and the poll on the other at the
    same instant: the agent-wide advisory lock serializes check and insert."""
    async with scratch_database("dedup", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, rows = await _seed(session, assignments=tuple(f"Mailbox {i}" for i in range(4)))

        async def _deliver(row: Assignment, index: int) -> str | None:
            async with db.sessions() as session:
                return await ae.trigger_assignment(
                    row, agent, _mail("<burst@mail.example>", f"AAMk-{index}"),
                    db=session, trigger_context={**MAIL_CONTEXT, "message_id": f"AAMk-{index}"},
                )

        ids = await asyncio.gather(*(_deliver(row, index) for index, row in enumerate(rows.values())))

        runs = await _runs(db)
        statuses = sorted(run.status for run in runs)
        assert statuses == ["pending", "skipped", "skipped", "skipped"], statuses
        original = next(run for run in runs if run.status == "pending")
        for run in runs:
            if run.status == "skipped":
                assert run.trigger_context[dedup.DUPLICATE_OF_CONTEXT] == str(original.id)
        assert set(ids) == {str(run.id) for run in runs}
        assert _engine == [original.id]

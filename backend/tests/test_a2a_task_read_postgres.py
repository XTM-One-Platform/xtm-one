"""A task is read no earlier than the run it is interpreted with.

``tasks/get`` loads the task, then projects its state from the run it mirrors.
The executor's terminal write commits both rows together: the run's status, and
on the task ``responding_agent_id`` or ``caller_question``. A task loaded before
that commit and interpreted with a run read after it answered ``completed`` with
no ``respondingAgent`` (the E2E failure), or ``completed`` with no
question for a run that ended by asking its caller.

The interleaving is built rather than timed: the partner's session loads the
task, the executor's terminal write commits from another session, and only then
does the partner read the state. Same scratch-database recipe as
``test_a2a_cancel_lock_order_postgres.py``.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.a2a import protocol as proto
from app.a2a import tasks as task_svc
from app.models.a2a import A2AContext, A2AExposure, A2ATask
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.user import User
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_database

pytestmark = pytest.mark.integration

_CLIENT_ID = "partner-torn-read"
_QUESTION = "Which region should the report cover?"


@asynccontextmanager
async def _temp_database() -> AsyncIterator[ScratchDatabase]:
    async with scratch_database("a2aread", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db


async def _seed(session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A partner's task mirroring one running run.

    Returns ``(task_id, run_id, agent_id, peer_id)``: the exposed agent, and the
    peer a mandatory transfer hands the answer to.
    """
    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Task Read Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Exposed", user_id=user.id)
    peer = Agent(id=uuid.uuid4(), name="Peer stage", user_id=user.id)
    session.add_all([agent, peer])
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
        status="running",
        started_at=datetime.now(UTC),
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
    return task.id, run.id, agent.id, peer.id


async def _nothing(*_args, **_kwargs) -> None:
    return None


async def _terminal_write(
    db: ScratchDatabase,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    summary: str | None = None,
    responding_agent_id: uuid.UUID | None = None,
    question: str | None = None,
) -> None:
    """The executor's end of a run, committed from its own session."""
    async with db.sessions() as executor:
        task = (await executor.execute(select(A2ATask).where(A2ATask.id == task_id))).scalar_one()
        if question:
            task.caller_question = question
        await task_svc._finalise_run(
            executor,
            task,
            run_id,
            "completed",
            summary,
            None,
            responding_agent_id=responding_agent_id,
        )


@pytest.fixture(autouse=True)
def _quiet_announcements(monkeypatch):
    # After the commit, and not about the two rows: the frames have their own
    # files.
    monkeypatch.setattr(task_svc, "announce_state", _nothing)


class TestAPollLandingAcrossTheTerminalWrite:
    async def test_a_transferred_answer_keeps_its_responding_agent(self):
        """``completed``, the peer stage's text, and no
        ``metadata.respondingAgent``."""
        async with _temp_database() as db:
            async with db.sessions() as seed:
                task_id, run_id, agent_id, peer_id = await _seed(seed)

            async with db.sessions() as partner:
                task = await task_svc.get_task(partner, task_id, _CLIENT_ID)
                assert task is not None and task.responding_agent_id is None

                await _terminal_write(
                    db, task_id, run_id, summary="Stage answer.", responding_agent_id=peer_id
                )

                assert await task_svc.resolve_state(partner, task) is proto.A2AState.COMPLETED
                agent = (await partner.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
                artifacts = await task_svc.build_artifacts(partner, task, agent, proto.PROTOCOL_VERSION_0_3)

            assert artifacts, "a completed task carried no result"
            responding = (artifacts[0].get("metadata") or {}).get("respondingAgent")
            assert responding is not None, "the peer's answer reads as the exposed agent's"
            assert responding["id"] == str(peer_id)

    async def test_a_run_that_asked_its_caller_is_input_required(self):
        """Same poll, a run that ended by asking: never ``completed`` with no
        question, which a partner reads as done and stops polling."""
        async with _temp_database() as db:
            async with db.sessions() as seed:
                task_id, run_id, _agent_id, _peer_id = await _seed(seed)

            async with db.sessions() as partner:
                task = await task_svc.get_task(partner, task_id, _CLIENT_ID)
                assert task is not None and task.caller_question is None

                await _terminal_write(db, task_id, run_id, question=_QUESTION)

                assert await task_svc.resolve_state(partner, task) is proto.A2AState.INPUT_REQUIRED
                assert task.caller_question == _QUESTION


class TestACancelMeetingTheTerminalWrite:
    async def test_a_cancel_meeting_a_run_that_asked_withdraws_the_question(self, monkeypatch):
        """The run completes, asking, between the cancel's state read and its
        guarded write: the write matches nothing, and the task is not terminal
        but ``input-required`` - the cancel withdraws the question."""
        real_resolve_state = task_svc.resolve_state

        async with _temp_database() as db:
            async with db.sessions() as seed:
                task_id, run_id, _agent_id, _peer_id = await _seed(seed)

            reads = 0

            async def _finalised_after_the_first_read(session, task):
                nonlocal reads
                state = await real_resolve_state(session, task)
                reads += 1
                if reads == 1:
                    await _terminal_write(db, task_id, run_id, question=_QUESTION)
                return state

            monkeypatch.setattr(task_svc, "resolve_state", _finalised_after_the_first_read)
            async with db.sessions() as partner:
                task = await task_svc.get_task(partner, task_id, _CLIENT_ID)
                assert task is not None
                cancelled = await task_svc.request_cancel(partner, task)
                await partner.commit()
            monkeypatch.setattr(task_svc, "resolve_state", real_resolve_state)

            assert reads >= 2, "the cancel never read the task again after its write"
            assert cancelled is True, "an input-required task was answered already terminal"
            async with db.sessions() as check:
                run = (await check.execute(select(AssignmentRun).where(AssignmentRun.id == run_id))).scalar_one()
                stored = (await check.execute(select(A2ATask).where(A2ATask.id == task_id))).scalar_one()
                assert run.status == "completed", "the cancel overwrote the run's own ending"
                assert stored.cancel_requested_at is not None
                assert stored.caller_question == _QUESTION
                assert await task_svc.resolve_state(check, stored) is proto.A2AState.CANCELED

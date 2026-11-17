"""Emergency stop — the sweep, against real rows (#panic).

Which rows a scoped sweep reaches is decided by running its SQL, so it is
run here: two users with a run tree, a background task, a flow run, a pending
runner command and assignments each; the ``self`` stop of one must leave the
other's rows exactly as they were, and the ``platform`` stop must reach both
plus the ownerless surfaces and the AI kill switch. The resume must re-enable
only what the stop paused — a manual pause stays a manual pause.

Redis is stubbed (the chat-turn arm only needs "which conversations are live"
and "ask that one to stop"); everything else is the real code path. Same
scratch-database recipe as ``test_a2a_cancel_child_runs_postgres.py``.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.core.database import Base
from app.models.assignment import Assignment, AssignmentRun
from app.models.audit_log import AuditLog
from app.models.chat import Conversation
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.orchestration import OrchestrationExecution
from app.models.platform_settings import PlatformSettings
from app.models.runner import Runner, RunnerCommand
from app.models.sub_agent_task import SubAgentTask
from app.services import chat_turn_guard, panic
from app.services.assignment_pause import (
    PANIC_STOP_REASON,
    actor_display_name,
    pause_assignment,
)
from app.services.orchestration_adapters.autonomous import (
    ADAPTER_NAME as AUTONOMOUS_ADAPTER,
)

pytestmark = pytest.mark.integration


def _base_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"panic_{uuid.uuid4().hex[:12]}"
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
        raise
    except Exception as exc:  # pragma: no cover — no PG in this environment
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": db_name},
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


# ---------------------------------------------------------------------------
# Seeds — one "world" per user
# ---------------------------------------------------------------------------


async def _user(session, *, role: str = "user"):
    from app.models.agent import Agent
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"panic-{uuid.uuid4().hex[:8]}@test.local",
        full_name=f"Panic {role}",
        hashed_password="x",
        role=role,
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Worker", user_id=user.id)
    session.add(agent)
    await session.flush()
    user.agent_id = agent.id  # plain attribute for the seeds below
    return user


async def _assignment(session, owner, *, enabled=True, run_as=None, company=False):
    row = Assignment(
        id=uuid.uuid4(),
        agent_id=owner.agent_id,
        user_id=owner.id,
        name=f"assignment-{uuid.uuid4().hex[:6]}",
        trigger_type="schedule",
        trigger_config={},
        prompt="do the thing",
        enabled=enabled,
        is_company_managed=company,
        run_as_user_id=run_as.id if run_as is not None else None,
    )
    session.add(row)
    await session.flush()
    return row


async def _run(session, user, *, parent=None, status="running", assignment=None):
    row = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id if assignment is not None else None,
        agent_id=user.agent_id,
        user_id=user.id,
        parent_run_id=parent,
        status=status,
        started_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def _execution(session, *, adapter, dedup_key, payload, state="running"):
    row = OrchestrationExecution(
        id=uuid.uuid4(),
        adapter=adapter,
        state=state,
        payload=payload,
        dedup_key=dedup_key,
        run_at=datetime.now(UTC),
        lease_owner="worker-1" if state == "running" else None,
    )
    session.add(row)
    await session.flush()
    return row


async def _world(session, user):
    """Everything the stop must reach for one user, plus its foreign rows."""
    assignment = await _assignment(session, user)
    parent = await _run(session, user, assignment=assignment)
    child = await _run(session, user, parent=parent.id)
    run_exec = await _execution(
        session,
        adapter="assignment",
        dedup_key=f"assignment_run:{parent.id}",
        payload={"run_id": str(parent.id)},
    )
    task = SubAgentTask(
        id=uuid.uuid4(),
        user_id=user.id,
        parent_agent_id=user.agent_id,
        target_agent_id=user.agent_id,
        task_description="background work",
        status="running",
        metadata_json={},
    )
    session.add(task)
    await session.flush()
    task_exec = await _execution(
        session,
        adapter="sub_agent",
        dedup_key=f"sub_agent_task:{task.id}",
        payload={"task_id": str(task.id)},
    )
    flow = Flow(
        id=uuid.uuid4(),
        name="flow",
        description="",
        graph={},
        version_number=1,
        user_id=user.id,
    )
    session.add(flow)
    await session.flush()
    flow_run = FlowRun(
        id=uuid.uuid4(),
        flow_id=flow.id,
        flow_version_number=1,
        graph={},
        status="running",
        trigger_type="manual",
        depth=0,
        user_id=user.id,
        is_company_managed=False,
        created_at=datetime.now(UTC),
    )
    session.add(flow_run)
    await session.flush()
    runner = Runner(
        id=uuid.uuid4(),
        name="box",
        platform="linux",
        key_prefix="run-abc",
        key_hash="h",
        user_id=user.id,
    )
    session.add(runner)
    await session.flush()
    command = RunnerCommand(
        id=uuid.uuid4(),
        runner_id=runner.id,
        user_id=user.id,
        kind="shell",
        payload={"command": "ls"},
        status="pending",
    )
    session.add(command)
    conversation = Conversation(id=uuid.uuid4(), user_id=user.id)
    session.add(conversation)
    await session.flush()
    return {
        "assignment": assignment,
        "parent": parent,
        "child": child,
        "run_exec": run_exec,
        "task": task,
        "task_exec": task_exec,
        "flow_run": flow_run,
        "command": command,
        "conversation": conversation,
    }


async def _get(session, model, row_id):
    return (
        await session.execute(select(model).where(model.id == row_id))
    ).scalar_one()


def _stub_redis(live_conversations: list[str]):
    """The chat-turn arm's two Redis calls, without Redis."""
    return (
        patch.object(
            chat_turn_guard,
            "list_processing_conversations",
            AsyncMock(return_value=live_conversations),
        ),
        patch.object(
            chat_turn_guard, "request_turn_cancel", AsyncMock(return_value=True)
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_self_scope_stops_only_the_actors_world_and_resumes_it():
    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _user(session)
            bob = await _user(session)
            a = await _world(session, alice)
            b = await _world(session, bob)
            # A company-managed automation that runs AS alice: in her scope.
            admin = await _user(session, role="admin")
            run_as_alice = await _assignment(
                session, admin, run_as=alice, company=True
            )
            # A pause alice made by hand: the stop must not restamp it, the
            # resume must not lift it.
            manual = await _assignment(session, alice, enabled=False)
            pause_assignment(
                manual,
                reason="user_toggle",
                details="by hand",
                actor_user_id=alice.id,
                actor_label="alice",
            )
            queued = await _run(session, alice, status="queued", assignment=a["assignment"])
            await session.commit()

        live = [str(a["conversation"].id), str(b["conversation"].id)]
        list_patch, cancel_patch = _stub_redis(live)
        async with sessions() as session:
            with list_patch, cancel_patch as cancel:
                actor = await _get(session, type(alice), alice.id)
                outcome = await panic.emergency_stop(
                    session, actor=actor, scope="self"
                )
                await session.commit()

        # Only alice's live conversation was asked to stop.
        cancel.assert_awaited_once_with(str(a["conversation"].id))
        assert outcome.chat_turns_cancelled == 1
        assert outcome.cancelled_conversation_ids == [str(a["conversation"].id)]
        assert outcome.runs_cancelled == 3  # parent + child + queued
        assert outcome.sub_agent_tasks_cancelled == 1
        assert outcome.flow_runs_cancelled == 1
        assert outcome.runner_commands_cancelled == 1
        assert outcome.assignments_paused == 2  # her own + the run-as-her one
        assert outcome.ai_disabled is False
        assert set(outcome.partner_cancel_run_ids) >= {a["parent"].id, a["child"].id}
        # The child is both a root and the parent's descendant: told once.
        assert len(outcome.partner_cancel_run_ids) == len(set(outcome.partner_cancel_run_ids))
        assert outcome.late_admissions_cancelled == 0

        async with sessions() as session:
            # Alice's world is stopped...
            for run_id in (a["parent"].id, a["child"].id, queued.id):
                run = await _get(session, AssignmentRun, run_id)
                assert run.status == "cancelled"
                assert run.completed_at is not None
            assert (await _get(session, OrchestrationExecution, a["run_exec"].id)).state == "cancelled"
            assert (await _get(session, OrchestrationExecution, a["task_exec"].id)).state == "cancelled"
            task = await _get(session, SubAgentTask, a["task"].id)
            assert task.status == "cancelled"
            assert task.metadata_json.get("delivery_suppressed") is True
            assert (await _get(session, FlowRun, a["flow_run"].id)).status == "cancelled"
            command = await _get(session, RunnerCommand, a["command"].id)
            assert command.status == "cancelled"
            assert command.error == panic.STOP_MESSAGE
            for assignment_id in (a["assignment"].id, run_as_alice.id):
                row = await _get(session, Assignment, assignment_id)
                assert row.enabled is False
                assert row.paused_reason == PANIC_STOP_REASON
                assert row.paused_by_user_id == alice.id
            manual_row = await _get(session, Assignment, manual.id)
            assert manual_row.enabled is False
            assert manual_row.paused_reason == "user_toggle"

            # ...and bob's is exactly as it was.
            assert (await _get(session, AssignmentRun, b["parent"].id)).status == "running"
            assert (await _get(session, AssignmentRun, b["child"].id)).status == "running"
            assert (await _get(session, OrchestrationExecution, b["run_exec"].id)).state == "running"
            assert (await _get(session, SubAgentTask, b["task"].id)).status == "running"
            assert (await _get(session, FlowRun, b["flow_run"].id)).status == "running"
            assert (await _get(session, RunnerCommand, b["command"].id)).status == "pending"
            assert (await _get(session, Assignment, b["assignment"].id)).enabled is True

            # No platform switch was touched by a self stop.
            ps = (await session.execute(select(PlatformSettings))).scalars().first()
            assert ps is None or ps.ai_disabled is None

            # One audit row for the stop, attributed to alice, with the counts.
            audit = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == panic.AUDIT_ACTION_STOP)
                )
            ).scalar_one()
            assert audit.user_id == alice.id
            assert audit.entity_type == "user"
            assert audit.details["assignments_paused"] == 2
            # ...plus one ``pause`` row per assignment the stop paused.
            pauses = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "pause",
                        AuditLog.details["reason"].astext == PANIC_STOP_REASON,
                    )
                )
            ).scalars().all()
            assert len(pauses) == 2

            # Status: what a resume would touch.
            actor = await _get(session, type(alice), alice.id)
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["self_paused_assignments"] == 2
            assert status["platform_paused_assignments"] is None
            assert status["ai_disabled"] is None
            assert status["platform_stop_active"] is False  # a self stop
            assert status["active"] is True
            # A display name, never the audit row's email.
            assert status["last_stop"]["by"] == actor_display_name(alice)
            assert status["last_stop"]["scope"] == "self"
            assert status["last_stop"]["reason"] is None  # not asked yet
            # Cancelled runs say why, on the row the run card reads.
            assert (await _get(session, AssignmentRun, a["parent"].id)).error == panic.STOP_MESSAGE
            assert (await _get(session, AssignmentRun, a["child"].id)).error == panic.STOP_MESSAGE

        # The reason arrives AFTER the stop, and lands on the audit row and on
        # the assignments this very stop paused (not on the hand-paused one).
        async with sessions() as session:
            actor = await _get(session, type(alice), alice.id)
            annotated = await panic.record_stop_reason(
                session,
                actor=actor,
                audit_id=outcome.audit_id,
                reason="Agent looping on a ticket queue",
            )
            await session.commit()
        assert annotated == 2
        async with sessions() as session:
            audit = await _get(session, AuditLog, outcome.audit_id)
            assert audit.details["reason"] == "Agent looping on a ticket queue"
            assert audit.summary.endswith("— reason: Agent looping on a ticket queue")
            for assignment_id in (a["assignment"].id, run_as_alice.id):
                row = await _get(session, Assignment, assignment_id)
                assert row.paused_details.endswith(
                    "Reason: Agent looping on a ticket queue"
                )
            manual_row = await _get(session, Assignment, manual.id)
            assert manual_row.paused_details == "by hand"
            # ...and the banner now knows why.
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["last_stop"]["reason"] == "Agent looping on a ticket queue"
            # Bob cannot annotate alice's stop.
            bob_actor = await _get(session, type(bob), bob.id)
            with pytest.raises(LookupError):
                await panic.record_stop_reason(
                    session, actor=bob_actor, audit_id=outcome.audit_id, reason="mine"
                )

        async with sessions() as session:
            actor = await _get(session, type(alice), alice.id)
            resumed = await panic.resume_after_emergency_stop(
                session, actor=actor, scope="self", reason="Loop fixed, safe to resume"
            )
            await session.commit()
        assert resumed.assignments_resumed == 2
        assert resumed.ai_enabled is False

        async with sessions() as session:
            for assignment_id in (a["assignment"].id, run_as_alice.id):
                row = await _get(session, Assignment, assignment_id)
                assert row.enabled is True
                assert row.paused_reason is None
            manual_row = await _get(session, Assignment, manual.id)
            assert manual_row.enabled is False  # the hand pause stands
            assert manual_row.paused_reason == "user_toggle"
            # Cancelled work stays cancelled: a resume re-arms schedules, it
            # does not resurrect runs.
            assert (await _get(session, AssignmentRun, a["parent"].id)).status == "cancelled"
            # The resume reason is on the panic_resume row AND on each
            # per-assignment resume row.
            lifted = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == panic.AUDIT_ACTION_RESUME)
                )
            ).scalar_one()
            assert lifted.details["reason"] == "Loop fixed, safe to resume"
            assert lifted.summary.endswith("— reason: Loop fixed, safe to resume")
            resumes = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "resume")
                )
            ).scalars().all()
            assert len(resumes) == 2
            assert {r.details["note"] for r in resumes} == {"Loop fixed, safe to resume"}


async def test_platform_scope_reaches_everyone_and_the_kill_switch():
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            alice = await _user(session)
            bob = await _user(session)
            a = await _world(session, alice)
            b = await _world(session, bob)
            autonomous = await _execution(
                session,
                adapter=AUTONOMOUS_ADAPTER,
                dedup_key="autonomous_run:openaev-1",
                payload={"openaev_run_id": "openaev-1"},
                state="pending",
            )
            await session.commit()

        # The route's two transactions: the door is closed and DURABLE — with
        # its audit row, so the trail already says a stop is in effect — before
        # anything live is selected.
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            armed = await panic.arm_admission_barrier(
                session, actor=actor, scope="platform"
            )
            await session.commit()
        assert armed.audit_id is not None
        assert armed.assignments_paused == 2 and armed.ai_disabled is True
        async with sessions() as session:
            assert await panic.platform_stop_open(session) is True
            # Even if phase 2 failed now, the banner and the resume would work.
            probe = await panic.emergency_stop_status(
                session, actor=await _get(session, type(alice), alice.id)
            )
            assert probe["platform_stop_active"] is True and probe["active"] is True
            assert (await _get(session, AuditLog, armed.audit_id)).details["phase"] == "armed"

        live = [str(a["conversation"].id), str(b["conversation"].id)]
        list_patch, cancel_patch = _stub_redis(live)
        async with sessions() as session:
            with list_patch, cancel_patch as cancel:
                actor = await _get(session, type(admin), admin.id)
                outcome = await panic.emergency_stop(
                    session, actor=actor, scope="platform", outcome=armed
                )
                await session.commit()
        assert outcome.audit_id == armed.audit_id

        assert cancel.await_count == 2
        assert outcome.ai_disabled is True
        assert outcome.runs_cancelled == 4
        assert outcome.sub_agent_tasks_cancelled == 2
        assert outcome.flow_runs_cancelled == 2
        assert outcome.runner_commands_cancelled == 2
        assert outcome.assignments_paused == 2
        assert outcome.autonomous_runs_cancelled == 1

        async with sessions() as session:
            for world in (a, b):
                assert (await _get(session, AssignmentRun, world["parent"].id)).status == "cancelled"
                assert (await _get(session, AssignmentRun, world["child"].id)).status == "cancelled"
                assert (await _get(session, SubAgentTask, world["task"].id)).status == "cancelled"
                assert (await _get(session, FlowRun, world["flow_run"].id)).status == "cancelled"
                assert (await _get(session, RunnerCommand, world["command"].id)).status == "cancelled"
                row = await _get(session, Assignment, world["assignment"].id)
                assert row.enabled is False
                assert row.paused_reason == PANIC_STOP_REASON
                assert row.paused_by_user_id == admin.id
            assert (await _get(session, OrchestrationExecution, autonomous.id)).state == "cancelled"
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.ai_disabled is True

            audit = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == panic.AUDIT_ACTION_STOP)
                )
            ).scalar_one()  # ONE row for the two phases
            assert audit.entity_type == "platform"
            assert audit.details["ai_disabled"] is True
            assert audit.details["phase"] == "completed"
            assert audit.details["runs_cancelled"] == 4
            # A self resume cannot lift the platform stop's boundary.
            with pytest.raises(panic.PlatformStopActiveError):
                await panic.resume_after_emergency_stop(
                    session,
                    actor=await _get(session, type(alice), alice.id),
                    scope="self",
                    reason="mine back please",
                )
            await session.rollback()
            toggles = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "toggle",
                        AuditLog.details["setting"].astext == "ai_enabled",
                    )
                )
            ).scalars().all()
            assert [t.details["to"] for t in toggles] == [False]

            actor = await _get(session, type(admin), admin.id)
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["self_paused_assignments"] == 0
            assert status["platform_paused_assignments"] == 2
            assert status["ai_disabled"] is True
            assert status["platform_stop_active"] is True
            assert status["active"] is True
            assert status["last_stop"]["scope"] == "platform"
            # A non-admin sees the platform stop too: their assignments carry it.
            alice_status = await panic.emergency_stop_status(
                session, actor=await _get(session, type(alice), alice.id)
            )
            assert alice_status["active"] is True
            assert alice_status["platform_stop_active"] is True
            assert alice_status["self_paused_assignments"] == 1
            assert alice_status["last_stop"]["by"] == actor_display_name(admin)
            # ...and so does a user who owns NOTHING the stop could pause.
            carol = await _user(session)
            await session.commit()
            carol_status = await panic.emergency_stop_status(session, actor=carol)
            assert carol_status["self_paused_assignments"] == 0
            assert carol_status["platform_stop_active"] is True
            assert carol_status["active"] is True

        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            resumed = await panic.resume_after_emergency_stop(
                session, actor=actor, scope="platform", reason="All clear"
            )
            await session.commit()
        assert resumed.assignments_resumed == 2
        assert resumed.ai_enabled is True

        async with sessions() as session:
            for world in (a, b):
                row = await _get(session, Assignment, world["assignment"].id)
                assert row.enabled is True
                assert row.paused_reason is None
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            # Back to "follow the deployment mode", never forced ON.
            assert ps.ai_disabled is None
            actor = await _get(session, type(admin), admin.id)
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["platform_paused_assignments"] == 0
            assert status["ai_disabled"] is False
            assert status["platform_stop_active"] is False
            assert status["active"] is False
            assert status["last_stop"] is None

        # AI switched off in Settings BEFORE a platform stop: the resume puts
        # that back (AI stays off), it does not flip AI on over the admin's head.
        async with sessions() as session:
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            ps.ai_disabled = True
            await session.commit()
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            armed = await panic.arm_admission_barrier(session, actor=actor, scope="platform")
            await session.commit()
        async with sessions() as session:
            row = await _get(session, AuditLog, armed.audit_id)
            assert row.details["ai_override_before_stop"] is False  # wire: AI was OFF
            # The stubs wrap the phase that touches Redis (the chat-turn arm).
            with list_patch, cancel_patch:
                actor = await _get(session, type(admin), admin.id)
                await panic.emergency_stop(
                    session, actor=actor, scope="platform", outcome=armed
                )
                await session.commit()
        # A SECOND platform stop while the first is open (a token stop after a
        # UI stop) records the False the first one wrote — the resume must
        # still restore what the administrator had before the sequence began.
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            second = await panic.arm_admission_barrier(session, actor=actor, scope="platform")
            await session.commit()
        async with sessions() as session:
            assert (await _get(session, AuditLog, second.audit_id)).details[
                "ai_override_before_stop"
            ] is False
            assert await panic._ai_override_before_stop(session) is False
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            lifted = await panic.resume_after_emergency_stop(
                session, actor=actor, scope="platform", reason="over"
            )
            await session.commit()
        assert lifted.ai_enabled is False
        async with sessions() as session:
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.ai_disabled is True  # restored to the pre-stop setting
            ps.ai_disabled = None
            await session.commit()

        # The same sequence starting from AI ON: the second stop's row says
        # False, the FIRST stop's row says None, and None is what comes back.
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            first = await panic.arm_admission_barrier(session, actor=actor, scope="platform")
            await session.commit()
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            await panic.arm_admission_barrier(session, actor=actor, scope="platform")
            await session.commit()
        async with sessions() as session:
            assert (await _get(session, AuditLog, first.audit_id)).details[
                "ai_override_before_stop"
            ] is None
            assert await panic._ai_override_before_stop(session) is None
            actor = await _get(session, type(admin), admin.id)
            await panic.resume_after_emergency_stop(
                session, actor=actor, scope="platform", reason="over"
            )
            await session.commit()
        async with sessions() as session:
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.ai_disabled is None  # back to the deployment default

        # An administrator turning AI off from SETTINGS is not an emergency:
        # no banner, and a resume leaves that deliberate override alone.
        async with sessions() as session:
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            ps.ai_disabled = True
            await session.commit()
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["ai_disabled"] is False  # not the stop's doing
            assert status["active"] is False
            lifted = await panic.resume_after_emergency_stop(
                session, actor=actor, scope="platform", reason="nothing to lift"
            )
            await session.commit()
        assert lifted.ai_enabled is False
        async with sessions() as session:
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.ai_disabled is True  # the administrator's choice stands


async def test_platform_scope_is_refused_to_a_regular_user_without_touching_rows():
    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _user(session)
            a = await _world(session, alice)
            await session.commit()
        async with sessions() as session:
            actor = await _get(session, type(alice), alice.id)
            with pytest.raises(PermissionError):
                await panic.emergency_stop(session, actor=actor, scope="platform")
            await session.rollback()
        async with sessions() as session:
            assert (await _get(session, AssignmentRun, a["parent"].id)).status == "running"
            assert (await _get(session, Assignment, a["assignment"].id)).enabled is True


async def test_the_emergency_token_presses_the_platform_stop_without_a_session():
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            alice = await _user(session)
            a = await _world(session, alice)
            await session.commit()

        # Nothing configured: the token path is closed and says so in the trail.
        async with sessions() as session:
            assert await panic.resolve_emergency_token(session, "xes-anything") is None
            status = await panic.emergency_token_status(session)
            assert status["configured"] is False
            await session.commit()

        # An administrator mints it; only the hash and the prefix stay.
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            created = await panic.generate_emergency_token(session, actor=actor)
            await session.commit()
        raw = created["token"]
        assert raw.startswith(panic.EMERGENCY_TOKEN_PREFIX) and len(raw) == 52
        async with sessions() as session:
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.emergency_stop_token_hash and raw not in ps.emergency_stop_token_hash
            assert ps.emergency_stop_token_prefix == raw[:12] + "..."
            assert ps.emergency_stop_token_created_by == admin.email
            status = await panic.emergency_token_status(session)
            assert status["configured"] is True and status["last_used_at"] is None

        # A wrong token is refused and recorded; the right one resolves to the
        # minting administrator and stamps last_used_at.
        list_patch, cancel_patch = _stub_redis([])
        async with sessions() as session:
            # Always a DIFFERENT last character: replacing it with a fixed "0"
            # reproduced the real token whenever it already ended in "0" (1 in 16).
            tampered = raw[:-1] + ("1" if raw[-1] == "0" else "0")
            assert tampered != raw
            assert await panic.resolve_emergency_token(session, tampered) is None
            actor = await panic.resolve_emergency_token(session, raw)
            assert actor is not None and actor.id == admin.id
            with list_patch, cancel_patch:
                outcome = await panic.emergency_stop_with_token(
                    session, actor=actor, reason="Runbook step 3: agent looping"
                )
            await session.commit()
        assert outcome.scope == "platform"
        assert outcome.ai_disabled is True
        assert outcome.runs_cancelled == 2

        async with sessions() as session:
            assert (await _get(session, AssignmentRun, a["parent"].id)).status == "cancelled"
            row = await _get(session, Assignment, a["assignment"].id)
            assert row.paused_reason == PANIC_STOP_REASON
            assert row.paused_details.endswith("Reason: Runbook step 3: agent looping")
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.ai_disabled is True
            assert ps.emergency_stop_token_last_used_at is not None
            audit = await _get(session, AuditLog, outcome.audit_id)
            assert audit.user_id == admin.id
            assert audit.details["via"] == "api_token"
            assert audit.details["reason"] == "Runbook step 3: agent looping"
            rejected = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == panic.SECURITY_EVENT_TOKEN_REJECTED
                    )
                )
            ).scalars().all()
            assert len(rejected) == 2  # unconfigured probe + wrong token

        # The minting administrator is demoted: the token keeps working, now
        # attributed to another active administrator rather than failing.
        async with sessions() as session:
            other_admin = await _user(session, role="admin")
            minting = await _get(session, type(admin), admin.id)
            minting.role = "user"
            await session.commit()
        async with sessions() as session:
            actor = await panic.resolve_emergency_token(session, raw)
            assert actor is not None and actor.id == other_admin.id
            await session.rollback()
        async with sessions() as session:
            (await _get(session, type(admin), admin.id)).role = "admin"
            await session.commit()

        # The minting administrator is DEACTIVATED: attribution moves to a live
        # administrator while one exists...
        async with sessions() as session:
            (await _get(session, type(admin), admin.id)).is_active = False
            await session.commit()
        async with sessions() as session:
            actor = await panic.resolve_emergency_token(session, raw)
            assert actor is not None and actor.id == other_admin.id
            await session.rollback()
        # ...and when NO administrator is active, the printed procedure still
        # works (it is the way in when nobody can sign in), attributed to the
        # deactivated minter — not to the oldest admin, which is someone else.
        async with sessions() as session:
            minted_at = (await _get(session, type(admin), admin.id)).created_at
            other = await _get(session, type(admin), other_admin.id)
            other.is_active = False
            other.created_at = minted_at - timedelta(days=1)
            await session.commit()
        async with sessions() as session:
            actor = await panic.resolve_emergency_token(session, raw)
            assert actor is not None and actor.id == admin.id
            await session.rollback()
        async with sessions() as session:
            (await _get(session, type(admin), admin.id)).is_active = True
            (await _get(session, type(admin), other_admin.id)).is_active = True
            await session.commit()

        # Revoking closes the path again.
        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            assert await panic.revoke_emergency_token(session, actor=actor) is True
            assert await panic.revoke_emergency_token(session, actor=actor) is False
            await session.commit()
        async with sessions() as session:
            assert await panic.resolve_emergency_token(session, raw) is None
            assert (await panic.emergency_token_status(session))["configured"] is False


async def test_late_admissions_are_swept_once_the_stop_is_durable():
    """A run created between the sweep and the commit (a scheduler tick on
    another replica that still saw ``enabled = True``) is caught afterwards."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _user(session)
            bob = await _user(session)
            a = await _world(session, alice)
            b = await _world(session, bob)
            await session.commit()

        list_patch, cancel_patch = _stub_redis([])
        async with sessions() as session:
            with list_patch, cancel_patch:
                actor = await _get(session, type(alice), alice.id)
                outcome = await panic.emergency_stop(session, actor=actor, scope="self")
                await session.commit()

        # The race, reproduced: a run admitted after the stop's timestamp.
        async with sessions() as session:
            alice_row = await _get(session, type(alice), alice.id)
            alice_row.agent_id = a["parent"].agent_id
            late_alice = await _run(session, alice_row, assignment=a["assignment"])
            late_alice.created_at = outcome.stopped_at + timedelta(seconds=1)
            bob_row = await _get(session, type(bob), bob.id)
            bob_row.agent_id = b["parent"].agent_id
            late_bob = await _run(session, bob_row, assignment=b["assignment"])
            late_bob.created_at = outcome.stopped_at + timedelta(seconds=1)
            await session.commit()

        # The sweep opens its own session: point the app's factory at ours.
        from app.core import database as core_db

        with patch.object(core_db, "async_session", sessions), patch(
            "app.a2a.client.delegation.spawn_run_cancel"
        ) as spawn:
            before = outcome.runs_cancelled
            swept = await panic.sweep_late_admissions(outcome)
        assert swept == 1
        assert outcome.runs_cancelled == before + 1
        spawn.assert_called_once()

        async with sessions() as session:
            row = await _get(session, AssignmentRun, late_alice.id)
            assert row.status == "cancelled"
            assert row.error == panic.STOP_MESSAGE
            # Bob's late run is out of alice's scope and untouched.
            assert (await _get(session, AssignmentRun, late_bob.id)).status == "running"
            # The trail counts it too.
            audit = await _get(session, AuditLog, outcome.audit_id)
            assert audit.details["late_admissions_cancelled"] == 1
            assert audit.details["runs_cancelled"] == before + 1


async def test_the_delayed_pass_of_a_self_stop_spares_a_run_the_user_starts_after_it():
    """A ``self`` stop does not gate new work: a run the user starts
    after the first pass (a chat turn consulting another agent) survives the
    delayed pass, which still cancels a run of an assignment the stop paused,
    a child of a run it cancelled, and a run flushed before the first pass
    but committed after it."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _user(session)
            a = await _world(session, alice)
            await session.commit()

        from app.core import database as core_db

        list_patch, cancel_patch = _stub_redis([])
        async with sessions() as session:
            with list_patch, cancel_patch:
                actor = await _get(session, type(alice), alice.id)
                outcome = await panic.emergency_stop(session, actor=actor, scope="self")
                await session.commit()
        with patch.object(core_db, "async_session", sessions), patch(
            "app.a2a.client.delegation.spawn_run_cancel"
        ):
            assert await panic.sweep_late_admissions(outcome) == 0
        assert outcome.first_swept_at is not None

        async with sessions() as session:
            alice_row = await _get(session, type(alice), alice.id)
            alice_row.agent_id = a["parent"].agent_id
            after = outcome.first_swept_at + timedelta(seconds=1)
            # The user's own new work: no assignment, no stopped parent.
            own = await _run(session, alice_row)
            own.created_at = after
            own_child = await _run(session, alice_row, parent=own.id)
            own_child.created_at = after
            # A scheduler that loaded the assignment before the pause.
            scheduled = await _run(session, alice_row, assignment=a["assignment"])
            scheduled.created_at = after
            # A run the stop cancelled, still winding down.
            orphan = await _run(session, alice_row, parent=a["parent"].id)
            orphan.created_at = after
            # Flushed before the first pass read the table, committed after.
            flushed_before = await _run(session, alice_row)
            flushed_before.created_at = outcome.first_swept_at - timedelta(
                milliseconds=1
            )
            await session.commit()

        with (
            patch.object(core_db, "async_session", sessions),
            patch("app.a2a.client.delegation.spawn_run_cancel"),
        ):
            await panic._delayed_sweep(outcome, 0)
        assert outcome.late_admissions_cancelled == 3

        async with sessions() as session:
            for kept in (own, own_child):
                row = await _get(session, AssignmentRun, kept.id)
                assert row.status == "running"
                assert row.error is None
            for swept in (scheduled, orphan, flushed_before):
                row = await _get(session, AssignmentRun, swept.id)
                assert row.status == "cancelled"
                assert row.error == panic.STOP_MESSAGE
            audit = await _get(session, AuditLog, outcome.audit_id)
            assert audit.details["late_admissions_cancelled"] == 3


async def test_the_delayed_pass_of_a_platform_stop_still_cancels_every_run():
    """With AI off nothing is legitimately live: the platform scope keeps the
    unbounded sweep, whenever the run was created."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            bob = await _user(session)
            await session.commit()

        from app.core import database as core_db

        list_patch, cancel_patch = _stub_redis([])
        async with sessions() as session:
            with list_patch, cancel_patch:
                actor = await _get(session, type(admin), admin.id)
                outcome = await panic.emergency_stop(
                    session, actor=actor, scope="platform"
                )
                await session.commit()
        with (
            patch.object(core_db, "async_session", sessions),
            patch("app.a2a.client.delegation.spawn_run_cancel"),
            list_patch,
            cancel_patch,
        ):
            assert await panic.sweep_late_admissions(outcome) == 0

        async with sessions() as session:
            late = await _run(session, bob)
            late.created_at = outcome.first_swept_at + timedelta(seconds=1)
            await session.commit()

        with (
            patch.object(core_db, "async_session", sessions),
            patch("app.a2a.client.delegation.spawn_run_cancel"),
            list_patch,
            cancel_patch,
        ):
            await panic._delayed_sweep(outcome, 0)
        assert outcome.late_admissions_cancelled == 1
        async with sessions() as session:
            assert (await _get(session, AssignmentRun, late.id)).status == "cancelled"


async def test_a_sub_flow_a_cancelled_run_started_during_the_stop_is_swept():
    """A ``flow.call`` node already running when the stop cancelled its run can
    insert the child after the stop's selection: the sweep cancels it, and
    the child it started in turn, and leaves another user's sub-flow alone."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _user(session)
            bob = await _user(session)
            a = await _world(session, alice)
            b = await _world(session, bob)
            await session.commit()

        list_patch, cancel_patch = _stub_redis([])
        async with sessions() as session:
            with list_patch, cancel_patch:
                actor = await _get(session, type(alice), alice.id)
                outcome = await panic.emergency_stop(session, actor=actor, scope="self")
                await session.commit()

        def _child(parent_run, user, *, depth):
            return FlowRun(
                id=uuid.uuid4(),
                flow_id=parent_run.flow_id,
                flow_version_number=1,
                graph={},
                status="pending",
                trigger_type="flow_call",
                depth=depth,
                parent_run_id=parent_run.id,
                user_id=user.id,
                is_company_managed=False,
                created_at=outcome.stopped_at + timedelta(seconds=1),
            )

        async with sessions() as session:
            child = _child(a["flow_run"], alice, depth=1)
            session.add(child)
            await session.flush()
            grandchild = _child(child, alice, depth=2)
            # Bob's run was not stopped: its sub-flow is live work.
            bobs_child = _child(b["flow_run"], bob, depth=1)
            session.add_all([grandchild, bobs_child])
            await session.flush()
            child_exec = await _execution(
                session,
                adapter="flow",
                dedup_key=f"flow_run:{child.id}",
                payload={"run_id": str(child.id)},
                state="pending",
            )
            await session.commit()

        from app.core import database as core_db

        with (
            patch.object(core_db, "async_session", sessions),
            patch("app.a2a.client.delegation.spawn_run_cancel"),
            patch(
                "app.services.flow_run_stream.publish_flow_run_update", AsyncMock()
            ),
        ):
            before = outcome.flow_runs_cancelled
            await panic.sweep_late_admissions(outcome)
        assert outcome.flow_runs_cancelled == before + 2

        async with sessions() as session:
            assert (await _get(session, FlowRun, child.id)).status == "cancelled"
            assert (await _get(session, FlowRun, grandchild.id)).status == "cancelled"
            assert (await _get(session, FlowRun, bobs_child.id)).status == "pending"
            assert (
                await _get(session, OrchestrationExecution, child_exec.id)
            ).state == "cancelled"
            audit = await _get(session, AuditLog, outcome.audit_id)
            assert audit.details["flow_runs_cancelled"] == before + 2


async def test_a_platform_resume_leaves_a_users_own_stop_in_place():
    """A user who stopped their own activity keeps it stopped when an
    administrator lifts a later platform stop: the platform resume re-enables
    what the platform stop paused, the user lifts their own."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            alice = await _user(session)
            own = await _assignment(session, alice)
            await session.commit()

        list_patch, cancel_patch = _stub_redis([])
        with list_patch, cancel_patch:
            async with sessions() as session:
                actor = await _get(session, type(alice), alice.id)
                await panic.emergency_stop(session, actor=actor, scope="self")
                await session.commit()
            async with sessions() as session:
                # Created after alice's stop: only the platform stop pauses it.
                later = await _assignment(session, alice)
                await session.commit()
            async with sessions() as session:
                actor = await _get(session, type(admin), admin.id)
                await panic.emergency_stop(session, actor=actor, scope="platform")
                await session.commit()

        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["platform_paused_assignments"] == 1
            resumed = await panic.resume_after_emergency_stop(
                session, actor=actor, scope="platform", reason="All clear"
            )
            await session.commit()
        assert resumed.assignments_resumed == 1

        async with sessions() as session:
            assert (await _get(session, Assignment, later.id)).enabled is True
            kept = await _get(session, Assignment, own.id)
            assert kept.enabled is False
            assert kept.paused_reason == PANIC_STOP_REASON
            actor = await _get(session, type(alice), alice.id)
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["self_paused_assignments"] == 1
            assert status["active"] is True
            # The banner names the stop still in effect, hers, not the more
            # recent platform stop that was lifted.
            assert status["last_stop"]["scope"] == "self"
            assert status["last_stop"]["by"] == actor_display_name(actor)
            resumed = await panic.resume_after_emergency_stop(
                session, actor=actor, scope="self", reason="Mine is fixed too"
            )
            await session.commit()
        assert resumed.assignments_resumed == 1
        async with sessions() as session:
            assert (await _get(session, Assignment, own.id)).enabled is True


async def test_a_platform_sweep_cancels_tasks_and_turns_admitted_after_the_stop():
    """With AI off nothing agentic may be live: a background task admitted
    through a still-warm AI cache would be held and run at the resume, and a
    chat turn would run to its end. The platform pass catches both."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            alice = await _user(session)
            await session.commit()

        list_patch, cancel_patch = _stub_redis([])
        async with sessions() as session:
            with list_patch, cancel_patch:
                actor = await _get(session, type(admin), admin.id)
                outcome = await panic.emergency_stop(
                    session, actor=actor, scope="platform"
                )
                await session.commit()

        async with sessions() as session:
            late_task = SubAgentTask(
                id=uuid.uuid4(),
                user_id=alice.id,
                parent_agent_id=alice.agent_id,
                target_agent_id=alice.agent_id,
                task_description="spawned by a late turn",
                status="pending",
                metadata_json={},
            )
            session.add(late_task)
            await session.commit()

        from app.core import database as core_db

        late_conversation = str(uuid.uuid4())
        list_patch, cancel_patch = _stub_redis([late_conversation])
        with (
            patch.object(core_db, "async_session", sessions),
            patch("app.a2a.client.delegation.spawn_run_cancel"),
            patch.object(panic, "_cancel_conversation_dependants", AsyncMock()),
            list_patch,
            cancel_patch as cancel,
        ):
            await panic.sweep_late_admissions(outcome)
        cancel.assert_awaited_once_with(late_conversation)
        assert outcome.sub_agent_tasks_cancelled == 1
        assert outcome.chat_turns_cancelled == 1

        async with sessions() as session:
            row = await _get(session, SubAgentTask, late_task.id)
            assert row.status == "cancelled"
            assert row.error == panic.STOP_MESSAGE
            assert row.metadata_json["delivery_suppressed"] is True
            audit = await _get(session, AuditLog, outcome.audit_id)
            assert audit.details["sub_agent_tasks_cancelled"] == 1
            assert audit.details["chat_turns_cancelled"] == 1


# ---------------------------------------------------------------------------
# Review follow-ups (fred-lr): overlapping stops, commit timing, runner
# commands
# ---------------------------------------------------------------------------


async def _self_stop(sessions, user):
    list_patch, cancel_patch = _stub_redis([])
    async with sessions() as session:
        with list_patch, cancel_patch:
            actor = await _get(session, type(user), user.id)
            outcome = await panic.emergency_stop(session, actor=actor, scope="self")
            await session.commit()
    return outcome


async def _self_resume(sessions, user):
    async with sessions() as session:
        actor = await _get(session, type(user), user.id)
        outcome = await panic.resume_after_emergency_stop(
            session, actor=actor, scope="self", reason="all clear"
        )
        await session.commit()
    return outcome


async def _status(sessions, user):
    async with sessions() as session:
        actor = await _get(session, type(user), user.id)
        return await panic.emergency_stop_status(session, actor=actor)


@pytest.mark.parametrize("first", ["owner", "run_as"])
async def test_a_self_resume_leaves_what_another_users_open_stop_covers(first):
    """An assignment in two users' scopes (owned by one, run as the other) is
    covered by both their stops, whichever paused it: the first to lift theirs
    leaves it paused, and it comes back when the other lifts theirs too."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            alice = await _user(session)
            shared = await _assignment(session, admin, run_as=alice, company=True)
            await session.commit()

        order = [admin, alice] if first == "owner" else [alice, admin]
        for user in order:
            await _self_stop(sessions, user)

        # Alice lifts hers first: the admin's stop still covers the assignment.
        status = await _status(sessions, alice)
        assert status["self_paused_assignments"] == 0
        assert status["self_stop_active"] is True  # her resume stays on offer
        assert status["active"] is True
        outcome = await _self_resume(sessions, alice)
        assert outcome.assignments_resumed == 0
        async with sessions() as session:
            assert (await _get(session, Assignment, shared.id)).enabled is False

        # The admin lifts theirs: nothing covers it any more.
        assert (await _status(sessions, admin))["self_paused_assignments"] == 1
        outcome = await _self_resume(sessions, admin)
        assert outcome.assignments_resumed == 1
        async with sessions() as session:
            assert (await _get(session, Assignment, shared.id)).enabled is True
        assert (await _status(sessions, alice))["active"] is False


async def test_an_open_stop_does_not_hold_what_a_later_stop_paused():
    """A stop covers what was paused when it was pressed. One left open and
    forgotten never holds an assignment a later stop paused."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            bob = await _user(session)
            await session.commit()
        await _self_stop(sessions, bob)  # nothing of his to pause yet, never lifted
        async with sessions() as session:
            later = await _assignment(session, admin, run_as=bob, company=True)
            await session.commit()

        await _self_stop(sessions, admin)
        outcome = await _self_resume(sessions, admin)
        assert outcome.assignments_resumed == 1
        async with sessions() as session:
            assert (await _get(session, Assignment, later.id)).enabled is True


async def test_the_sweep_catches_a_run_flushed_before_the_stop_and_committed_after():
    """``created_at`` is stamped at the flush: a run flushed just before the
    stop's timestamp but committed after phase 2 read the live runs is still
    swept."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _user(session)
            a = await _world(session, alice)
            await session.commit()
        outcome = await _self_stop(sessions, alice)

        async with sessions() as session:
            alice_row = await _get(session, type(alice), alice.id)
            alice_row.agent_id = a["parent"].agent_id
            straggler = await _run(session, alice_row, assignment=a["assignment"])
            straggler.created_at = outcome.stopped_at - timedelta(seconds=1)
            await session.commit()

        from app.core import database as core_db

        with patch.object(core_db, "async_session", sessions), patch(
            "app.a2a.client.delegation.spawn_run_cancel"
        ):
            swept = await panic.sweep_late_admissions(outcome)
        assert swept == 1
        async with sessions() as session:
            assert (await _get(session, AssignmentRun, straggler.id)).status == "cancelled"


async def test_the_stop_gives_up_runner_commands_through_the_transport():
    """A queued command is cancelled, a dispatched one is marked given up (it
    is never delivered again), and the command of a cancelled flow run is
    released even when another user dispatched it."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _user(session)
            bob = await _user(session)
            a = await _world(session, alice)
            runner = (
                await session.execute(
                    select(Runner).where(Runner.id == a["command"].runner_id)
                )
            ).scalar_one()
            dispatched = RunnerCommand(
                id=uuid.uuid4(),
                runner_id=runner.id,
                user_id=alice.id,
                kind="shell",
                payload={"command": "sleep 60"},
                status="dispatched",
                dispatched_at=datetime.now(UTC),
            )
            of_the_flow = RunnerCommand(
                id=uuid.uuid4(),
                runner_id=runner.id,
                user_id=bob.id,
                kind="shell",
                payload={"command": "ls"},
                status="pending",
                flow_run_id=a["flow_run"].id,
            )
            session.add_all([dispatched, of_the_flow])
            await session.commit()

        outcome = await _self_stop(sessions, alice)
        assert outcome.runner_commands_cancelled == 1
        assert outcome.finished_runner_command_ids == [a["command"].id]
        async with sessions() as session:
            queued = await _get(session, RunnerCommand, a["command"].id)
            assert queued.status == "cancelled"
            assert queued.error == panic.STOP_MESSAGE
            held = await _get(session, RunnerCommand, dispatched.id)
            assert held.status == "dispatched"
            assert held.given_up_at is not None
            released = await _get(session, RunnerCommand, of_the_flow.id)
            assert released.status == "cancelled"


async def test_an_open_platform_stop_outlives_the_audit_rows_of_its_presser():
    """Deleting the administrator who pressed a platform stop cascades
    its ``panic_stop`` row away. The stop stays in effect — banner, refused
    ``self`` resume — and the platform resume still re-enables what it paused
    and puts the AI override back. An earlier, lifted platform stop whose rows
    survive must not stand in for it on the banner."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            presser = await _user(session, role="admin")
            alice = await _user(session)
            held = await _assignment(session, alice)
            await session.commit()

        list_patch, cancel_patch = _stub_redis([])
        with list_patch, cancel_patch:
            # An earlier platform stop, lifted: its rows stay on the trail.
            async with sessions() as session:
                actor = await _get(session, type(admin), admin.id)
                await panic.emergency_stop(session, actor=actor, scope="platform")
                await session.commit()
            async with sessions() as session:
                actor = await _get(session, type(admin), admin.id)
                await panic.resume_after_emergency_stop(
                    session, actor=actor, scope="platform", reason="earlier one over"
                )
                await session.commit()
            async with sessions() as session:
                actor = await _get(session, type(presser), presser.id)
                outcome = await panic.emergency_stop(
                    session, actor=actor, scope="platform"
                )
                await session.commit()
        assert outcome.assignments_paused == 1

        async with sessions() as session:
            await session.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": presser.id}
            )
            await session.commit()

        async with sessions() as session:
            stops = (
                await session.execute(
                    select(AuditLog.user_id).where(
                        AuditLog.action == panic.AUDIT_ACTION_STOP
                    )
                )
            ).scalars().all()
            assert stops == [admin.id]  # the presser's row went with them
            assert await panic.platform_stop_open(session) is True
            row = await _get(session, Assignment, held.id)
            assert row.enabled is False
            assert row.paused_reason == PANIC_STOP_REASON
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.ai_disabled is True
            alice_row = await _get(session, type(alice), alice.id)
            status = await panic.emergency_stop_status(session, actor=alice_row)
            assert status["platform_stop_active"] is True
            assert status["active"] is True
            assert status["self_paused_assignments"] == 1
            assert status["last_stop"] == {
                "at": outcome.stopped_at.isoformat(),
                "by": None,
                "scope": "platform",
                "reason": None,
            }
            with pytest.raises(panic.PlatformStopActiveError):
                await panic.resume_after_emergency_stop(
                    session, actor=alice_row, scope="self", reason="mine back please"
                )
            await session.rollback()

        async with sessions() as session:
            actor = await _get(session, type(admin), admin.id)
            status = await panic.emergency_stop_status(session, actor=actor)
            assert status["platform_paused_assignments"] == 1
            assert status["ai_disabled"] is True
            resumed = await panic.resume_after_emergency_stop(
                session, actor=actor, scope="platform", reason="All clear"
            )
            await session.commit()
        assert resumed.assignments_resumed == 1
        assert resumed.ai_enabled is True

        async with sessions() as session:
            row = await _get(session, Assignment, held.id)
            assert row.enabled is True
            assert row.paused_reason is None
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            assert ps.ai_disabled is None  # the pre-stop override, restored
            assert ps.platform_stop_at is None
            assert ps.platform_stop_times is None
            assert ps.platform_stop_ai_override is None
            assert await panic.platform_stop_open(session) is False
            status = await panic.emergency_stop_status(
                session, actor=await _get(session, type(alice), alice.id)
            )
            assert status["active"] is False
            assert status["last_stop"] is None


_STOP_STATE_REVISION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "p5251stop01_add_platform_settings_platform_stop_state.py"
)


def _run_stop_state_upgrade(sync_conn) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location(
        _STOP_STATE_REVISION.stem, _STOP_STATE_REVISION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(sync_conn)):
        module.upgrade()


@pytest.mark.parametrize("open_at_upgrade", [True, False])
async def test_the_migration_carries_a_platform_stop_open_at_upgrade_over(
    open_at_upgrade,
):
    """A deployment upgraded during a platform stop keeps it: the revision
    copies the open sequence off the trail (first stop's ``stopped_at`` and
    pre-stop AI override, every stop's ``stopped_at``); a lifted one leaves
    the columns empty."""
    from app.api.v1.platform_settings_helpers import get_platform_settings

    base = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    lifted, first, second = (base + timedelta(hours=h) for h in (0, 2, 3))
    async with _temp_database() as sessions:
        async with sessions() as session:
            admin = await _user(session, role="admin")
            await get_platform_settings(session)

            def _row(action, at, details=None):
                return AuditLog(
                    user_id=admin.id,
                    user_email=admin.email,
                    action=action,
                    entity_type="platform",
                    summary=action,
                    details=details,
                    created_at=at,
                )

            session.add_all(
                [
                    _row(
                        panic.AUDIT_ACTION_STOP,
                        lifted,
                        {"stopped_at": lifted.isoformat(), "ai_override_before_stop": False},
                    ),
                    _row(panic.AUDIT_ACTION_RESUME, lifted + timedelta(hours=1)),
                    _row(
                        panic.AUDIT_ACTION_STOP,
                        first,
                        {"stopped_at": first.isoformat(), "ai_override_before_stop": True},
                    ),
                    _row(
                        panic.AUDIT_ACTION_STOP,
                        second,
                        {"stopped_at": second.isoformat(), "ai_override_before_stop": False},
                    ),
                ]
            )
            if not open_at_upgrade:
                session.add(
                    _row(panic.AUDIT_ACTION_RESUME, second + timedelta(hours=1))
                )
            await session.commit()

        engine = sessions.kw["bind"]
        # The state an existing install is in before the revision: no columns.
        async with engine.begin() as conn:
            for column in (
                "platform_stop_at",
                "platform_stop_times",
                "platform_stop_ai_override",
            ):
                await conn.execute(
                    text(f"ALTER TABLE platform_settings DROP COLUMN {column}")
                )
        async with engine.begin() as conn:
            await conn.run_sync(_run_stop_state_upgrade)
        # A re-run is a no-op.
        async with engine.begin() as conn:
            await conn.run_sync(_run_stop_state_upgrade)

        async with sessions() as session:
            ps = (await session.execute(select(PlatformSettings))).scalars().one()
            if open_at_upgrade:
                assert ps.platform_stop_at == first
                assert ps.platform_stop_times == [first.isoformat(), second.isoformat()]
                assert ps.platform_stop_ai_override is True
                assert await panic.platform_stop_open(session) is True
                assert await panic._ai_override_before_stop(session) is True
                assert sorted(await panic._platform_stop_times(session)) == [
                    lifted,
                    first,
                    second,
                ]
            else:
                assert ps.platform_stop_at is None
                assert ps.platform_stop_times is None
                assert ps.platform_stop_ai_override is None
                assert await panic.platform_stop_open(session) is False

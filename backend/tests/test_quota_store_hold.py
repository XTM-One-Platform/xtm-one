"""A due background run waits out a quota-store outage instead of failing.

Quotas fail closed in XTM One mode: while the quota store cannot be
reached, no agentic run goes unmetered. A due assignment run (schedule,
webhook, integration event, work-item delegation) or delegated task that meets
the outage before anything was charged is HELD, exactly like work waiting for
agentic AI: its execution goes back to wait through
``orchestration_engine.defer`` with the capped backoff, the run carries the
``ai_hold`` marker, it is charged once when it finally runs, and it is never a
failure - so never a slot of the agent failure pool. Past
``AI_UNAVAILABLE_HOLD_MAX_SECONDS`` it fails with the reason. Chat turns are
not held (out of scope). A store refusing the platform's credentials or
permissions is a configuration error, not an outage: that work meets the
refusal it always met, at once.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import (
    AuthenticationError,
    AuthenticationWrongNumberOfArgsError,
    AuthorizationError,
    BusyLoadingError,
    ClusterDownError,
    ExternalAuthProviderError,
    MasterDownError,
    NoPermissionError,
    OutOfMemoryError,
    ReadOnlyError,
    TryAgainError,
)
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings
from app.services import ai_hold, orchestration_engine
from app.services.agent_chat import run_agent_chat as real_run_agent_chat
from app.services.agentic_gate import AgenticVerdict
from app.services.failure_causes import FailureCause
from app.services.quota import QUOTA_UNAVAILABLE_MESSAGE
from app.services.quota import quota_store_out as real_quota_store_out
from app.services.quota import quota_store_outage as real_quota_store_outage
from tests.test_ai_unavailable_hold import _execution
from tests.test_background_edition_gate import (
    ENTERPRISE,
    _patch_task_collaborators,
    _pin,
    _run_stubs,
    _Session,
    _task_row,
    _TaskSession,
)

QUOTA = "quota_unavailable"
QUOTA_HELD = ai_hold.held_adapter_state(QUOTA)

TRIGGERS = pytest.mark.parametrize(
    "trigger_type", ["schedule", "webhook", "integration_event", "work_item"]
)


class _Started(BaseException):
    """Stops the engine once the run went past the quota, charged."""


def _store_down():
    return RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")


@pytest.fixture
def xtm_one(monkeypatch):
    """XTM One mode, agentic AI licensed and on: quotas fail closed."""
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    _pin(monkeypatch, **ENTERPRISE)


def _copilot_open(monkeypatch):
    """Copilot mode with ``QUOTA_FAIL_MODE=open``: an outage runs unmetered."""
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    monkeypatch.setattr(settings, "quota_fail_mode", "open")
    monkeypatch.setattr(settings, "_environment_fields", frozenset({"quota_fail_mode"}))
    _pin(monkeypatch, **ENTERPRISE)


def _engine(monkeypatch, run, assignment, agent):
    """The run's collaborators around the quota, recorded."""
    from app.services import assignment_engine

    monkeypatch.setattr(
        assignment_engine, "async_session", lambda: _Session([run, assignment, agent])
    )
    spies = SimpleNamespace(
        increment=AsyncMock(),
        executions=AsyncMock(side_effect=_Started),
        publish=AsyncMock(),
        pool=AsyncMock(),
        free=AsyncMock(),
    )
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", spies.increment)
    monkeypatch.setattr(assignment_engine, "record_agentic_execution", spies.executions)
    monkeypatch.setattr(assignment_engine, "_publish_run_status", spies.publish)
    monkeypatch.setattr("app.services.quota.refund_agentic_usage_for_failure", spies.pool)
    monkeypatch.setattr(
        "app.services.quota.refund_agentic_usage_for_provider_outage", spies.free
    )
    return assignment_engine, spies


def _check(monkeypatch, *, down: bool):
    check = AsyncMock(side_effect=_store_down() if down else None)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", check)
    return check


# -- Assignment runs, whatever triggered them ---------------------------------


@pytest.mark.asyncio
@TRIGGERS
async def test_a_due_run_is_held_through_a_store_outage_then_charged_once(
    monkeypatch, xtm_one, trigger_type
):
    run, assignment, agent = _run_stubs()
    run.trigger_context = {"type": trigger_type}
    engine, spies = _engine(monkeypatch, run, assignment, agent)

    _check(monkeypatch, down=True)
    outcome = await engine.execute_assignment_run(str(run.id), hold=True)

    assert outcome == engine.RunHeld(QUOTA)
    assert run.status == "pending"
    assert run.error is None and run.failure_cause is None and run.completed_at is None
    assert run.started_at is None
    assert ai_hold.held_code_of(run.trigger_context) == QUOTA
    assert ai_hold.held_reason_of(run.trigger_context) == ai_hold.held_message(QUOTA)
    assert "quota_metered" not in run.trigger_context
    spies.increment.assert_not_awaited()
    spies.executions.assert_not_awaited()
    spies.publish.assert_not_awaited()

    # The store answers again: the run goes ahead, charged exactly once.
    check = _check(monkeypatch, down=False)
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    with pytest.raises(_Started):
        await engine.execute_assignment_run(str(run.id), hold=True)

    check.assert_awaited_once()
    spies.increment.assert_awaited_once()
    assert run.trigger_context["quota_metered"] is True
    assert run.trigger_context["type"] == trigger_type
    assert ai_hold.HOLD_MARKER_KEY not in run.trigger_context
    spies.pool.assert_not_awaited()
    spies.free.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_asked_again_while_the_store_is_out_writes_nothing(monkeypatch, xtm_one):
    """Its marker keeps the first hold's start."""
    from app.services import assignment_engine

    run, assignment, agent = _run_stubs()
    run.trigger_context = ai_hold.with_hold_marker({"type": "schedule"}, QUOTA)
    since = run.trigger_context[ai_hold.HOLD_MARKER_KEY]["since"]
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    _check(monkeypatch, down=True)

    assert await assignment_engine.execute_assignment_run(str(run.id), hold=True) == (
        assignment_engine.RunHeld(QUOTA)
    )
    assert session.commits == 0
    assert run.trigger_context[ai_hold.HOLD_MARKER_KEY]["since"] == since


@pytest.mark.asyncio
async def test_a_run_held_for_the_store_past_the_bound_fails_with_the_reason(
    monkeypatch, xtm_one
):
    run, assignment, agent = _run_stubs()
    run.trigger_context = ai_hold.with_hold_marker({"type": "webhook"}, QUOTA)
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    _check(monkeypatch, down=True)

    assert await engine.execute_assignment_run(str(run.id), hold=False) is None

    assert run.status == "failed"
    assert run.error.startswith(QUOTA_UNAVAILABLE_MESSAGE)
    assert "held for 168 hour(s) waiting for the quota store" in run.error
    assert run.failure_cause == FailureCause.QUOTA_BACKEND_UNAVAILABLE.value
    spies.increment.assert_not_awaited()
    spies.pool.assert_not_awaited()
    spies.free.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_an_engine_to_hold_it_a_run_fails_as_before(monkeypatch, xtm_one):
    run, assignment, agent = _run_stubs()
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    _check(monkeypatch, down=True)

    assert await engine.execute_assignment_run(str(run.id)) is None

    assert run.status == "failed"
    assert run.error == QUOTA_UNAVAILABLE_MESSAGE
    assert run.failure_cause == FailureCause.QUOTA_BACKEND_UNAVAILABLE.value
    assert ai_hold.HOLD_MARKER_KEY not in run.trigger_context
    spies.increment.assert_not_awaited()
    spies.pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_quota_fault_that_is_not_an_outage_still_fails_closed(monkeypatch, xtm_one):
    """Only an unreachable store is waited out: a fault of the quota code or
    its data would hold every run for the whole bound."""
    run, assignment, agent = _run_stubs()
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota",
        AsyncMock(side_effect=TypeError("unsupported operand")),
    )

    assert await engine.execute_assignment_run(str(run.id), hold=True) is None

    assert run.status == "failed"
    assert run.error == QUOTA_UNAVAILABLE_MESSAGE
    assert ai_hold.HOLD_MARKER_KEY not in run.trigger_context
    spies.increment.assert_not_awaited()
    spies.pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_outage_met_while_charging_is_not_held(monkeypatch, xtm_one):
    """The check passed, so part of the charge may have landed: holding the run
    would charge it again."""
    run, assignment, agent = _run_stubs()
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    _check(monkeypatch, down=False)
    spies.increment.side_effect = _store_down()

    assert await engine.execute_assignment_run(str(run.id), hold=True) is None

    assert run.status == "failed"
    assert run.error == QUOTA_UNAVAILABLE_MESSAGE
    assert ai_hold.HOLD_MARKER_KEY not in run.trigger_context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refused",
    [
        ReadOnlyError("You can't write against a read only replica."),
        OutOfMemoryError("command not allowed when used memory > 'maxmemory'."),
    ],
    ids=["read-only-replica", "out-of-memory"],
)
async def test_a_store_refusing_writes_is_held_before_anything_is_charged(
    monkeypatch, xtm_one, refused
):
    """Such a store still answers the check's reads: the run asks it for a
    write first, so it is held uncharged instead of failing at the charge."""
    run, assignment, agent = _run_stubs()
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    monkeypatch.setattr(
        "app.services.quota.quota_store_outage", AsyncMock(return_value=refused)
    )
    check = _check(monkeypatch, down=False)

    assert await engine.execute_assignment_run(str(run.id), hold=True) == (
        engine.RunHeld(QUOTA)
    )

    assert run.status == "pending" and run.error is None
    assert ai_hold.held_code_of(run.trigger_context) == QUOTA
    check.assert_not_awaited()
    spies.increment.assert_not_awaited()


@pytest.mark.asyncio
async def test_copilot_mode_left_open_still_runs_unmetered(monkeypatch):
    run, assignment, agent = _run_stubs()
    _copilot_open(monkeypatch)
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    _check(monkeypatch, down=True)
    monkeypatch.setattr(
        "app.services.quota.quota_store_outage",
        AsyncMock(side_effect=AssertionError("probed the quota store")),
    )

    with pytest.raises(_Started):
        await engine.execute_assignment_run(str(run.id), hold=True)

    spies.increment.assert_not_awaited()
    assert "quota_metered" not in run.trigger_context


# -- The adapter: the hold stays open until the store answers -----------------


def _held_execution(*, seconds: float, count: int = 1, run_id: str | None = None):
    since = datetime.now(UTC) - timedelta(seconds=seconds)
    return _execution(
        payload={
            "run_id": run_id or str(uuid.uuid4()),
            orchestration_engine.HOLD_PAYLOAD_KEY: {
                "since": since.isoformat(),
                "count": count,
                "reason": QUOTA_HELD,
                "attempts": 0,
            },
        }
    )


@pytest.mark.asyncio
async def test_the_adapter_keeps_the_hold_open_while_the_store_is_out(monkeypatch, xtm_one):
    """The bound, the backoff and the runnable budget keep counting: the hold
    is not ended and reopened at every re-check."""
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=True))
    body = AsyncMock(return_value=assignment_engine.RunHeld(QUOTA))
    monkeypatch.setattr(assignment_engine, "execute_assignment_run", body)
    monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))
    monkeypatch.setattr(assignment_adapter, "_run_trigger_payload", AsyncMock(return_value=None))
    execution = _held_execution(seconds=600, count=2)
    since = orchestration_engine.hold_started_at(execution)
    db = _Session()

    result = await assignment_adapter.AssignmentAdapter().execute(
        AdapterContext(db=db, worker_id="w1"), execution
    )

    assert orchestration_engine.hold_started_at(execution) == since
    assert db.commits == 0
    assert body.await_args.kwargs["hold"] is True
    # One reading: the run holds on the store the re-check found out.
    assert body.await_args.kwargs["verdict"] == AgenticVerdict(QUOTA)
    assert result.kind == "defer"
    assert result.adapter_state == QUOTA_HELD
    assert result.backoff_seconds == ai_hold.hold_delay_seconds(2)


@pytest.mark.asyncio
async def test_the_adapter_ends_the_hold_before_the_run_once_the_store_answers(
    monkeypatch, xtm_one
):
    """Ended before the work starts, as a hold for agentic AI is: a running run
    never carries a hold the deadline sweep would read as waiting."""
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    execution = _held_execution(seconds=600)
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )
    seen: dict = {}

    async def _body(run_id, *, hold, verdict, deadline_at, attempt, cancel_event=None, **_taken_back):
        seen["hold_open"] = orchestration_engine.hold_record(execution) is not None
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _body)
    monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))
    monkeypatch.setattr(assignment_adapter, "_run_trigger_payload", AsyncMock(return_value=None))
    run = SimpleNamespace(status="completed", error=None)

    result = await assignment_adapter.AssignmentAdapter().execute(
        AdapterContext(db=_Session([run]), worker_id="w1"), execution
    )

    assert seen == {"hold_open": False}
    assert orchestration_engine.hold_record(execution) is None
    assert result.kind == "completed"


@pytest.mark.asyncio
async def test_the_store_is_probed_only_for_a_hold_on_it(monkeypatch, xtm_one):
    """An execution never held, or held for agentic AI, reads nothing more."""
    probe = AsyncMock(side_effect=AssertionError("probed the quota store"))
    monkeypatch.setattr("app.services.quota.quota_store_out", probe)
    ctx = SimpleNamespace(db=_Session(), worker_id="w1")

    assert await ai_hold.close_hold_if_ai_is_back(ctx, _execution()) is None
    ai_held = _execution(
        payload={
            "run_id": str(uuid.uuid4()),
            orchestration_engine.HOLD_PAYLOAD_KEY: {
                "since": datetime.now(UTC).isoformat(),
                "count": 1,
                "reason": ai_hold.held_adapter_state("ai_disabled"),
            },
        }
    )
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=ai_held)
    )
    await ai_hold.close_hold_if_ai_is_back(ctx, ai_held)
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_quota_hold_ends_once_quotas_fail_open(monkeypatch):
    """Failing open (Copilot / dev mode), an outage runs unmetered: the hold
    ends before the work starts whatever the store answers, so a running
    execution never carries a hold the deadline sweep would rebase."""
    _copilot_open(monkeypatch)
    monkeypatch.setattr(
        "app.services.quota.quota_store_out",
        AsyncMock(side_effect=AssertionError("probed the quota store")),
    )
    execution = _held_execution(seconds=600)
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )
    ctx = SimpleNamespace(db=_Session(), worker_id="w1")

    verdict = await ai_hold.close_hold_if_ai_is_back(ctx, execution)

    assert verdict.refusal is None
    assert orchestration_engine.hold_record(execution) is None


@pytest.mark.asyncio
async def test_a_run_re_checked_while_the_store_is_out_holds_on_that_reading(
    monkeypatch, xtm_one
):
    """The re-check found the store out: the run holds on that verdict and
    never meets the store a second time, so it cannot start while its hold is
    open (a store that answered in between would otherwise run it held)."""
    from app.services import assignment_engine

    run, assignment, agent = _run_stubs()
    run.trigger_context = ai_hold.with_hold_marker({"type": "schedule"}, QUOTA)
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota",
        AsyncMock(side_effect=AssertionError("met the quota store again")),
    )

    outcome = await assignment_engine.execute_assignment_run(
        str(run.id), hold=True, verdict=AgenticVerdict(QUOTA)
    )

    assert outcome == assignment_engine.RunHeld(QUOTA)
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_run_re_checked_past_the_bound_fails_with_the_store_s_cause(
    monkeypatch, xtm_one
):
    run, assignment, agent = _run_stubs()
    run.trigger_context = ai_hold.with_hold_marker({"type": "webhook"}, QUOTA)
    engine, spies = _engine(monkeypatch, run, assignment, agent)

    assert (
        await engine.execute_assignment_run(
            str(run.id), hold=False, verdict=AgenticVerdict(QUOTA)
        )
        is None
    )

    assert run.status == "failed"
    assert run.error.startswith(QUOTA_UNAVAILABLE_MESSAGE) and "held for" in run.error
    assert run.failure_cause == FailureCause.QUOTA_BACKEND_UNAVAILABLE.value
    spies.increment.assert_not_awaited()
    spies.pool.assert_not_awaited()


# -- Delegated background tasks -----------------------------------------------


@pytest.mark.asyncio
async def test_a_delegated_task_is_held_pending_while_the_store_is_out(monkeypatch, xtm_one):
    """Held before it is marked running: its agent turn, which charges the
    quota and counts the execution, is never reached, and its parent is not
    woken - nothing ended."""
    from app.services import sub_agent

    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=True))
    task = _task_row()
    session = _TaskSession([task])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    collaborators = _patch_task_collaborators(monkeypatch)
    mark_run = AsyncMock(return_value=True)
    monkeypatch.setattr(sub_agent, "_mark_run_held", mark_run)
    run_id = str(uuid.uuid4())

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=run_id,
        hold=True,
    )

    assert outcome.held and outcome.refusal == QUOTA
    assert task.status == "pending"
    assert task.started_at is None and task.error is None
    assert ai_hold.held_code_of(task.metadata_json) == QUOTA
    assert mark_run.await_args.args[1:] == (run_id, QUOTA)
    collaborators.run_agent_chat.assert_not_awaited()
    collaborators.update_run.assert_not_awaited()
    collaborators.delegation.assert_not_awaited()
    collaborators.wake.assert_not_called()


@pytest.mark.asyncio
async def test_a_delegated_task_re_checked_while_the_store_is_out_holds_on_that_reading(
    monkeypatch, xtm_one
):
    from app.services import sub_agent

    monkeypatch.setattr(
        "app.services.quota.quota_store_out",
        AsyncMock(side_effect=AssertionError("met the quota store again")),
    )
    task = _task_row()
    task.metadata_json = ai_hold.with_hold_marker({}, QUOTA)
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task]))
    collaborators = _patch_task_collaborators(monkeypatch)

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        hold=True,
        verdict=AgenticVerdict(QUOTA),
    )

    assert outcome.held and outcome.refusal == QUOTA
    assert task.status == "pending"
    collaborators.run_agent_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_held_delegated_task_goes_ahead_once_the_store_answers(monkeypatch, xtm_one):
    from app.services import sub_agent

    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    task = _task_row()
    task.metadata_json = ai_hold.with_hold_marker({"assignment_run_id": "x"}, QUOTA)
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _TaskSession([task, None])
    )
    _patch_task_collaborators(monkeypatch)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))

    await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        hold=True,
    )

    assert task.started_at is not None
    assert task.metadata_json == {"assignment_run_id": "x"}


@pytest.mark.asyncio
async def test_a_delegated_task_held_past_the_bound_fails_and_wakes_its_parent(
    monkeypatch, xtm_one
):
    from app.services import sub_agent

    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=True))
    task = _task_row()
    task.metadata_json = ai_hold.with_hold_marker({}, QUOTA)
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _TaskSession([task, "Exposure analyst"]),
    )
    collaborators = _patch_task_collaborators(monkeypatch)
    run_id = str(uuid.uuid4())

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=run_id,
        hold=False,
    )

    assert not outcome.succeeded
    assert task.status == "failed"
    assert task.error.startswith(QUOTA_UNAVAILABLE_MESSAGE) and "held for" in task.error
    assert collaborators.update_run.await_args.kwargs["failure_cause"] == (
        FailureCause.QUOTA_BACKEND_UNAVAILABLE
    )
    collaborators.run_agent_chat.assert_not_awaited()
    collaborators.wake.assert_called_once()


@pytest.mark.asyncio
async def test_a_delegated_task_is_not_probed_when_quotas_fail_open(monkeypatch):
    from app.services import sub_agent

    _copilot_open(monkeypatch)
    monkeypatch.setattr(
        "app.services.quota.quota_store_out",
        AsyncMock(side_effect=AssertionError("probed the quota store")),
    )
    task = _task_row()
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _TaskSession([task, None])
    )
    _patch_task_collaborators(monkeypatch)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))

    await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        hold=True,
    )

    assert task.started_at is not None


# -- The store answers the start's probe, not the turn's check ----------------


async def _refused_turn(*_args, error_out=None, **_kwargs):
    """An agent turn whose quota check met the outage: refused, uncharged."""
    from app.services.agent_chat import QUOTA_STORE_OUTAGE_KEY

    error_out.update(
        error=QUOTA_UNAVAILABLE_MESSAGE, error_code=QUOTA, **{QUOTA_STORE_OUTAGE_KEY: True}
    )
    return QUOTA_UNAVAILABLE_MESSAGE, None


def _flapping_store_task(monkeypatch, run_row):
    """A task whose start found the store answering and whose turn did not:
    the task, its linked run, the target agent, then the task again for the
    write after the turn."""
    from app.services import sub_agent

    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    task = _task_row()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Exposure analyst", handover_agent_ids=[])
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _TaskSession([task, run_row, agent, task]),
    )
    collaborators = _patch_task_collaborators(monkeypatch)
    collaborators.run_agent_chat.side_effect = _refused_turn
    collaborators.refund = AsyncMock()
    monkeypatch.setattr(
        "app.services.agentic_executions.refund_agentic_execution", collaborators.refund
    )
    collaborators.mark_run = AsyncMock(return_value=True)
    monkeypatch.setattr(sub_agent, "_mark_run_held", collaborators.mark_run)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    return task, collaborators


@pytest.mark.asyncio
async def test_a_task_whose_turn_meets_the_outage_is_held_again_uncounted(monkeypatch, xtm_one):
    """The store answered the start's probe and then refused the turn's quota
    check (a flapping store, a failover): nothing was charged, so the task goes
    back to pending and is held, as if the start had found the outage - never
    recorded as completed with the refusal as its result. The execution the
    refused turn counted is given back: the task is counted when it runs."""
    from app.services import sub_agent
    from app.services.agentic_executions import SOURCE_AGENT_CHAT

    run_row = SimpleNamespace(status="pending", started_at=None, trigger_context={})
    task, collaborators = _flapping_store_task(monkeypatch, run_row)
    run_id = str(uuid.uuid4())

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=run_id,
        timeout_seconds=600,
        hold=True,
    )

    assert outcome.held and outcome.refusal == QUOTA
    assert task.status == "pending"
    assert task.started_at is None and task.error is None
    assert not hasattr(task, "result"), "recorded as completed"
    assert ai_hold.held_code_of(task.metadata_json) == QUOTA
    collaborators.run_agent_chat.assert_awaited_once()
    collaborators.refund.assert_awaited_once_with(SOURCE_AGENT_CHAT)
    assert collaborators.mark_run.await_args.args[1:] == (run_id, QUOTA)
    assert collaborators.mark_run.await_args.kwargs == {"back_to_pending": True}
    assert [call.args[2] for call in collaborators.delegation.await_args_list] == [
        "running",
        "pending",
    ]
    collaborators.update_run.assert_not_awaited()
    collaborators.wake.assert_not_called()


@pytest.mark.asyncio
async def test_a_task_whose_turn_meets_the_outage_past_the_bound_fails_with_its_cause(
    monkeypatch, xtm_one
):
    from app.services import sub_agent

    run_row = SimpleNamespace(status="pending", started_at=None, trigger_context={})
    task, collaborators = _flapping_store_task(monkeypatch, run_row)

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=str(uuid.uuid4()),
        timeout_seconds=600,
        hold=False,
    )

    assert not outcome.succeeded
    assert task.status == "failed" and task.error == QUOTA_UNAVAILABLE_MESSAGE
    assert collaborators.update_run.await_args.args[2] == "failed"
    assert collaborators.update_run.await_args.kwargs["failure_cause"] == (
        FailureCause.QUOTA_BACKEND_UNAVAILABLE
    )
    collaborators.refund.assert_awaited_once()
    collaborators.mark_run.assert_not_awaited()
    collaborators.wake.assert_called_once()


@pytest.mark.asyncio
async def test_a_turn_refused_for_another_reason_still_completes_the_task(monkeypatch, xtm_one):
    """Only an outage the check met, before any charge, sends a started task
    back: a quota exhausted is the answer the task delivers, as before."""
    from app.services import sub_agent

    run_row = SimpleNamespace(status="pending", started_at=None, trigger_context={})
    task, collaborators = _flapping_store_task(monkeypatch, run_row)

    async def _exhausted(*_args, error_out=None, **_kwargs):
        error_out.update(error="quota reached", error_code="quota_exceeded")
        return "quota reached", None

    collaborators.run_agent_chat.side_effect = _exhausted

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=str(uuid.uuid4()),
        timeout_seconds=600,
        hold=True,
    )

    assert outcome.succeeded
    assert task.status == "completed" and task.result == "quota reached"
    collaborators.refund.assert_not_awaited()
    collaborators.mark_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_chat_gate_names_an_outage_its_check_met(monkeypatch, xtm_one):
    from app.services.agent_chat import QUOTA_STORE_OUTAGE_KEY, enforce_agentic_quota_ex

    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", AsyncMock(side_effect=_store_down())
    )
    increment = AsyncMock()
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", increment)
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(object(), str(uuid.uuid4()), quota_out=out)

    assert (refusal, metered) == (QUOTA_UNAVAILABLE_MESSAGE, False)
    assert out == {"actions_consumed": 0, "reason": QUOTA, QUOTA_STORE_OUTAGE_KEY: True}
    increment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["check_fault", "charge_outage"])
async def test_the_chat_gate_names_no_outage_it_did_not_meet_before_charging(
    monkeypatch, xtm_one, failing
):
    """A fault of the quota code, or an outage met while charging (part of the
    charge may have landed), refuses as before and is never named an outage a
    held task could be retried after."""
    from app.services.agent_chat import QUOTA_STORE_OUTAGE_KEY, enforce_agentic_quota_ex

    check = AsyncMock(side_effect=KeyError("tier") if failing == "check_fault" else None)
    increment = AsyncMock(side_effect=_store_down() if failing == "charge_outage" else None)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", check)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", increment)
    out: dict = {}

    refusal, _ = await enforce_agentic_quota_ex(object(), str(uuid.uuid4()), quota_out=out)

    assert refusal == QUOTA_UNAVAILABLE_MESSAGE
    assert out["reason"] == QUOTA and QUOTA_STORE_OUTAGE_KEY not in out


@pytest.mark.asyncio
async def test_an_agent_turn_refused_by_the_outage_says_so_in_its_errors(monkeypatch, xtm_one):
    from app.services import agent_chat
    from tests.test_agent_chat_smoke import USER_ID, _agent

    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", AsyncMock(side_effect=_store_down())
    )
    error_out: dict = {}

    text, _ = await agent_chat.run_agent_chat(
        db=object(),  # type: ignore[arg-type]
        user_id=USER_ID,
        agent=_agent(),  # type: ignore[arg-type]
        history=[],
        user_message="Summarise the new exposures",
        error_out=error_out,
    )

    assert text == QUOTA_UNAVAILABLE_MESSAGE
    assert error_out == {
        "error": QUOTA_UNAVAILABLE_MESSAGE,
        "error_code": QUOTA,
        agent_chat.QUOTA_STORE_OUTAGE_KEY: True,
    }


@pytest.mark.asyncio
async def test_a_run_marked_running_with_its_task_waits_again_with_it():
    """``back_to_pending``: the linked run was marked running when its task
    started and waits again, held, when the task goes back."""
    from app.services import sub_agent

    run = SimpleNamespace(
        status="running", started_at=datetime.now(UTC), trigger_context={"k": "v"}
    )

    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    session = _TaskSession([run])
    session.begin_nested = lambda: _Nested()

    wrote = await sub_agent._mark_run_held(
        session, str(uuid.uuid4()), QUOTA, back_to_pending=True
    )

    assert wrote
    assert run.status == "pending" and run.started_at is None
    assert ai_hold.held_code_of(run.trigger_context) == QUOTA
    assert run.trigger_context["k"] == "v"


# -- A store refusing the platform's credentials is not an outage -------------

# A configuration error, like a credential an LLM provider rejects: held, a
# wrong password would hold every due run for the whole bound. Each path meets
# the refusal it met before holds existed, at once.
REJECTED_CREDENTIALS = pytest.mark.parametrize(
    "rejected",
    [
        AuthenticationError("WRONGPASS invalid username-password pair or user is disabled."),
        AuthenticationError("NOAUTH Authentication required."),
        AuthorizationError("you are not authorized to view this ocsp certificate"),
        ExternalAuthProviderError("problem with LDAP service"),
    ],
    ids=["wrongpass", "noauth", "not-authorized", "auth-provider"],
)

FAILING_OVER = pytest.mark.parametrize(
    "failing_over",
    [
        MasterDownError(
            "MASTERDOWN Link with MASTER is down and replica-serve-stale-data is set to 'no'."
        ),
        ClusterDownError("CLUSTERDOWN The cluster is down"),
        TryAgainError("TRYAGAIN Multiple keys request during rehashing of slot"),
    ],
    ids=["masterdown", "clusterdown", "tryagain"],
)


def _rejecting_store(monkeypatch, rejected):
    """The quota store refuses every command with *rejected*: the probe's
    write and the check's reads alike."""
    redis = SimpleNamespace(set=AsyncMock(side_effect=rejected))
    monkeypatch.setattr("app.services.quota.redis_client", AsyncMock(return_value=redis))
    # The unit suite's autouse fixture answers the probe at this source.
    monkeypatch.setattr("app.services.quota.quota_store_outage", real_quota_store_outage)
    check = AsyncMock(side_effect=rejected)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", check)
    return redis, check


@REJECTED_CREDENTIALS
def test_a_rejected_credential_is_a_configuration_error_not_an_outage(rejected):
    from app.services.quota import is_quota_store_outage

    assert is_quota_store_outage(rejected) is False


@pytest.mark.asyncio
@REJECTED_CREDENTIALS
async def test_a_due_run_meeting_a_rejected_credential_fails_at_once_as_before(
    monkeypatch, xtm_one, rejected
):
    run, assignment, agent = _run_stubs()
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    redis, check = _rejecting_store(monkeypatch, rejected)

    assert await engine.execute_assignment_run(str(run.id), hold=True) is None

    assert run.status == "failed"
    assert run.error == QUOTA_UNAVAILABLE_MESSAGE
    assert run.failure_cause == FailureCause.QUOTA_BACKEND_UNAVAILABLE.value
    assert ai_hold.HOLD_MARKER_KEY not in run.trigger_context
    redis.set.assert_awaited_once()
    check.assert_awaited_once()
    spies.increment.assert_not_awaited()
    spies.pool.assert_not_awaited()


@pytest.mark.asyncio
@REJECTED_CREDENTIALS
async def test_a_quota_hold_re_checked_against_a_rejected_credential_ends(
    monkeypatch, xtm_one, rejected
):
    """The store answers, refusing the credential: nothing to wait out, so the
    hold ends before the work starts and the work meets that refusal at once."""
    _rejecting_store(monkeypatch, rejected)
    execution = _held_execution(seconds=600)
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )
    ctx = SimpleNamespace(db=_Session(), worker_id="w1")

    verdict = await ai_hold.close_hold_if_ai_is_back(ctx, execution)

    assert verdict.refusal is None
    assert orchestration_engine.hold_record(execution) is None


@pytest.mark.asyncio
@REJECTED_CREDENTIALS
async def test_a_delegated_task_meeting_a_rejected_credential_ends_as_before(
    monkeypatch, xtm_one, rejected
):
    """Not held at its start, nor sent back after its turn: the real turn is
    refused by the real gate at once, and that refusal is the task's answer."""
    from app.services import sub_agent
    from tests.test_agent_chat_smoke import _agent

    _rejecting_store(monkeypatch, rejected)
    task = _task_row()
    run_row = SimpleNamespace(status="pending", started_at=None, trigger_context={})
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _TaskSession([task, run_row, _agent(handover_agent_ids=[]), task]),
    )
    _patch_task_collaborators(monkeypatch)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", real_run_agent_chat)
    refund = AsyncMock()
    monkeypatch.setattr("app.services.agentic_executions.refund_agentic_execution", refund)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=str(uuid.uuid4()),
        timeout_seconds=600,
        hold=True,
    )

    assert outcome.succeeded
    assert task.status == "completed" and task.result == QUOTA_UNAVAILABLE_MESSAGE
    assert ai_hold.HOLD_MARKER_KEY not in task.metadata_json
    refund.assert_not_awaited()


@pytest.mark.asyncio
@REJECTED_CREDENTIALS
async def test_the_chat_gate_refuses_a_rejected_credential_as_before(
    monkeypatch, xtm_one, rejected
):
    from app.services.agent_chat import enforce_agentic_quota_ex

    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", AsyncMock(side_effect=rejected)
    )
    increment = AsyncMock()
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", increment)
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(object(), str(uuid.uuid4()), quota_out=out)

    assert (refusal, metered) == (QUOTA_UNAVAILABLE_MESSAGE, False)
    assert out == {"actions_consumed": 0, "reason": QUOTA}
    increment.assert_not_awaited()


@pytest.mark.asyncio
@FAILING_OVER
async def test_a_store_failing_over_holds_the_due_run(monkeypatch, xtm_one, failing_over):
    """A master or cluster the store is failing over to is unavailability, not
    a configuration error: the run waits it out uncharged."""
    run, assignment, agent = _run_stubs()
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", AsyncMock(side_effect=failing_over)
    )

    assert await engine.execute_assignment_run(str(run.id), hold=True) == (
        engine.RunHeld(QUOTA)
    )

    assert run.status == "pending" and run.error is None
    assert ai_hold.held_code_of(run.trigger_context) == QUOTA
    spies.increment.assert_not_awaited()


# -- The words ----------------------------------------------------------------


def test_the_hold_is_worded_as_waiting_for_the_store():
    assert ai_hold.QUOTA_STORE_UNAVAILABLE_CODE == QUOTA
    message = ai_hold.held_message(QUOTA)
    assert message.startswith("Held:") and "quota store" in message
    assert "error" not in message.lower()
    assert ai_hold.failure_cause_of(QUOTA) == FailureCause.QUOTA_BACKEND_UNAVAILABLE
    assert ai_hold.failure_cause_of("ai_disabled") == FailureCause.AI_DISABLED


@pytest.mark.parametrize(
    ("exc", "outage"),
    [
        (RedisConnectionError("Connection refused"), True),
        (ConnectionRefusedError(111, "Connection refused"), True),
        (ConnectionResetError(104, "Connection reset by peer"), True),
        (TimeoutError("timed out"), True),
        (BusyLoadingError("LOADING Redis is loading the dataset in memory"), True),
        (ReadOnlyError("You can't write against a read only replica."), True),
        (OutOfMemoryError("command not allowed when used memory > 'maxmemory'."), True),
        # An ACL denial is a configuration error, like a wrong password.
        (NoPermissionError("NOPERM this user has no permissions to run the 'set' command"), False),
        (AuthenticationWrongNumberOfArgsError("wrong number of arguments for 'auth' command"), False),
        (TypeError("unsupported operand"), False),
        (KeyError("agentic"), False),
        # Local I/O faults of the rest of the check (licence, config) are
        # OSErrors too, and never the store.
        (FileNotFoundError(2, "No such file or directory"), False),
        (PermissionError(13, "Permission denied"), False),
    ],
)
def test_only_an_unreachable_store_is_an_outage(exc, outage):
    from app.services.quota import is_quota_store_outage

    assert is_quota_store_outage(exc) is outage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write", "out"),
    [
        (None, False),
        (_store_down(), True),
        (NoPermissionError("NOPERM this user has no permissions"), False),
    ],
    ids=["answers", "unreachable", "not-an-outage"],
)
async def test_the_probe_writes_to_the_store_and_never_raises(monkeypatch, write, out):
    """A write, not a ping: a replica refusing writes or a store out of memory
    answers a ping. Only an outage says the store is out."""
    redis = SimpleNamespace(set=AsyncMock(side_effect=write))
    monkeypatch.setattr("app.services.quota.redis_client", AsyncMock(return_value=redis))
    # The unit suite's autouse fixture answers the probe at this source.
    monkeypatch.setattr("app.services.quota.quota_store_outage", real_quota_store_outage)

    assert await real_quota_store_outage() is (write if out else None)
    assert await real_quota_store_out() is out
    assert redis.set.await_count == 2

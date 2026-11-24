"""Tests for sub-agent failure propagation and delegation lifecycle.

Covers the two halves of the fix:

* ORCH-1 — ``SubAgentAdapter.execute()`` must map a failed
  ``SubAgentTaskOutcome`` from ``_execute_sub_agent_task`` onto
  ``AdapterResult.failed(..., retryable=False)`` instead of always
  completing, and must not re-run a task whose ``SubAgentTask.status`` is
  already terminal.
* ORCH-2 — ``update_delegation`` transitions the ``background_task``
  delegation audit row out of ``pending`` (running / completed / failed)
  when the task starts and when it reaches a terminal state — including
  the task-row-missing path, which maps to a NON-retryable adapter failure
  and therefore never gets an ``on_dead_letter`` cleanup pass.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.delegation import update_delegation
from app.services.orchestration_adapters.base import AdapterContext
from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
from app.services.sub_agent import SubAgentTaskOutcome, _execute_sub_agent_task


class _FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDb:
    """Fake AsyncSession.

    ``scalar`` answers every ``execute()`` with the same row; ``scalars``
    (a list) answers consecutive ``execute()`` calls in order, then ``None``
    once exhausted — so an unexpected extra query surfaces as a missing row
    in the assertions instead of silently reusing stale data.
    """

    def __init__(self, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = list(scalars) if scalars is not None else None
        self.commits = 0
        self.flushes = 0

    async def execute(self, *_a, **_k):
        if self._scalars is not None:
            value = self._scalars.pop(0) if self._scalars else None
            return _FakeResult(value)
        return _FakeResult(self._scalar)

    async def commit(self):
        self.commits += 1

    async def flush(self, *args, **kwargs):
        # Accept the real AsyncSession.flush(objects=...) signature so scoped
        # flushes like ``db.flush([delegation])`` don't TypeError here.
        self.flushes += 1


class _FakeSessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _execution_payload() -> SimpleNamespace:
    return SimpleNamespace(
        payload={
            "task_id": str(uuid.uuid4()),
            "target_agent_id": str(uuid.uuid4()),
            "task_description": "do the thing",
            "user_id": str(uuid.uuid4()),
        }
    )


# ── SubAgentAdapter outcome mapping (ORCH-1) ─────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_maps_failed_outcome_to_failed_result(monkeypatch):
    """A failed task execution must surface as a non-retryable failed
    execution (the task body already recorded the terminal status)."""
    from app.services import sub_agent as sub_agent_mod

    async def _fake_task(**_kwargs):
        return SubAgentTaskOutcome(status="failed", error="LLM exploded")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _fake_task)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    result = await SubAgentAdapter().execute(ctx, _execution_payload())

    assert result.kind == "failed"
    assert result.error == "LLM exploded"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_adapter_maps_failed_outcome_without_error_message(monkeypatch):
    from app.services import sub_agent as sub_agent_mod

    async def _fake_task(**_kwargs):
        return SubAgentTaskOutcome(status="failed")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _fake_task)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    result = await SubAgentAdapter().execute(ctx, _execution_payload())

    assert result.kind == "failed"
    assert result.error == "Sub-agent task failed"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_adapter_maps_completed_outcome_to_completed_result(monkeypatch):
    from app.services import sub_agent as sub_agent_mod

    async def _fake_task(**_kwargs):
        return SubAgentTaskOutcome(status="completed")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _fake_task)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    result = await SubAgentAdapter().execute(ctx, _execution_payload())

    assert result.kind == "completed"


@pytest.mark.asyncio
async def test_adapter_terminal_completed_task_short_circuits(monkeypatch):
    """A re-claimed execution for an already-completed task must NOT re-run
    the agentic loop — it just mirrors the terminal state."""
    from app.services import sub_agent as sub_agent_mod

    async def _must_not_run(**_kwargs):
        raise AssertionError("must not re-run a terminal task")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _must_not_run)

    task_row = SimpleNamespace(status="completed", error=None)
    ctx = AdapterContext(db=_FakeDb(scalar=task_row), worker_id="w1")
    result = await SubAgentAdapter().execute(ctx, _execution_payload())

    assert result.kind == "completed"


@pytest.mark.asyncio
async def test_adapter_terminal_failed_task_short_circuits(monkeypatch):
    from app.services import sub_agent as sub_agent_mod

    async def _must_not_run(**_kwargs):
        raise AssertionError("must not re-run a terminal task")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _must_not_run)

    task_row = SimpleNamespace(status="failed", error="already failed")
    ctx = AdapterContext(db=_FakeDb(scalar=task_row), worker_id="w1")
    result = await SubAgentAdapter().execute(ctx, _execution_payload())

    assert result.kind == "failed"
    assert result.error == "already failed"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_adapter_invalid_task_id_fails_non_retryable(monkeypatch):
    """A corrupted payload whose task_id is not a UUID can never succeed —
    it must surface as a clean non-retryable failure (graceful miss, like
    ``get_task_status``) instead of a ValueError the reconciler would retry."""
    from app.services import sub_agent as sub_agent_mod

    async def _must_not_run(**_kwargs):
        raise AssertionError("must not run the task body for a bad payload")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _must_not_run)

    execution = _execution_payload()
    execution.payload["task_id"] = "not-a-uuid"
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    result = await SubAgentAdapter().execute(ctx, execution)

    assert result.kind == "failed"
    assert result.retryable is False
    assert "not a valid UUID" in result.error


@pytest.mark.asyncio
async def test_dead_letter_with_invalid_ids_does_not_raise():
    """``on_dead_letter`` is the last-chance cleanup pass — a corrupted
    payload (non-UUID task_id / assignment_run_id) must not crash it, and the
    string-keyed delegation close must still be attempted."""
    execution = SimpleNamespace(
        payload={
            "task_id": "not-a-uuid",
            "assignment_run_id": "also-not-a-uuid",
        },
        last_error="boom",
    )
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")

    await SubAgentAdapter().on_dead_letter(ctx, execution)


# ── Dead-letter wakes the parked parent conversation ─────────────────


def _dead_letter_execution(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        payload={"task_id": task_id},
        last_error="worker lost",
    )


def _make_task_row(*, conversation_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        error=None,
        completed_at=None,
        conversation_id=conversation_id,
        user_id=uuid.uuid4(),
        target_agent_id=uuid.uuid4(),
        metadata_json={},
    )


def _patch_dead_letter_collaborators(monkeypatch, wakes: list):
    """Silence the delegation close + quota refund side paths and record
    parent-wake scheduling instead of running the real fire-and-forget task."""
    from app.services import quota as quota_mod
    from app.services import sub_agent as sub_agent_mod

    async def _no_delegation(*_a, **_k):
        return None

    async def _no_refund(*_a, **_k):
        return None

    def _record_wake(task, *, agent_name, terminal_status):
        wakes.append((task, agent_name, terminal_status))

    async def _no_release(*_a, **_k):
        return 0

    monkeypatch.setattr(sub_agent_mod, "release_failed_task_claims", _no_release)
    monkeypatch.setattr(sub_agent_mod, "_update_task_delegation", _no_delegation)
    monkeypatch.setattr(quota_mod, "refund_agentic_usage_by_receipt", _no_refund)
    monkeypatch.setattr(
        sub_agent_mod, "_schedule_parent_wake_if_needed", _record_wake
    )


@pytest.mark.asyncio
async def test_dead_letter_wakes_parked_parent_for_chat_task(monkeypatch):
    """An abandoned (dead-lettered) background task spawned from a chat
    conversation must schedule a parent wake after committing the ``failed``
    status — otherwise the parent stays parked on 'Background work is still
    running' forever (the task body never runs again on this path)."""
    wakes: list = []
    _patch_dead_letter_collaborators(monkeypatch, wakes)

    task_row = _make_task_row(conversation_id=uuid.uuid4())
    # Consecutive executes: task lookup → Agent-name lookup for the wake.
    db = _FakeDb(scalars=[task_row, "Specialist"])
    ctx = AdapterContext(db=db, worker_id="w1")

    await SubAgentAdapter().on_dead_letter(
        ctx, _dead_letter_execution(str(task_row.id))
    )

    assert task_row.status == "failed"
    assert task_row.error == "worker lost"
    # The failed status must be durable BEFORE the wake fires: the wake task
    # re-reads the row in its own session.
    assert db.commits == 1
    assert wakes == [(task_row, "Specialist", "failed")]


@pytest.mark.asyncio
async def test_dead_letter_does_not_wake_without_conversation(monkeypatch):
    """A dead-lettered task with no parent conversation (assignment-spawned)
    has nothing to resume — no wake. Its failure is committed once, so its
    work item claims are released in the same pass."""
    wakes: list = []
    _patch_dead_letter_collaborators(monkeypatch, wakes)

    task_row = _make_task_row(conversation_id=None)
    db = _FakeDb(scalars=[task_row])
    ctx = AdapterContext(db=db, worker_id="w1")

    await SubAgentAdapter().on_dead_letter(
        ctx, _dead_letter_execution(str(task_row.id))
    )

    assert task_row.status == "failed"
    assert wakes == []
    assert db.commits == 1


@pytest.mark.asyncio
async def test_dead_letter_does_not_wake_already_terminal_task(monkeypatch):
    """A task the body already drove terminal woke its parent there — the
    cleanup pass must not transition it again or schedule a second wake."""
    wakes: list = []
    _patch_dead_letter_collaborators(monkeypatch, wakes)

    task_row = _make_task_row(conversation_id=uuid.uuid4())
    task_row.status = "completed"
    db = _FakeDb(scalars=[task_row])
    ctx = AdapterContext(db=db, worker_id="w1")

    await SubAgentAdapter().on_dead_letter(
        ctx, _dead_letter_execution(str(task_row.id))
    )

    assert task_row.status == "completed"
    assert wakes == []
    assert db.commits == 0


# ── Missing-task-row path closes delegation + run (ORCH-2) ───────────────────


@pytest.mark.asyncio
async def test_missing_task_row_closes_delegation_and_assignment_run(monkeypatch):
    """When the SubAgentTask row is gone, the failed outcome maps to a
    NON-retryable adapter failure that never reaches ``on_dead_letter`` —
    so the body itself must fail the linked AssignmentRun and close the
    delegation audit row (otherwise both stay pending forever)."""
    from app.core import database as database_mod

    run = SimpleNamespace(status="pending", error=None, completed_at=None)
    delegation = _make_delegation()
    # Consecutive executes: task lookup (missing) → AssignmentRun lookup →
    # delegation lookup.
    db = _FakeDb(scalars=[None, run, delegation])
    monkeypatch.setattr(database_mod, "async_session", lambda: _FakeSessionCM(db))

    task_id = str(uuid.uuid4())
    outcome = await _execute_sub_agent_task(
        task_id=task_id,
        user_id=str(uuid.uuid4()),
        target_agent_id=str(uuid.uuid4()),
        task_description="do the thing",
        assignment_run_id=str(uuid.uuid4()),
    )

    assert not outcome.succeeded
    assert outcome.error == f"Sub-agent task {task_id} not found"
    assert run.status == "failed"
    assert run.error == outcome.error
    assert delegation.status == "failed"
    assert delegation.error == outcome.error
    assert delegation.completed_at is not None
    assert db.commits == 1


# ── Delegation lifecycle (ORCH-2) ────────────────────────────────────────────


def _make_delegation() -> SimpleNamespace:
    return SimpleNamespace(
        status="pending",
        started_at=None,
        completed_at=None,
        result=None,
        error=None,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_update_delegation_pending_to_running_sets_started_at():
    delegation = _make_delegation()
    db = _FakeDb(scalar=delegation)

    updated = await update_delegation(
        db,
        sub_agent_task_id=str(uuid.uuid4()),
        status="running",
    )

    assert updated is delegation
    assert delegation.status == "running"
    assert delegation.started_at is not None
    assert delegation.completed_at is None
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_update_delegation_pending_to_completed():
    delegation = _make_delegation()
    db = _FakeDb(scalar=delegation)

    updated = await update_delegation(
        db,
        sub_agent_task_id=str(uuid.uuid4()),
        status="completed",
        result="the answer",
    )

    assert updated is delegation
    assert delegation.status == "completed"
    assert delegation.result == "the answer"
    assert delegation.error is None
    assert delegation.started_at is not None
    assert delegation.completed_at is not None
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_update_delegation_pending_to_failed():
    delegation = _make_delegation()
    db = _FakeDb(scalar=delegation)

    updated = await update_delegation(
        db,
        sub_agent_task_id=str(uuid.uuid4()),
        status="failed",
        error="Task timed out after 900s",
    )

    assert updated is delegation
    assert delegation.status == "failed"
    assert delegation.error == "Task timed out after 900s"
    assert delegation.result is None
    assert delegation.completed_at is not None
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_update_delegation_keeps_the_whole_answer_and_bounds_the_error():
    """A 60,000-character answer is stored whole (it used to be cut at 50,000
    with no marker); only the error stays bounded."""
    delegation = _make_delegation()
    db = _FakeDb(scalar=delegation)

    await update_delegation(
        db,
        sub_agent_task_id=str(uuid.uuid4()),
        status="failed",
        result="r" * 60_000,
        error="e" * 6_000,
    )

    assert delegation.result == "r" * 60_000
    assert len(delegation.error) == 5_000


@pytest.mark.asyncio
async def test_update_delegation_returns_none_when_no_row():
    db = _FakeDb(scalar=None)

    updated = await update_delegation(
        db,
        sub_agent_task_id=str(uuid.uuid4()),
        status="completed",
    )

    assert updated is None
    assert db.flushes == 0


@pytest.mark.asyncio
async def test_update_delegation_preserves_existing_started_at():
    started = datetime.now(UTC)
    delegation = _make_delegation()
    delegation.started_at = started
    db = _FakeDb(scalar=delegation)

    await update_delegation(
        db,
        sub_agent_task_id=str(uuid.uuid4()),
        status="completed",
    )

    assert delegation.started_at is started

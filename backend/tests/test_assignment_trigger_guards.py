"""Tests for the assignment trigger guards (assessment findings ORCH-4, ORCH-5).

ORCH-4: ``execute_assignment_run`` must refuse to re-execute a run that is
already in a terminal state (completed / failed / skipped / cancelled) — a
lease reclaim, mistaken requeue, or duplicate orchestration dispatch must not
replay side effects. The HITL resume path (``awaiting_human``) must remain
re-entrant.

ORCH-5: ``trigger_assignment(skip_if_active=True)`` is the centralized,
opt-in overlap guard for burst-prone automatic triggers (webhooks,
integration events, chaining): when the assignment already has an active run
the trigger is skipped (returns ``None``) instead of stacking parallel runs.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.services import assignment_engine as ae


@pytest.fixture(autouse=True)
def _ai_enabled(monkeypatch):
    """AI on, without a database.

    ``trigger_assignment`` and ``trigger_or_queue_assignment`` now refuse when
    AI is switched off — gated at the service so the tick, the manual
    route and the external webhooks all inherit it. Resolving that opens a
    session, which these suites do not have; they are about delay, queueing and
    run-as semantics behind the gate.
    """
    from app.services import assignment_engine as _engine

    async def _never_refuse(_assignment) -> bool:
        return False

    monkeypatch.setattr(_engine, "_refuse_if_ai_disabled", _never_refuse)


# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Fake AsyncSession returning queued scalar results in FIFO order.

    Statements that don't consume a queued result (e.g. the advisory-lock
    ``SELECT pg_advisory_xact_lock(...)`` taken by the overlap guard) get a
    ``None`` scalar. Every statement is recorded in ``statements`` so tests
    can assert on the SQL that was issued.
    """

    def __init__(self, results=()):
        self.results = list(results)
        self.execute_calls = 0
        self.commits = 0
        self.added = []
        self.statements = []

    async def execute(self, stmt, params=None):
        self.execute_calls += 1
        self.statements.append(stmt)
        if not self.results:
            return _FakeResult(None)
        return _FakeResult(self.results.pop(0))

    def lock_statements(self):
        return [s for s in self.statements if "pg_advisory_xact_lock" in str(s)]

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def _build_agent() -> Agent:
    return Agent(id=uuid.uuid4(), name="Test Agent", enabled=True)


def _build_assignment(agent_id: uuid.UUID) -> Assignment:
    return Assignment(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="Guarded Assignment",
        trigger_type="webhook",
        trigger_config={},
        prompt="Do work",
        enabled=True,
    )


def _build_run(status: str) -> AssignmentRun:
    return AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status=status,
    )


# ── Status vocabulary sanity ─────────────────────────────────────────────────


def test_status_sets_are_disjoint_and_awaiting_human_is_active():
    # HITL resume re-enters execute_assignment_run on a paused run, so
    # awaiting_human must never be classified as terminal.
    assert "awaiting_human" in ae.ACTIVE_RUN_STATUSES
    assert "awaiting_human" not in ae.TERMINAL_RUN_STATUSES
    assert set(ae.ACTIVE_RUN_STATUSES).isdisjoint(ae.TERMINAL_RUN_STATUSES)
    assert {"completed", "failed"} <= set(ae.TERMINAL_RUN_STATUSES)


# ── ORCH-4: terminal-state guard on execute_assignment_run ──────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "skipped", "cancelled"])
async def test_execute_refuses_terminal_run_without_mutation(monkeypatch, status):
    run = _build_run(status)
    session = _FakeSession(results=[run])
    monkeypatch.setattr(ae, "async_session", lambda: session)

    await ae.execute_assignment_run(str(run.id))

    # Returned right after the run load: status untouched, nothing committed,
    # no further queries (assignment/agent never loaded).
    assert run.status == status
    assert run.started_at is None
    assert session.commits == 0
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_execute_allows_awaiting_human_resume(monkeypatch):
    # An awaiting_human run must pass the terminal-state guard (HITL resume).
    # The fake session then returns no assignment, so execution proceeds past
    # the guard and fails on the assignment lookup — proving the guard did
    # NOT short-circuit the resume path.
    run = _build_run("awaiting_human")
    session = _FakeSession(results=[run, None])
    monkeypatch.setattr(ae, "async_session", lambda: session)

    await ae.execute_assignment_run(str(run.id))

    assert run.status == "failed"
    assert run.error == "Assignment not found"
    assert session.commits == 1
    assert session.execute_calls == 2


# ── has_active_run ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_has_active_run_true_when_active_run_exists():
    session = _FakeSession(results=[uuid.uuid4()])
    assert await ae.has_active_run(session, uuid.uuid4()) is True


@pytest.mark.asyncio
async def test_has_active_run_false_when_no_active_run():
    session = _FakeSession(results=[None])
    assert await ae.has_active_run(session, uuid.uuid4()) is False


# ── ORCH-5: trigger_assignment(skip_if_active=True) ─────────────────────────


def _patch_engine(monkeypatch):
    """Capture create_execution + notify_orchestration_wake calls."""
    created: list[dict] = []

    async def _mock_create_execution(db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def _mock_wake():
        return None

    monkeypatch.setattr(ae, "create_execution", _mock_create_execution)
    monkeypatch.setattr(ae, "notify_orchestration_wake", _mock_wake)
    return created


@pytest.mark.asyncio
async def test_trigger_skips_when_active_run_exists(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)

    async def _active(_db, _assignment_id):
        return True

    monkeypatch.setattr(ae, "has_active_run", _active)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=session,
        trigger_context={"type": "webhook"},
        skip_if_active=True,
    )

    assert run_id is None
    assert session.added == []
    assert created == []
    # The per-assignment advisory lock must have been taken even on the
    # skip path (it serializes the check itself).
    assert len(session.lock_statements()) == 1
    # The skip path must COMMIT to end the transaction and release the
    # advisory lock immediately — otherwise the lock would stay held until
    # the caller's next commit/rollback (piling up across webhook fan-out
    # loops and the long-lived scheduler session).
    assert session.commits == 1


@pytest.mark.asyncio
async def test_trigger_proceeds_when_no_active_run(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    async def _not_active(_db, _assignment_id):
        return False

    monkeypatch.setattr(ae, "has_active_run", _not_active)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=session,
        trigger_context={"type": "webhook"},
        skip_if_active=True,
    )

    assert run_id is not None
    assert len(session.added) == 1
    run = session.added[0]
    assert str(run.id) == run_id
    assert run.status == "pending"
    assert len(created) == 1
    assert created[0]["dedup_key"] == f"assignment_run:{run_id}"
    assert session.commits == 1
    assert len(session.lock_statements()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("active", "started"), [(2, True), (3, False), (5, False)])
async def test_trigger_counts_active_runs_against_max_active_runs(monkeypatch, active, started):
    """A work-item inbox with max_concurrent_runs 3: the guard counts
    the active runs under the advisory lock and refuses once the cap is met."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    async def _count(_db, _assignment_id):
        return active

    async def _never(*_a, **_k):
        raise AssertionError("a counted cap never reads the one-run guard")

    monkeypatch.setattr(ae, "count_active_runs", _count)
    monkeypatch.setattr(ae, "has_active_run", _never)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=session,
        trigger_context={"type": "work_item"},
        skip_if_active=True,
        max_active_runs=3,
    )

    assert (run_id is not None) is started
    assert len(created) == (1 if started else 0)
    assert len(session.lock_statements()) == 1


@pytest.mark.asyncio
async def test_trigger_skip_guard_locks_before_checking(monkeypatch):
    # The check-then-insert is only atomic if the transaction-scoped
    # advisory lock is acquired BEFORE has_active_run is consulted — two
    # concurrent triggers must serialize on the lock so the second one
    # observes the first one's committed run.
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    session = _FakeSession()
    locks_seen_at_check: list[int] = []

    async def _not_active(_db, _assignment_id):
        locks_seen_at_check.append(len(session.lock_statements()))
        return False

    monkeypatch.setattr(ae, "has_active_run", _not_active)

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=session,
        trigger_context={"type": "webhook"},
        skip_if_active=True,
    )

    assert run_id is not None
    # has_active_run was consulted exactly once, with the lock already held.
    assert locks_seen_at_check == [1]


@pytest.mark.asyncio
async def test_trigger_default_does_not_check_active_runs(monkeypatch):
    # Backward compatibility: the scheduler and the manual API keep their own
    # guards and call trigger_assignment WITHOUT skip_if_active — the helper
    # must not be consulted (no double-guarding).
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    async def _explode(_db, _assignment_id):
        raise AssertionError("has_active_run must not be called by default")

    monkeypatch.setattr(ae, "has_active_run", _explode)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=session,
        trigger_context={"type": "schedule"},
    )

    assert run_id is not None
    assert len(session.added) == 1
    # No advisory lock either — the default path must stay overhead-free.
    assert session.lock_statements() == []

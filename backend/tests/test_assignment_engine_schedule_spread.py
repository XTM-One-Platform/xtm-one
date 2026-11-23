"""Tests for schedule-spread offset computation and the orchestration-engine
dispatch path of ``trigger_assignment``.

Schedule-triggered runs get a deterministic per-assignment spread offset added
to the orchestration execution's ``run_at``; the reconciler claims the
execution when due. There is no longer a ``delayed_run:*`` Redis key or SAQ
scheduled set involved.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment
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



class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _FakeDB:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def execute(self, stmt, params=None):
        # The deadline floor's platform-default lookup
        # (resolve_run_max_iterations) gets "no GA row" → code default.
        return _FakeResult()

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


def _build_assignment(agent_id: uuid.UUID, *, delay: str | None) -> Assignment:
    return Assignment(
        id=uuid.UUID("00000000-0000-0000-0000-00000000000f"),
        agent_id=agent_id,
        name="Scheduled Assignment",
        trigger_type="schedule",
        trigger_config={"cron": "* * * * *"},
        prompt="Do work",
        enabled=True,
        delay=delay,
    )


# ── Spread-offset computation ────────────────────────────────────────────────


def test_schedule_spread_delay_only_applies_to_schedule(monkeypatch):
    assignment_id = uuid.UUID("00000000-0000-0000-0000-00000000000f")
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 30)

    schedule_delay = ae._schedule_spread_delay_seconds(
        assignment_id, {"type": "schedule"}
    )
    webhook_delay = ae._schedule_spread_delay_seconds(assignment_id, {"type": "webhook"})
    none_delay = ae._schedule_spread_delay_seconds(assignment_id, None)

    assert schedule_delay == assignment_id.int % 31
    assert webhook_delay == 0
    assert none_delay == 0


def test_schedule_spread_skipped_for_first_run(monkeypatch):
    """The first post-creation scheduled run skips the spread entirely so a
    freshly created assignment starts immediately instead of sitting in
    "delayed / waiting…". Subsequent (recurring) runs keep the spread."""
    assignment_id = uuid.UUID("00000000-0000-0000-0000-00000000000f")
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 900)

    first = ae._schedule_spread_delay_seconds(
        assignment_id, {"type": "schedule", "_first_run": True}
    )
    recurring = ae._schedule_spread_delay_seconds(
        assignment_id, {"type": "schedule", "_first_run": False}
    )
    # A missing flag (legacy runs) behaves like a recurring run — spread kept.
    legacy = ae._schedule_spread_delay_seconds(assignment_id, {"type": "schedule"})

    assert first == 0
    assert recurring == assignment_id.int % 901
    assert legacy == assignment_id.int % 901


def test_schedule_spread_disabled_when_zero(monkeypatch):
    assignment_id = uuid.UUID("00000000-0000-0000-0000-00000000000f")
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)
    assert ae._schedule_spread_delay_seconds(assignment_id, {"type": "schedule"}) == 0


def test_schedule_spread_window_one(monkeypatch):
    assignment_id = uuid.UUID("00000000-0000-0000-0000-00000000000f")
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 1)
    result = ae._schedule_spread_delay_seconds(assignment_id, {"type": "schedule"})
    # Inclusive `[0, 1]` range — two distinct buckets.
    assert 0 <= result <= 1


def test_settings_default_schedule_spread_disabled():
    """The spread is OPT-IN: the code default must stay 0 so scheduled runs
    start punctually. The reconciler's concurrency semaphore already absorbs
    the ":00" thundering herd, so the spread is only useful for very large
    fleets that explicitly enable it. A regression that re-enables it by
    default (e.g. flips back to 900) trips the suite."""
    from app.core.config import Settings

    default = Settings.model_fields["scheduled_assignment_spread_seconds"].default
    assert default == 0, (
        "scheduled_assignment_spread_seconds code default must stay 0 "
        f"(spread is opt-in; scheduled runs must start on time); got {default}"
    )


# ── trigger_assignment → orchestration execution ─────────────────────────────


def _patch_engine(monkeypatch):
    """Capture create_execution + notify_orchestration_wake calls."""
    created: list[dict] = []
    woke: list[bool] = []

    async def _mock_create_execution(
        db,
        *,
        adapter,
        payload,
        run_at=None,
        deadline_at=None,
        dedup_key=None,
        max_attempts=None,
    ):
        created.append(
            {
                "adapter": adapter,
                "payload": payload,
                "run_at": run_at,
                "deadline_at": deadline_at,
                "dedup_key": dedup_key,
            }
        )
        return SimpleNamespace(id=uuid.uuid4())

    async def _mock_wake():
        woke.append(True)

    monkeypatch.setattr(ae, "create_execution", _mock_create_execution)
    monkeypatch.setattr(ae, "notify_orchestration_wake", _mock_wake)
    return created, woke


@pytest.mark.asyncio
async def test_trigger_assignment_no_delay_creates_pending_execution(monkeypatch):
    fake_db = _FakeDB()
    agent = _build_agent()
    assignment = _build_assignment(agent.id, delay=None)
    created, woke = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=fake_db,
        trigger_context={"type": "schedule"},
    )

    assert fake_db.commits == 1
    run = fake_db.added[0]
    assert str(run.id) == run_id
    assert run.status == "pending"
    assert len(created) == 1
    assert created[0]["adapter"] == "assignment"
    assert created[0]["payload"]["run_id"] == run_id
    assert created[0]["payload"]["start_budget_seconds"] > 0
    assert created[0]["dedup_key"] == f"assignment_run:{run_id}"
    assert created[0]["deadline_at"] is not None
    assert woke == [True]


@pytest.mark.asyncio
async def test_trigger_assignment_with_delay_marks_delayed_and_schedules_run_at(
    monkeypatch,
):
    fake_db = _FakeDB()
    agent = _build_agent()
    assignment = _build_assignment(agent.id, delay="10s")
    created, woke = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 30)

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=fake_db,
        trigger_context={"type": "schedule"},
    )

    run = fake_db.added[0]
    assert str(run.id) == run_id
    # A configured delay (+ spread) means the user-facing status is "delayed"
    # and the execution is scheduled in the future via run_at.
    assert run.status == "delayed"
    assert len(created) == 1
    exec_args = created[0]
    assert exec_args["run_at"] is not None
    assert exec_args["deadline_at"] > exec_args["run_at"]
    assert woke == [True]


@pytest.mark.asyncio
async def test_trigger_assignment_disabled_timeout_sets_no_deadline(monkeypatch):
    # timeout_seconds == 0 means "disable the timeout" (per the model), so the
    # orchestration execution must carry deadline_at=None — otherwise
    # sweep_deadlines would dead-letter a run whose timeout was deliberately
    # turned off.
    fake_db = _FakeDB()
    agent = _build_agent()
    assignment = _build_assignment(agent.id, delay=None)
    assignment.timeout_seconds = 0
    created, woke = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=fake_db,
        trigger_context={"type": "schedule"},
    )

    assert len(created) == 1
    assert created[0]["deadline_at"] is None
    assert woke == [True]


@pytest.mark.asyncio
async def test_trigger_assignment_positive_timeout_sets_deadline(monkeypatch):
    # A positive timeout still yields a bounded deadline (regression guard that
    # the disable-fix didn't accidentally drop the deadline for normal runs).
    fake_db = _FakeDB()
    agent = _build_agent()
    assignment = _build_assignment(agent.id, delay=None)
    assignment.timeout_seconds = 120
    created, _woke = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=fake_db,
        trigger_context={"type": "schedule"},
    )

    assert created[0]["deadline_at"] is not None


@pytest.mark.asyncio
async def test_trigger_assignment_deadline_applies_iteration_floor(monkeypatch):
    # timeout_seconds=300 with max_iterations=50 → the orchestration deadline
    # must track the floored budget (50 × 20 s = 1000 s) + grace, NOT the
    # stored 300 s. Without this, the reconciler's deadline-derived task
    # budget and sweep_deadlines would still kill the run at ~300 s + grace
    # even though the agentic loop's wait_for was floored.
    fake_db = _FakeDB()
    agent = _build_agent()
    assignment = _build_assignment(agent.id, delay=None)
    assignment.timeout_seconds = 300
    assignment.max_iterations = 50
    created, _woke = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=fake_db,
        trigger_context={"type": "schedule"},
    )

    exec_args = created[0]
    # The budget counts from the run's start: carried on the payload, applied at the claim.
    budget = exec_args["payload"]["start_budget_seconds"]
    expected = (
        50 * ae._MIN_SECS_PER_ITERATION + ae.settings.orchestration_grace_seconds
    )
    assert budget == pytest.approx(expected)


@pytest.mark.asyncio
async def test_trigger_assignment_deadline_applies_static_minimum_floor(monkeypatch):
    # timeout_seconds=300 with max_iterations=20 → the orchestration deadline
    # must track the static 600 s minimum + grace, NOT the old iteration-only
    # floor (20 × 20 s = 400 s). Locks the fix at the FSM deadline layer:
    # without it, sweep_deadlines would still dead-letter the weekly SOP run
    # at ~400 s + grace even though the agentic loop's wait_for was raised.
    fake_db = _FakeDB()
    agent = _build_agent()
    assignment = _build_assignment(agent.id, delay=None)
    assignment.timeout_seconds = 300
    assignment.max_iterations = 20
    created, _woke = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=fake_db,
        trigger_context={"type": "schedule"},
    )

    exec_args = created[0]
    budget = exec_args["payload"]["start_budget_seconds"]
    expected = (
        ae._MIN_ASSIGNMENT_TIMEOUT_SECONDS + ae.settings.orchestration_grace_seconds
    )
    assert budget == pytest.approx(expected)


@pytest.mark.asyncio
async def test_trigger_assignment_generous_timeout_deadline_not_floored(monkeypatch):
    # An explicitly generous timeout above the floor keeps its own deadline —
    # the floor is purely additive and never reduces the operator's value.
    fake_db = _FakeDB()
    agent = _build_agent()
    assignment = _build_assignment(agent.id, delay=None)
    assignment.timeout_seconds = 3600
    assignment.max_iterations = 50  # floor 1000 s < 3600 s
    created, _woke = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)

    await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        db=fake_db,
        trigger_context={"type": "schedule"},
    )

    exec_args = created[0]
    budget = exec_args["payload"]["start_budget_seconds"]
    assert budget == pytest.approx(3600 + ae.settings.orchestration_grace_seconds)
    # Until a worker starts it, the deadline is the queue-wait bound.
    delta = (exec_args["deadline_at"] - exec_args["run_at"]).total_seconds()
    assert delta == pytest.approx(ae.settings.orchestration_max_queue_wait_seconds)

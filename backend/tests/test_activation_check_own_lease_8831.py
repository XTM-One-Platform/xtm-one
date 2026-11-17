"""A run is never skipped by its gate check on the lease it took itself (#8831).

An event-driven assignment gates on its branch lease::

    {"tool": "get_shared_state",
     "arguments": {"key": "lease:{{payload.pull_request.head.ref}}"},
     "condition": {"path": "$.held_by_run.alive", "none_in": [true]}}

The run passes the gate at dispatch, takes the lease, and is resumed after a
worker stop. Evaluated again, the gate read the run's own lease as a live
holder and ended the run skipped, the hold released with the work undone.
Now: the gate is evaluated once per run, a hold of the run itself is not
another holder, and a run holding a lease is never ended skipped.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import assignment_engine
from app.services.assignment_activation_check import run_activation_check, without_own_hold
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition, ToolResult

LEASE_CHECK = {
    "tool": "get_shared_state",
    "arguments": {"key": "lease:{{payload.pull_request.head.ref}}"},
    "condition": {"path": "$.held_by_run.alive", "none_in": [True]},
}
PAYLOAD = {"action": "created", "pull_request": {"number": 12, "head": {"ref": "issue/12-fix"}}}
RUN_ID = str(uuid.uuid4())
HELD_BY_ITSELF = {"key": "lease:issue/12-fix", "held_by_run": {"id": RUN_ID, "status": "running", "alive": True}}
HELD_BY_ANOTHER = {
    "key": "lease:issue/12-fix",
    "held_by_run": {"id": str(uuid.uuid4()), "status": "running", "alive": True},
}


def _executor(state: dict) -> CompositeToolExecutor:
    async def _get(key: str = ""):
        return ToolResult("", "state", structured=state)

    inner = CallableToolExecutor()
    inner.register("get_shared_state", _get)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="get_shared_state", description="read")], inner)
    return composite


def test_without_own_hold_drops_only_the_runs_own_holder():
    assert without_own_hold(HELD_BY_ITSELF, RUN_ID) == {"key": "lease:issue/12-fix"}
    assert without_own_hold(HELD_BY_ANOTHER, RUN_ID) == HELD_BY_ANOTHER
    assert without_own_hold(HELD_BY_ITSELF, None) == HELD_BY_ITSELF
    assert without_own_hold([HELD_BY_ITSELF], RUN_ID) == [HELD_BY_ITSELF]


async def test_a_lease_the_run_holds_itself_passes_none_in():
    outcome = await run_activation_check(LEASE_CHECK, _executor(HELD_BY_ITSELF), payload=PAYLOAD, run_id=RUN_ID)
    assert outcome.passed and outcome.error is None


async def test_a_lease_held_by_another_live_run_still_fails_the_check():
    outcome = await run_activation_check(LEASE_CHECK, _executor(HELD_BY_ANOTHER), payload=PAYLOAD, run_id=RUN_ID)
    assert not outcome.passed
    assert outcome.summary == "Skipped: gate check get_shared_state: $.held_by_run.alive is in [true]"


@pytest.mark.parametrize(
    ("status", "resuming", "due"),
    [
        ("pending", False, True),
        ("running", True, False),
        ("pending", True, False),
        ("awaiting_human", False, False),
    ],
)
def test_the_gate_is_evaluated_once_per_run_at_dispatch(status, resuming, due):
    assert assignment_engine._gate_check_due(SimpleNamespace(status=status), resuming=resuming) is due


class _Rows:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Db:
    def __init__(self, holds: bool):
        self.holds = holds
        self.commits = 0

    async def execute(self, _statement):
        return _Rows((uuid.uuid4(),) if self.holds else None)

    async def commit(self):
        self.commits += 1


def _run():
    return SimpleNamespace(
        id=uuid.UUID(RUN_ID),
        user_id=uuid.uuid4(),
        status="pending",
        result_summary=None,
        messages=None,
        total_input_tokens=None,
        total_output_tokens=None,
        started_at=None,
        completed_at=None,
        is_company_managed=False,
        trigger_context={},
        trigger_payload=PAYLOAD,
    )


@pytest.fixture
def _engine(monkeypatch):
    released: list = []

    async def _publish(*_args, **_kwargs):
        return None

    async def _release(run, agent, *, failed, error=None):
        released.append(run.id)

    async def _executor_for(*_args, **_kwargs):
        return _executor(HELD_BY_ANOTHER)

    async def _single_writer():
        return None

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _publish)
    monkeypatch.setattr(assignment_engine, "_release_run_work_item", _release)
    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)
    monkeypatch.setattr(assignment_engine, "ensure_single_writer", _single_writer)
    return released


async def _gate(db, run):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Desk")
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Red check", activation_check=LEASE_CHECK)
    return await assignment_engine._skip_by_activation_check(
        db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
    )


async def test_a_run_holding_a_lease_is_never_ended_skipped(_engine):
    run, db = _run(), _Db(holds=True)
    assert await _gate(db, run) is False
    assert run.status == "pending" and run.result_summary is None
    assert db.commits == 0 and _engine == []


async def test_a_run_holding_no_lease_is_still_skipped(_engine):
    run, db = _run(), _Db(holds=False)
    assert await _gate(db, run) is True
    assert run.status == "completed"
    assert run.result_summary.startswith("Skipped: gate check get_shared_state")
    assert _engine == [run.id]

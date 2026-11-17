"""Coverage for the per-assignment "run as" execution identity.

Two contracts are pinned here:

1. ``_resolve_run_as_user_id`` (``app.api.v1.assignments``) — validates and
   resolves the optional ``run_as_user_id`` on create / update:
     * ignored (returns ``None``) for non-company-managed agents,
     * ignored when no value is supplied,
     * rejects a malformed id (400),
     * rejects an id that does not reference a real user (400),
     * returns the parsed UUID for a valid user on a company-managed agent.

2. ``trigger_assignment`` (``app.services.assignment_engine``) — stamps the
   created ``AssignmentRun`` with the right execution identity:
     ``triggered_by_user_id`` > ``assignment.run_as_user_id`` > owner.
   This is what drives per-user integration / model / memory resolution
   downstream, so a regression here silently breaks the company-managed
   per-user integration binding this feature exists to fix.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import assignments as assignments_api
from app.services import assignment_engine


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



class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _UserLookupDB:
    """Fake DB whose ``execute`` returns a preset user-id lookup result."""

    def __init__(self, found_user_id):
        self._found = found_user_id

    async def execute(self, _stmt):
        return _ScalarResult(self._found)


def _agent(*, is_company_managed: bool):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=is_company_managed,
    )


# ---------------------------------------------------------------------------
# _resolve_run_as_user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_run_as_ignored_for_private_agent():
    """A private (non-company-managed) agent ignores run_as entirely — runs
    always execute as the assignment owner, so the value resolves to None
    even when a syntactically valid user id is supplied."""
    agent = _agent(is_company_managed=False)
    # DB should never be consulted; a poisoned execute proves we short-circuit.
    db = _UserLookupDB(found_user_id=uuid.uuid4())
    resolved = await assignments_api._resolve_run_as_user_id(
        db, agent, str(uuid.uuid4())
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_run_as_none_when_not_supplied():
    agent = _agent(is_company_managed=True)
    db = _UserLookupDB(found_user_id=None)
    assert await assignments_api._resolve_run_as_user_id(db, agent, None) is None
    assert await assignments_api._resolve_run_as_user_id(db, agent, "") is None


@pytest.mark.asyncio
async def test_resolve_run_as_rejects_malformed_id():
    agent = _agent(is_company_managed=True)
    db = _UserLookupDB(found_user_id=None)
    with pytest.raises(HTTPException):
        await assignments_api._resolve_run_as_user_id(db, agent, "not-a-uuid")


@pytest.mark.asyncio
async def test_resolve_run_as_rejects_unknown_user():
    """A well-formed id that does not reference a real user is a 400 — not a
    silently-ignored value — so a stale / deleted run-as user surfaces to the
    admin instead of falling back to the owner without warning."""
    agent = _agent(is_company_managed=True)
    db = _UserLookupDB(found_user_id=None)  # lookup returns no row
    with pytest.raises(HTTPException) as exc:
        await assignments_api._resolve_run_as_user_id(db, agent, str(uuid.uuid4()))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_run_as_returns_uuid_for_valid_user():
    agent = _agent(is_company_managed=True)
    target = uuid.uuid4()
    db = _UserLookupDB(found_user_id=target)
    resolved = await assignments_api._resolve_run_as_user_id(db, agent, str(target))
    assert resolved == target


# ---------------------------------------------------------------------------
# trigger_assignment execution-identity precedence
# ---------------------------------------------------------------------------


class _CaptureDB:
    """Captures the ``AssignmentRun`` added during ``trigger_assignment`` and
    no-ops the flush/commit so the run is never persisted or executed."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt, params=None):
        # The deadline floor's platform-default lookup
        # (resolve_run_max_iterations) gets "no GA row" → code default.
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _trigger_assignment_fixtures(monkeypatch):
    """Stub the orchestration-engine hooks so ``trigger_assignment`` exercises
    only the run-creation identity logic (no DB execution / Redis wake)."""
    created: list[dict] = []

    async def _create_execution(db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def _notify_wake():
        return None

    monkeypatch.setattr(assignment_engine, "create_execution", _create_execution)
    monkeypatch.setattr(assignment_engine, "notify_orchestration_wake", _notify_wake)
    return created


def _assignment(*, owner_id, run_as_user_id, is_company_managed=True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="Nightly digest",
        enabled=True,
        delay="10m",  # non-zero → scheduled in the future via run_at
        timeout_seconds=300,
        max_iterations=None,
        user_id=owner_id,
        run_as_user_id=run_as_user_id,
        is_company_managed=is_company_managed,
    )


@pytest.mark.asyncio
async def test_trigger_stamps_run_with_run_as_user_for_automatic_trigger(monkeypatch):
    """Fully automatic trigger (no human in the loop) on a company-managed
    assignment stamps the run with the configured run-as user, NOT the owner."""
    _trigger_assignment_fixtures(monkeypatch)
    owner_id = uuid.uuid4()
    run_as_id = uuid.uuid4()
    assignment = _assignment(owner_id=owner_id, run_as_user_id=run_as_id)
    agent = SimpleNamespace(enabled=True, name="CTEM Assistant", max_iterations=None)
    db = _CaptureDB()

    await assignment_engine.trigger_assignment(
        assignment, agent, payload={}, db=db, trigger_context=None
    )

    assert len(db.added) == 1
    run = db.added[0]
    assert run.user_id == run_as_id
    assert run.is_company_managed is True


@pytest.mark.asyncio
async def test_trigger_prefers_triggering_user_over_run_as(monkeypatch):
    """A human-initiated trigger (manual UI / agent-builder tool) runs as the
    acting user, overriding the assignment's run-as default."""
    _trigger_assignment_fixtures(monkeypatch)
    owner_id = uuid.uuid4()
    run_as_id = uuid.uuid4()
    triggered_by = uuid.uuid4()
    assignment = _assignment(owner_id=owner_id, run_as_user_id=run_as_id)
    agent = SimpleNamespace(enabled=True, name="CTEM Assistant", max_iterations=None)
    db = _CaptureDB()

    await assignment_engine.trigger_assignment(
        assignment,
        agent,
        payload={},
        db=db,
        trigger_context=None,
        triggered_by_user_id=triggered_by,
    )

    run = db.added[0]
    assert run.user_id == triggered_by


@pytest.mark.asyncio
async def test_trigger_falls_back_to_owner_without_run_as(monkeypatch):
    """With neither a triggering user nor a run-as override, the run executes
    as the assignment owner."""
    _trigger_assignment_fixtures(monkeypatch)
    owner_id = uuid.uuid4()
    assignment = _assignment(owner_id=owner_id, run_as_user_id=None)
    agent = SimpleNamespace(enabled=True, name="CTEM Assistant", max_iterations=None)
    db = _CaptureDB()

    await assignment_engine.trigger_assignment(
        assignment, agent, payload={}, db=db, trigger_context=None
    )

    run = db.added[0]
    assert run.user_id == owner_id

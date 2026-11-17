"""Assignment list writes commit before they answer.

``get_db`` commits on dependency teardown, which FastAPI runs AFTER the
response is sent. The agent page's Add Assignment panel invalidates the
agent's assignment list the moment ``POST /assignments`` answers, and the
refetch it fires can reach the database before the create lands: the list
then reads "No assignments yet" and, with no poll on it, keeps saying so.
The ``assignments.spec.ts`` E2E spec caught it waiting 15 s for a row that
never came. Deleting an assignment has the same shape: the list refetched
right after the 204 can still hold the deleted row.

Both routes are driven through a session stub that records its calls in
order. The commit has to be the last of them: the response is built after
it, and the write and its audit row belong to the transaction it lands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.v1 import assignments as assignments_api


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RecordingSession:
    """Just enough ``AsyncSession`` for the assignment routes, in call order."""

    def __init__(self, *results):
        self.calls: list[str] = []
        self.added: list[object] = []
        self._results = list(results)

    async def execute(self, _stmt):
        self.calls.append("execute")
        return _Result(self._results.pop(0) if self._results else None)

    def add(self, obj):
        self.calls.append("add")
        self.added.append(obj)

    async def flush(self):
        self.calls.append("flush")
        # The server-side defaults a real flush would populate.
        now = datetime.now(UTC)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            obj.created_at = now
            obj.updated_at = now
            if getattr(obj, "execution_count", None) is None:
                obj.execution_count = 0

    async def delete(self, _obj):
        self.calls.append("delete")

    async def commit(self):
        self.calls.append("commit")

    async def rollback(self):
        self.calls.append("rollback")


async def _record_audit(db, **_kw) -> None:
    db.calls.append("audit")


async def _no_groups(_db, _uid):
    return []


def _allowed(*_a, **_kw) -> None:
    return None


def _assert_committed_last(session: _RecordingSession, *writes: str) -> None:
    calls = session.calls
    assert calls.count("commit") == 1, f"expected exactly one commit: {calls}"
    assert calls[-1] == "commit", (
        "the commit must be the route's last session call, so the response "
        f"only ever describes committed state: {calls}"
    )
    for write in writes:
        assert write in calls, f"expected {write!r} inside the transaction: {calls}"


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin")


async def test_create_assignment_commits_before_responding(monkeypatch):
    user = _admin()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        is_company_managed=False,
        name="Agent",
        builtin_tool_ids=[],
    )

    async def _get_agent(*_a, **_kw):
        return agent

    async def _run_as(_db, _agent, _value):
        return None

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(assignments_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(assignments_api, "assert_writable_agent", _allowed)
    monkeypatch.setattr(assignments_api, "_resolve_run_as_user_id", _run_as)
    monkeypatch.setattr(assignments_api, "log_audit", _record_audit)
    session = _RecordingSession()

    response = await assignments_api.create_assignment(
        body=assignments_api.CreateAssignmentRequest(
            agent_id=str(agent.id),
            name="Daily Summary",
            prompt="Summarise today's activity.",
            trigger_type="manual",
            trigger_config={},
        ),
        request=_request(),
        user=user,
        db=session,
        _agentic_gate=None,
    )

    _assert_committed_last(session, "add", "flush", "audit")
    assert response.name == "Daily Summary"


async def test_delete_assignment_commits_before_responding(monkeypatch):
    user = _admin()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="Daily Summary",
        trigger_type="manual",
    )

    async def _get_assignment(*_a, **_kw):
        return assignment

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(assignments_api, "get_user_assignment", _get_assignment)
    monkeypatch.setattr(assignments_api, "assert_writable_assignment", _allowed)
    monkeypatch.setattr(assignments_api, "log_audit", _record_audit)
    # The parent-agent lookup.
    session = _RecordingSession(SimpleNamespace(id=assignment.agent_id))

    await assignments_api.delete_assignment(
        assignment_id=str(assignment.id),
        request=_request(),
        user=user,
        db=session,
        _ai_gate=None,
    )

    _assert_committed_last(session, "audit", "delete")

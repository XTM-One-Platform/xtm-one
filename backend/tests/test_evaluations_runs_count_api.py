"""API tests for ``GET /evaluations/{id}/runs/count``.

The Activity table paginates evaluation runs. Without a real total, the
frontend used to estimate it from the current page size, which made the page
count grow every time the user navigated to the next page (Page 1 of 2 → Page
2 of 3 → ...). This endpoint returns the real total so the page count is
stable.

The endpoint mirrors the ``/runs`` list endpoint's ``agent_id`` / ``model_id``
filters so the page count never diverges from the list it paginates — the
filter tests below assert the count query actually carries those WHERE clauses.
"""

import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.core.database import get_db


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeDb:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        # Captured for SQL-clause assertions (filter branch coverage).
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)


def _client(fake_db, fake_user):
    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")
    app.dependency_overrides[deps.get_current_user] = lambda: fake_user

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _patch_lookups(monkeypatch, evaluation):
    """Stub the auth/visibility lookups so the test exercises only the count."""

    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin")


def test_count_runs_returns_real_total(monkeypatch):
    evaluation_id = uuid.uuid4()
    evaluation = SimpleNamespace(id=evaluation_id, name="Eval")
    fake_db = _FakeDb(execute_results=[_ScalarOneResult(41)])
    _patch_lookups(monkeypatch, evaluation)

    with _client(fake_db, _admin()) as client:
        resp = client.get(f"/api/v1/evaluations/{evaluation_id}/runs/count")

    assert resp.status_code == 200
    assert resp.json() == {"count": 41}
    # Unfiltered: no agent/model predicate leaks into the query.
    sql = str(fake_db.statements[0])
    assert "agent_id" not in sql
    assert "model_id" not in sql


def test_count_runs_zero(monkeypatch):
    evaluation_id = uuid.uuid4()
    evaluation = SimpleNamespace(id=evaluation_id, name="Eval")
    fake_db = _FakeDb(execute_results=[_ScalarOneResult(0)])
    _patch_lookups(monkeypatch, evaluation)

    with _client(fake_db, _admin()) as client:
        resp = client.get(f"/api/v1/evaluations/{evaluation_id}/runs/count")

    assert resp.status_code == 200
    assert resp.json() == {"count": 0}


def test_count_runs_with_agent_filter(monkeypatch):
    evaluation_id, agent_id = uuid.uuid4(), uuid.uuid4()
    evaluation = SimpleNamespace(id=evaluation_id, name="Eval")
    fake_db = _FakeDb(execute_results=[_ScalarOneResult(7)])
    _patch_lookups(monkeypatch, evaluation)

    with _client(fake_db, _admin()) as client:
        resp = client.get(
            f"/api/v1/evaluations/{evaluation_id}/runs/count",
            params={"agent_id": str(agent_id)},
        )

    assert resp.status_code == 200
    assert resp.json() == {"count": 7}
    sql = str(fake_db.statements[0])
    assert "agent_id" in sql
    assert "model_id" not in sql


def test_count_runs_with_model_filter(monkeypatch):
    evaluation_id = uuid.uuid4()
    evaluation = SimpleNamespace(id=evaluation_id, name="Eval")
    fake_db = _FakeDb(execute_results=[_ScalarOneResult(3)])
    _patch_lookups(monkeypatch, evaluation)

    with _client(fake_db, _admin()) as client:
        resp = client.get(
            f"/api/v1/evaluations/{evaluation_id}/runs/count",
            params={"model_id": "gpt-4o"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"count": 3}
    sql = str(fake_db.statements[0])
    assert "model_id" in sql
    assert "agent_id" not in sql


def test_count_runs_with_both_filters(monkeypatch):
    evaluation_id, agent_id = uuid.uuid4(), uuid.uuid4()
    evaluation = SimpleNamespace(id=evaluation_id, name="Eval")
    fake_db = _FakeDb(execute_results=[_ScalarOneResult(2)])
    _patch_lookups(monkeypatch, evaluation)

    with _client(fake_db, _admin()) as client:
        resp = client.get(
            f"/api/v1/evaluations/{evaluation_id}/runs/count",
            params={"agent_id": str(agent_id), "model_id": "gpt-4o"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"count": 2}
    sql = str(fake_db.statements[0])
    assert "agent_id" in sql
    assert "model_id" in sql


def test_count_runs_invalid_agent_id_returns_422(monkeypatch):
    """A non-UUID ``agent_id`` is rejected (422) before the count query runs.

    The endpoint still performs the evaluation auth/visibility lookups first
    (stubbed here), so this asserts the count query itself is never executed
    when UUID validation fails — i.e. ``fake_db`` records no statements.
    """
    evaluation_id = uuid.uuid4()
    evaluation = SimpleNamespace(id=evaluation_id, name="Eval")
    fake_db = _FakeDb(execute_results=[])
    _patch_lookups(monkeypatch, evaluation)

    with _client(fake_db, _admin()) as client:
        resp = client.get(
            f"/api/v1/evaluations/{evaluation_id}/runs/count",
            params={"agent_id": "not-a-uuid"},
        )

    assert resp.status_code == 422
    assert fake_db.statements == []

"""Tests for GET /api/v1/agents/{agent_id}/feedback-comments.

Covers the authorization matrix (owner / admin / group permission /
company-managed) and the query semantics (non-empty-comment filter, rating
and search filters, pagination echo) of the agent feedback comments
endpoint added for the Observability tab.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import agent_chats as agent_chats_api
from app.core.database import get_db


class _RowResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FeedbackRow(SimpleNamespace):
    """Row-like object: ``row[0]`` is the MessageFeedback, labels are attrs."""

    def __getitem__(self, idx):
        if idx == 0:
            return self.feedback
        raise IndexError(idx)


class _FakeDb:
    def __init__(self, execute_results=None, scalar_results=None):
        self._execute_results = list(execute_results or [])
        self._scalar_results = list(scalar_results or [])
        self.execute_statements = []
        self.scalar_statements = []

    async def execute(self, statement, params=None):
        self.execute_statements.append(statement)
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        if not self._scalar_results:
            raise AssertionError("Unexpected scalar() call")
        return self._scalar_results.pop(0)


def _user(*, role: str = "user", user_id: uuid.UUID | None = None):
    uid = user_id or uuid.uuid4()
    return SimpleNamespace(
        id=uid,
        email=f"{role}@example.com",
        role=role,
        is_admin=role == "admin",
    )


def _agent(
    *,
    owner_id: uuid.UUID | None = None,
    is_company_managed: bool = False,
    shared_group_ids: list[str] | None = None,
    shared_group_permissions: dict[str, str] | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        is_company_managed=is_company_managed,
        shared_group_ids=shared_group_ids or [],
        shared_group_permissions=shared_group_permissions or {},
    )


def _feedback_row(*, rating: str = "negative", comment: str = "Wrong answer"):
    now = datetime.now(UTC)
    return _FeedbackRow(
        feedback=SimpleNamespace(
            id=uuid.uuid4(),
            rating=rating,
            comment=comment,
            created_at=now,
            updated_at=now,
        ),
        message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        channel="web",
        user_name="Jane Doe",
        user_email="jane@example.com",
    )


def _client(fake_db, fake_user, agent, monkeypatch, *, group_ids=None):
    async def _get_user_agent(_db, _uid, _agent_id, _group_ids=None, **_kwargs):
        return agent

    async def _get_user_group_ids(_db, _uid):
        return list(group_ids or [])

    monkeypatch.setattr(agent_chats_api, "get_user_agent", _get_user_agent)
    monkeypatch.setattr(agent_chats_api, "get_user_group_ids", _get_user_group_ids)

    app = FastAPI()
    app.include_router(agent_chats_api.router, prefix="/api/v1/agents")
    app.dependency_overrides[deps.get_current_user] = lambda: fake_user

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _url(agent, **params) -> str:
    from urllib.parse import urlencode

    qs = urlencode(params)
    return f"/api/v1/agents/{agent.id}/feedback-comments" + (f"?{qs}" if qs else "")


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------


def test_owner_can_list_feedback_comments(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(
        execute_results=[_RowResult([_feedback_row()])],
        scalar_results=[1],
    )

    client = _client(fake_db, user, agent, monkeypatch)
    resp = client.get(_url(agent))

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["rating"] == "negative"
    assert item["comment"] == "Wrong answer"
    assert item["user_name"] == "Jane Doe"
    assert item["channel"] == "web"


def test_admin_can_list_for_company_managed_agent(monkeypatch):
    user = _user(role="admin")
    agent = _agent(owner_id=None, is_company_managed=True)
    fake_db = _FakeDb(
        execute_results=[_RowResult([])],
        scalar_results=[0],
    )

    client = _client(fake_db, user, agent, monkeypatch)
    resp = client.get(_url(agent))

    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_non_owner_without_permission_is_forbidden(monkeypatch):
    user = _user()
    agent = _agent(owner_id=uuid.uuid4())
    fake_db = _FakeDb()

    client = _client(fake_db, user, agent, monkeypatch)
    resp = client.get(_url(agent))

    assert resp.status_code == 403
    # No query must have been issued before the refusal.
    assert fake_db.execute_statements == []
    assert fake_db.scalar_statements == []


def test_group_config_with_chats_grants_access(monkeypatch):
    user = _user()
    group_id = str(uuid.uuid4())
    agent = _agent(
        owner_id=uuid.uuid4(),
        shared_group_ids=[group_id],
        shared_group_permissions={group_id: "config_with_chats"},
    )
    fake_db = _FakeDb(
        execute_results=[_RowResult([])],
        scalar_results=[0],
    )

    client = _client(fake_db, user, agent, monkeypatch, group_ids=[group_id])
    resp = client.get(_url(agent))

    assert resp.status_code == 200


def test_group_config_grants_access(monkeypatch):
    """Aligned with the Observability gate: plain ``config`` may read feedback
    comments (the same audience that already sees chat content via traces)."""
    user = _user()
    group_id = str(uuid.uuid4())
    agent = _agent(
        owner_id=uuid.uuid4(),
        shared_group_ids=[group_id],
        shared_group_permissions={group_id: "config"},
    )
    fake_db = _FakeDb(
        execute_results=[_RowResult([])],
        scalar_results=[0],
    )

    client = _client(fake_db, user, agent, monkeypatch, group_ids=[group_id])
    resp = client.get(_url(agent))

    assert resp.status_code == 200


def test_company_managed_agent_forbidden_for_non_admin(monkeypatch):
    """Group permission cannot defeat the company-managed content gate."""
    user = _user()
    group_id = str(uuid.uuid4())
    agent = _agent(
        owner_id=None,
        is_company_managed=True,
        shared_group_ids=[group_id],
        shared_group_permissions={group_id: "config_with_chats"},
    )
    fake_db = _FakeDb()

    client = _client(fake_db, user, agent, monkeypatch, group_ids=[group_id])
    resp = client.get(_url(agent))

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Query semantics
# ---------------------------------------------------------------------------


def test_query_scopes_to_agent_and_non_empty_comments(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(
        execute_results=[_RowResult([])],
        scalar_results=[0],
    )

    client = _client(fake_db, user, agent, monkeypatch)
    resp = client.get(_url(agent))

    assert resp.status_code == 200
    list_sql = str(fake_db.execute_statements[-1]).lower()
    assert "conversations.agent_id" in list_sql
    # Only actionable qualitative feedback: comment IS NOT NULL and non-blank.
    assert "comment is not null" in list_sql
    assert "trim" in list_sql
    # Newest-first ordering for the UI list.
    assert "order by message_feedback.updated_at desc" in list_sql


def test_rating_and_search_filters_are_applied(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(
        execute_results=[_RowResult([_feedback_row(rating="positive", comment="Great")])],
        scalar_results=[1],
    )

    client = _client(fake_db, user, agent, monkeypatch)
    resp = client.get(_url(agent, rating="positive", search="Great"))

    assert resp.status_code == 200
    assert resp.json()["items"][0]["rating"] == "positive"
    list_sql = str(fake_db.execute_statements[-1]).lower()
    assert "message_feedback.rating" in list_sql
    # Case-insensitive comment search (ilike compiles to lower() LIKE lower()
    # on the generic dialect used by str()).
    assert "like" in list_sql
    assert "lower(message_feedback.comment)" in list_sql


def test_invalid_rating_is_rejected(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb()

    client = _client(fake_db, user, agent, monkeypatch)
    resp = client.get(_url(agent, rating="neutral"))

    assert resp.status_code == 422
    assert fake_db.execute_statements == []


def test_pagination_is_echoed_and_applied(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(
        execute_results=[_RowResult([_feedback_row()])],
        scalar_results=[25],
    )

    client = _client(fake_db, user, agent, monkeypatch)
    resp = client.get(_url(agent, limit=10, offset=20))

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 25
    assert data["limit"] == 10
    assert data["offset"] == 20
    list_sql = str(fake_db.execute_statements[-1]).lower()
    assert "limit" in list_sql
    assert "offset" in list_sql

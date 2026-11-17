"""Tests for the admin visibility bypass on assignment endpoints.

Admins open every agent from the Objects page (the agents DETAIL route passes
``is_admin=True``), so an admin opening another user's private agent must see
its assignments and reach the assignment detail / trigger / purge-runs
endpoints. The lists across agents read the whole platform for an
admin too: ``GET /assignments/schedule`` is what the admin's Flows > Schedule
tab shows, and admin API clients read ``GET /assignments``. The Manual
Triggers panel offers only the assignments whose agent its viewer's agent list
holds (``lib/manual-triggers.ts``), so it never lists an "Unknown agent".

These tests pin both halves:

* ``GET /assignments`` (one agent's or across agents) and
  ``GET /assignments/schedule`` take the admin bypass, and are three-tier
  for everyone else.
* ``GET /assignments/{id}``, ``POST /assignments/{id}/trigger`` and
  ``DELETE /assignments/{id}/runs`` must forward ``is_admin=user.is_admin``
  to ``get_user_assignment`` (the update/delete endpoints already did).

Mirrors the repo's fake-session test convention (sequenced ``execute``
results driven through a FastAPI ``TestClient``) - the backend suite has no
real-DB harness.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.deps import require_agentic_ai
from app.api.v1 import assignments as assignments_api
from app.core.database import get_db


class _ScalarsResult:
    """Result whose ``.scalars().all()`` returns the seeded rows."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SeqDB:
    """Async session stub returning queued results in ``execute`` order."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return self._results.pop(0)


def _make_client(monkeypatch, results, *, is_admin):
    fake_user = SimpleNamespace(
        id=uuid.uuid4(),
        is_admin=is_admin,
        role="admin" if is_admin else "user",
        email="user@example.com",
        full_name="User",
    )
    fake_db = _SeqDB(results)
    group_calls: list[uuid.UUID] = []

    async def _groups(_db, uid):
        group_calls.append(uid)
        return []

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    fake_db.group_calls = group_calls

    app = FastAPI()
    app.include_router(assignments_api.router, prefix="/api/v1/assignments")

    async def _get_db():
        yield fake_db

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai] = lambda: None
    # The manual trigger runs an agent, so it now also carries the AI gate:
    # without this the route 403s before reaching the lookup this
    # test is about.
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    return TestClient(app), fake_db, fake_user


def _where(statement) -> str:
    return str(statement.whereclause.compile(dialect=postgresql.dialect())).lower()


def _assert_three_tier(where: str) -> None:
    """The WHERE reads the parent agent's visibility, every tier of it."""
    assert "from agents" in where
    assert "agents.user_id" in where
    assert "agents.shared_user_ids" in where
    assert "agents.access_restricted is not true" in where


# ---------------------------------------------------------------------------
# GET /assignments - list visibility
# ---------------------------------------------------------------------------


def test_one_agent_s_assignments_keep_the_admin_bypass(monkeypatch):
    agent_id = uuid.uuid4()
    client, fake_db, _ = _make_client(
        monkeypatch, [_ScalarsResult([])], is_admin=True
    )
    resp = client.get(f"/api/v1/assignments?agent_id={agent_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    # The admin query keeps the agent_id filter but must NOT scope by the
    # agent's visibility: this is the agent's detail page.
    where = _where(fake_db.statements[0])
    assert "agent_id" in where
    assert "agents" not in where
    assert "user_id" not in where
    # Group memberships are irrelevant on the admin path — the extra DB
    # roundtrip must be skipped entirely.
    assert fake_db.group_calls == []


def test_the_list_across_agents_keeps_the_admin_bypass(monkeypatch):
    client, fake_db, _ = _make_client(
        monkeypatch, [_ScalarsResult([])], is_admin=True
    )
    resp = client.get("/api/v1/assignments")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    # The whole platform: no visibility scope at all, no group lookup.
    statement = fake_db.statements[0]
    assert statement.whereclause is None or "agents" not in _where(statement)
    assert fake_db.group_calls == []


@pytest.mark.parametrize("scoped", [False, True], ids=["across-agents", "one-agent"])
def test_the_list_is_three_tier_for_a_non_admin(monkeypatch, scoped):
    client, fake_db, _ = _make_client(
        monkeypatch, [_ScalarsResult([])], is_admin=False
    )
    query = f"?agent_id={uuid.uuid4()}" if scoped else ""
    resp = client.get(f"/api/v1/assignments{query}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    where = _where(fake_db.statements[0])
    _assert_three_tier(where)
    if scoped:
        assert "assignments.agent_id =" in where
    assert len(fake_db.group_calls) == 1


# ---------------------------------------------------------------------------
# GET /assignments/schedule - a list across agents too
# ---------------------------------------------------------------------------


def test_the_schedule_keeps_the_admin_bypass(monkeypatch):
    """An admin's Schedule tab reflects the whole platform."""
    client, fake_db, _ = _make_client(
        monkeypatch, [_ScalarsResult([])], is_admin=True
    )
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    where = _where(fake_db.statements[0])
    assert "assignments.enabled is true" in where
    assert "agents" not in where
    assert fake_db.group_calls == []


def test_the_schedule_is_three_tier_for_a_non_admin(monkeypatch):
    client, fake_db, user = _make_client(
        monkeypatch, [_ScalarsResult([])], is_admin=False
    )
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    where = _where(fake_db.statements[0])
    assert "assignments.enabled is true" in where
    _assert_three_tier(where)
    assert fake_db.group_calls == [user.id]


# ---------------------------------------------------------------------------
# Detail / trigger / purge-runs must forward is_admin to get_user_assignment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path_suffix"),
    [
        ("get", ""),
        ("post", "/trigger"),
        ("delete", "/runs"),
    ],
)
def test_assignment_lookup_forwards_is_admin(monkeypatch, method, path_suffix):
    recorded = {}

    async def _fake_get_user_assignment(_db, _uid, _aid, *args, **kwargs):
        recorded["is_admin"] = kwargs.get("is_admin", False)
        # Short-circuit the rest of the handler; the lookup contract is
        # the only thing under test here.
        raise HTTPException(status_code=404, detail="Assignment not found")

    monkeypatch.setattr(
        assignments_api, "get_user_assignment", _fake_get_user_assignment
    )

    client, _, _ = _make_client(monkeypatch, [], is_admin=True)
    resp = getattr(client, method)(f"/api/v1/assignments/{uuid.uuid4()}{path_suffix}")
    assert resp.status_code == 404
    assert recorded["is_admin"] is True

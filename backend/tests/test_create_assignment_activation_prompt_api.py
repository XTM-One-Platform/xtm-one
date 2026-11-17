"""``POST /assignments`` must persist
``activation_prompt``.

The ``CreateAssignmentRequest`` schema accepted ``activation_prompt`` and the
response serialiser echoed it, but the endpoint never wrote it onto the
``Assignment`` ORM row - so a gated assignment created through the REST API
(rather than the agent-builder tool) silently lost its activation gate.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import require_agentic_ai
from app.api.v1 import assignments as assignments_api
from app.core.database import get_db


class _CapturingDb:
    """Fake async session that captures added rows and mimics the server-side
    column defaults a real flush would populate (id + timestamps +
    execution_count) so ``_to_response`` can serialise the new assignment."""

    def __init__(self):
        self.added: list[object] = []
        self.committed = False

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        now = datetime.now(UTC)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            obj.created_at = now
            obj.updated_at = now
            if getattr(obj, "execution_count", None) is None:
                obj.execution_count = 0

    async def commit(self):
        self.committed = True


@pytest.fixture
def assignments_client(monkeypatch):
    fake_user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin")
    fake_db = _CapturingDb()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=fake_user.id,
        is_company_managed=False,
        name="Agent",
    )

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_a, **_k):
        return agent

    def _assert_writable(*_a, **_k):
        return None

    async def _run_as(_db, _agent, _value):
        return None

    async def _audit(*_a, **_k):
        return None

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(assignments_api, "assert_writable_agent", _assert_writable)
    monkeypatch.setattr(assignments_api, "_resolve_run_as_user_id", _run_as)
    monkeypatch.setattr(assignments_api, "log_audit", _audit)

    app = FastAPI()
    app.include_router(assignments_api.router, prefix="/api/v1/assignments")

    async def _get_db():
        yield fake_db

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai] = lambda: None
    with TestClient(app) as client:
        yield client, fake_db, agent


def test_create_assignment_persists_activation_prompt(assignments_client):
    client, fake_db, agent = assignments_client
    gate = "Is this a non-bot PR opened event? Answer YES or NO only."

    resp = client.post(
        "/api/v1/assignments",
        json={
            "agent_id": str(agent.id),
            "name": "Gated assignment",
            "prompt": "do the thing",
            "activation_prompt": gate,
            "trigger_type": "manual",
            "trigger_config": {},
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["activation_prompt"] == gate
    # The constructed ORM row must carry the gate (it used to be dropped).
    assert len(fake_db.added) == 1
    assert fake_db.added[0].activation_prompt == gate


def test_create_assignment_without_gate_stores_none(assignments_client):
    client, fake_db, agent = assignments_client

    resp = client.post(
        "/api/v1/assignments",
        json={
            "agent_id": str(agent.id),
            "name": "Ungated assignment",
            "prompt": "do the thing",
            "trigger_type": "manual",
            "trigger_config": {},
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["activation_prompt"] is None
    assert fake_db.added[0].activation_prompt is None


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_create_assignment_normalizes_blank_gate_to_none(assignments_client, blank):
    """A blank / whitespace-only gate is stored as NULL, not as a truthy gate
    the engine would run an LLM call for with a blank question."""
    client, fake_db, agent = assignments_client

    resp = client.post(
        "/api/v1/assignments",
        json={
            "agent_id": str(agent.id),
            "name": "Blank gate assignment",
            "prompt": "do the thing",
            "activation_prompt": blank,
            "trigger_type": "manual",
            "trigger_config": {},
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["activation_prompt"] is None
    assert fake_db.added[0].activation_prompt is None

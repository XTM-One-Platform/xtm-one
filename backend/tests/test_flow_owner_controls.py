"""The flow builder's Owner and access drawer through ``PUT /flows/{id}``.

The drawer sends ``user_id`` or ``is_company_managed`` alone, with no graph.
Both must reach the shared admin-only helpers every other entity uses
(``apply_owner_transfer`` / ``handle_company_managed_toggle``, run for real
here), must be refused for a non-admin who can otherwise edit the flow, and
must not mint a version - the builder reloads its canvas only when the
version moves, which is what keeps an unsaved edit alive across the change.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import flows as flows_api
from app.core.database import get_db


def _user(*, is_admin: bool, active: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_admin=is_admin,
        is_active=active,
        role="admin" if is_admin else "user",
        full_name="Someone",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
    )


def _flow(owner_id: uuid.UUID, *, company: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        name="Nightly digest",
        description="",
        tags=[],
        status="draft",
        version_number=3,
        graph={"nodes": [], "edges": []},
        is_company_managed=company,
        shared_group_ids=["group-1"],
        shared_group_permissions={"group-1": "config"},
    )


class _Db:
    """Only what the metadata-only branch of ``update_flow`` reaches."""

    def __init__(self, users):
        self.users = {u.id: u for u in users}
        self.commits = 0

    async def get(self, _model, pk):
        return self.users.get(pk)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None


@pytest.fixture
def world(monkeypatch):
    owner = _user(is_admin=False)
    target = _user(is_admin=False)
    flow = _flow(owner.id)
    db = _Db([owner, target])
    version = AsyncMock()
    audit = AsyncMock()

    monkeypatch.setattr(flows_api, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(flows_api, "get_manageable_entity", AsyncMock(return_value=flow))
    monkeypatch.setattr(flows_api, "create_flow_version", version)
    monkeypatch.setattr(flows_api, "log_audit", AsyncMock())
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    monkeypatch.setattr(flows_api, "can_manage", lambda *_a, **_k: True)
    # The owner-deactivation flag of the response reads the owner's
    # account, which this session stand-in does not serve.
    monkeypatch.setattr(flows_api, "_owner_paused", AsyncMock(return_value=False))
    monkeypatch.setattr(
        flows_api,
        "_to_response",
        lambda f, **_k: {
            "id": str(f.id),
            "user_id": str(f.user_id) if f.user_id else None,
            "name": f.name,
            "description": f.description,
            "status": f.status,
            "version_number": f.version_number,
            "node_count": 0,
            "is_company_managed": f.is_company_managed,
            "shared_group_ids": f.shared_group_ids,
            "can_manage": True,
            "created_at": "2026-09-27T00:00:00Z",
            "updated_at": "2026-09-27T00:00:00Z",
            "graph": f.graph,
        },
    )
    return SimpleNamespace(
        owner=owner, target=target, flow=flow, db=db, version=version, audit=audit
    )


def _client(user, db) -> TestClient:
    app = FastAPI()
    app.include_router(flows_api.router, prefix="/api/v1/flows")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_an_admin_transfers_the_flow_without_minting_a_version(world):
    with _client(_user(is_admin=True), world.db) as client:
        resp = client.put(
            f"/api/v1/flows/{world.flow.id}", json={"user_id": str(world.target.id)}
        )

    assert resp.status_code == 200, resp.text
    assert world.flow.user_id == world.target.id
    assert resp.json()["user_id"] == str(world.target.id)
    assert resp.json()["version_number"] == 3
    world.version.assert_not_awaited()
    assert world.audit.await_args.kwargs["action"] == "transfer_ownership"
    assert world.db.commits == 1


def test_an_admin_cannot_transfer_the_flow_to_a_deactivated_user(world):
    world.target.is_active = False
    with _client(_user(is_admin=True), world.db) as client:
        resp = client.put(
            f"/api/v1/flows/{world.flow.id}", json={"user_id": str(world.target.id)}
        )

    assert resp.status_code == 400, resp.text
    assert world.flow.user_id == world.owner.id
    assert world.db.commits == 0


@pytest.mark.parametrize("value", [True, False])
def test_an_admin_toggles_company_managed(world, value):
    world.flow.is_company_managed = not value
    with _client(_user(is_admin=True), world.db) as client:
        resp = client.put(
            f"/api/v1/flows/{world.flow.id}", json={"is_company_managed": value}
        )

    assert resp.status_code == 200, resp.text
    assert world.flow.is_company_managed is value
    assert resp.json()["is_company_managed"] is value
    # Company-managed means visible to everyone: the group grants go; turning
    # it off keeps the owner, so the flow stays with the person it belongs to.
    if value:
        assert world.flow.shared_group_ids is None
    assert world.flow.user_id == world.owner.id
    world.version.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [{"user_id": "target"}, {"is_company_managed": True}],
    ids=["transfer", "company-managed"],
)
def test_a_non_admin_who_can_edit_the_flow_is_refused(world, body):
    if body.get("user_id") == "target":
        body = {"user_id": str(world.target.id)}
    with _client(world.owner, world.db) as client:
        resp = client.put(f"/api/v1/flows/{world.flow.id}", json=body)

    assert resp.status_code == 403, resp.text
    assert world.flow.user_id == world.owner.id
    assert world.flow.is_company_managed is False
    assert world.db.commits == 0

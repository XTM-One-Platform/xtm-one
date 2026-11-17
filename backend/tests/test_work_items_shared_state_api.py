"""API tests for the company-managed write rule on the work-item and
shared-state REST surfaces.

Company-managed work items / shared state are fleet-wide: every user reads
them, and on the REST API only an admin writes them - whoever the row records
as its user. The rule lives in the services (``services/company_managed_writes.py``);
these tests drive the real route and service against a fake session and lock in
"403 for a non-admin, allowed for an admin, allowed for a non-admin on their own
private row" on every mutation path, then the routes forwarding their filters.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import shared_state as shared_state_api
from app.api.v1 import work_items as work_items_api


def _user(is_admin: bool):
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_admin=is_admin,
        role="admin" if is_admin else "user",
    )


def _client(router, prefix, user):
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    app.dependency_overrides[deps.get_current_user] = lambda: user
    return TestClient(app)


class _RowResult:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return self._row


class _RowSession:
    """Every query answers *row*; adds, deletes and commits are recorded."""

    def __init__(self, row):
        self.row = row
        self.added: list = []
        self.deleted: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        # The workspace tiers look the caller up first; an unknown user has
        # no workspace tier and executes nothing.
        return None

    async def execute(self, statement):
        return _RowResult(self.row)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


def _use_session(monkeypatch, row) -> _RowSession:
    session = _RowSession(row)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    return session


def _no_session(monkeypatch) -> None:
    def _boom():
        raise AssertionError("a refused write must not open a session")

    monkeypatch.setattr("app.core.database.async_session", _boom)


def _wi(**overrides) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        owner_agent_id=None,
        created_by_agent_id=None,
        title="Triage",
        description=None,
        payload={},
        status="open",
        priority="medium",
        correlation_key=None,
        due_at=None,
        claimed_by_agent_id=None,
        claimed_at=None,
        source="agent",
        result=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _ss(**overrides) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        workspace_id=None,
        key="fleet-incident",
        label=None,
        owner_agent_id=None,
        status="active",
        value={},
        notes=[],
        resolved_at=None,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Work items ────────────────────────────────────────────────────────────────


def test_update_company_managed_work_item_blocks_non_admin(monkeypatch):
    item = _wi(is_company_managed=True)
    session = _use_session(monkeypatch, item)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.put(f"/api/v1/work-items/{item.id}", json={"status": "done"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can update company-managed work items"
    assert item.status == "open"
    assert session.committed is False


def test_update_company_managed_work_item_allows_admin(monkeypatch):
    item = _wi(is_company_managed=True)
    _use_session(monkeypatch, item)

    with _client(work_items_api.router, "/api/v1/work-items", _user(True)) as client:
        resp = client.put(f"/api/v1/work-items/{item.id}", json={"status": "done"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


def test_update_private_work_item_allows_non_admin(monkeypatch):
    """The rule only restricts company-managed items - a user's own private item
    is still freely editable."""
    user = _user(False)
    item = _wi(user_id=user.id)
    _use_session(monkeypatch, item)

    with _client(work_items_api.router, "/api/v1/work-items", user) as client:
        resp = client.put(f"/api/v1/work-items/{item.id}", json={"status": "done"})

    assert resp.status_code == 200


def test_update_missing_work_item_returns_404(monkeypatch):
    _use_session(monkeypatch, None)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.put(f"/api/v1/work-items/{uuid.uuid4()}", json={"status": "done"})

    assert resp.status_code == 404


def test_complete_company_managed_work_item_blocks_non_admin(monkeypatch):
    item = _wi(is_company_managed=True)
    session = _use_session(monkeypatch, item)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.post(f"/api/v1/work-items/{item.id}/complete", json={})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can complete company-managed work items"
    assert session.committed is False


def test_delete_company_managed_work_item_blocks_non_admin_whoever_it_records(monkeypatch):
    """A company-managed item carrying the caller's own user id (they were
    chatting with the company-managed agent that created it) stays read-only."""
    user = _user(False)
    item = _wi(is_company_managed=True, user_id=user.id)
    session = _use_session(monkeypatch, item)

    with _client(work_items_api.router, "/api/v1/work-items", user) as client:
        resp = client.delete(f"/api/v1/work-items/{item.id}")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can delete company-managed work items"
    assert session.deleted == []


def test_delete_company_managed_work_item_allows_admin(monkeypatch):
    item = _wi(is_company_managed=True)
    session = _use_session(monkeypatch, item)

    with _client(work_items_api.router, "/api/v1/work-items", _user(True)) as client:
        resp = client.delete(f"/api/v1/work-items/{item.id}")

    assert resp.status_code == 204
    assert session.deleted == [item]


def test_create_company_managed_work_item_blocks_non_admin(monkeypatch):
    _no_session(monkeypatch)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.post(
            "/api/v1/work-items", json={"title": "Fleet", "is_company_managed": True}
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can create company-managed work items"


def test_create_work_item_forwards_workspace_id(monkeypatch):
    """``workspace_id`` reaches the service as a UUID (#9796): it used to be dropped."""
    captured: dict = {}
    workspace = uuid.uuid4()

    async def _create(**kwargs):
        captured.update(kwargs)
        return {"id": str(uuid.uuid4()), "workspace_id": str(kwargs["workspace_id"])}

    monkeypatch.setattr(work_items_api.work_item_service, "create_work_item", _create)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.post("/api/v1/work-items", json={"title": "Filed", "workspace_id": str(workspace)})
        unfiled = client.post("/api/v1/work-items", json={"title": "Unfiled"})

    assert resp.status_code == 201
    assert resp.json()["workspace_id"] == str(workspace)
    assert unfiled.status_code == 201
    assert captured["workspace_id"] is None


def test_create_work_item_refuses_invalid_workspace_id(monkeypatch):
    _no_session(monkeypatch)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.post("/api/v1/work-items", json={"title": "Filed", "workspace_id": "not-a-uuid"})

    assert resp.status_code == 422


def test_create_work_item_in_read_only_workspace_is_403(monkeypatch):
    from app.services.shared_state import WorkspaceWriteRefusedError

    async def _create(**kwargs):
        raise WorkspaceWriteRefusedError("You do not have permission to create work items in that workspace")

    monkeypatch.setattr(work_items_api.work_item_service, "create_work_item", _create)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.post("/api/v1/work-items", json={"title": "Filed", "workspace_id": str(uuid.uuid4())})

    assert resp.status_code == 403
    assert "permission to create work items" in resp.json()["detail"]


# ── Work items: count + bulk delete ──────────────────────────────────────────


def test_count_work_items_returns_total(monkeypatch):
    async def _count(**kwargs):
        return 7

    monkeypatch.setattr(work_items_api.work_item_service, "count_work_items", _count)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.get("/api/v1/work-items/count?status=open&search=triage")

    assert resp.status_code == 200
    assert resp.json() == {"count": 7}


def test_count_work_items_forwards_deletable_only_and_admin(monkeypatch):
    """``deletable_only`` + the caller's admin flag reach the service so the
    "select all matching" count can mirror the bulk-delete scope."""
    captured: dict = {}

    async def _count(**kwargs):
        captured.update(kwargs)
        return 2

    monkeypatch.setattr(work_items_api.work_item_service, "count_work_items", _count)

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.get("/api/v1/work-items/count?deletable_only=true&search=x")

    assert resp.status_code == 200
    assert resp.json() == {"count": 2}
    assert captured["deletable_only"] is True
    assert captured["is_admin"] is False
    assert captured["search"] == "x"


def test_bulk_delete_work_items_requires_ids_or_filters(monkeypatch):
    async def _bulk(**kwargs):
        raise AssertionError("bulk delete must not run without a target")

    monkeypatch.setattr(
        work_items_api.work_item_service, "bulk_delete_work_items", _bulk
    )

    with _client(work_items_api.router, "/api/v1/work-items", _user(True)) as client:
        resp = client.post("/api/v1/work-items/bulk-delete", json={})

    assert resp.status_code == 400


def test_bulk_delete_work_items_forwards_ids_and_admin(monkeypatch):
    captured: dict = {}

    async def _bulk(**kwargs):
        captured.update(kwargs)
        return 3

    monkeypatch.setattr(
        work_items_api.work_item_service, "bulk_delete_work_items", _bulk
    )

    with _client(work_items_api.router, "/api/v1/work-items", _user(True)) as client:
        resp = client.post(
            "/api/v1/work-items/bulk-delete", json={"ids": ["a", "b", "c"]}
        )

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 3}
    assert captured["is_admin"] is True
    assert captured["ids"] == ["a", "b", "c"]


def test_bulk_delete_work_items_forwards_non_admin_and_filters(monkeypatch):
    captured: dict = {}

    async def _bulk(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        work_items_api.work_item_service, "bulk_delete_work_items", _bulk
    )

    with _client(work_items_api.router, "/api/v1/work-items", _user(False)) as client:
        resp = client.post(
            "/api/v1/work-items/bulk-delete",
            json={"all_matching_filters": True, "status": "open", "search": "x"},
        )

    assert resp.status_code == 200
    assert captured["is_admin"] is False
    assert captured["all_matching_filters"] is True
    assert captured["status"] == "open"
    assert captured["search"] == "x"


# ── Shared state ────────────────────────────────────────────────────────────────


def test_upsert_existing_company_managed_state_blocks_non_admin(monkeypatch):
    """A non-admin must not modify an existing company-managed entry by upserting
    the same key (even with is_company_managed=false in the body)."""
    state = _ss(is_company_managed=True, value={"stage": "triage"})
    session = _use_session(monkeypatch, state)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.post(
            "/api/v1/shared-state",
            json={"key": "fleet-incident", "value": {"stage": "x"}},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can modify company-managed shared state"
    assert state.value == {"stage": "triage"}
    assert session.added == [] and session.committed is False


def test_upsert_company_managed_create_blocks_non_admin(monkeypatch):
    _no_session(monkeypatch)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.post(
            "/api/v1/shared-state", json={"key": "fleet", "is_company_managed": True}
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can create company-managed shared state"


def test_upsert_existing_company_managed_state_allows_admin(monkeypatch):
    state = _ss(is_company_managed=True)
    _use_session(monkeypatch, state)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(True)) as client:
        resp = client.post(
            "/api/v1/shared-state", json={"key": "fleet-incident", "value": {"a": 1}}
        )

    assert resp.status_code == 201
    assert resp.json()["value"] == {"a": 1}


def test_upsert_own_private_state_allows_non_admin(monkeypatch):
    """A non-admin can upsert when no company-managed row shadows the key."""
    session = _use_session(monkeypatch, None)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.post("/api/v1/shared-state", json={"key": "my-key"})

    assert resp.status_code == 201
    assert resp.json()["is_company_managed"] is False
    assert len(session.added) == 1


def test_upsert_requires_a_key(monkeypatch):
    _no_session(monkeypatch)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.post("/api/v1/shared-state", json={"key": "   "})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "key is required"


def test_delete_company_managed_state_blocks_non_admin_whoever_it_records(monkeypatch):
    """No owner exception: a company-managed entry carrying the caller's own user
    id (they were chatting with the company-managed agent that created it) is
    read-only to them like to every non-admin."""
    user = _user(False)
    state = _ss(is_company_managed=True, user_id=user.id)
    session = _use_session(monkeypatch, state)

    with _client(shared_state_api.router, "/api/v1/shared-state", user) as client:
        resp = client.delete(f"/api/v1/shared-state/{state.id}")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can delete company-managed shared state"
    assert session.deleted == []


def test_delete_company_managed_state_allows_admin(monkeypatch):
    state = _ss(is_company_managed=True)
    session = _use_session(monkeypatch, state)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(True)) as client:
        resp = client.delete(f"/api/v1/shared-state/{state.id}")

    assert resp.status_code == 204
    assert session.deleted == [state]


def test_delete_missing_state_returns_404(monkeypatch):
    _use_session(monkeypatch, None)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.delete(f"/api/v1/shared-state/{uuid.uuid4()}")

    assert resp.status_code == 404


# ── Shared state: resolve-by-id (terminal lifecycle) ──────────────────────────


def test_resolve_company_managed_state_blocks_non_admin_whoever_it_records(monkeypatch):
    user = _user(False)
    state = _ss(is_company_managed=True, user_id=user.id)
    session = _use_session(monkeypatch, state)

    with _client(shared_state_api.router, "/api/v1/shared-state", user) as client:
        resp = client.post(f"/api/v1/shared-state/{state.id}/resolve", json={})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can resolve company-managed shared state"
    assert state.status == "active"
    assert session.committed is False


def test_resolve_company_managed_state_allows_admin(monkeypatch):
    state = _ss(is_company_managed=True)
    _use_session(monkeypatch, state)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(True)) as client:
        resp = client.post(
            f"/api/v1/shared-state/{state.id}/resolve", json={"note": "handled"}
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


def test_resolve_own_state_by_id_allows_non_admin(monkeypatch):
    """A non-admin resolves their own private row by id; the note is appended
    and the resolved entry is returned."""
    user = _user(False)
    state = _ss(user_id=user.id)
    _use_session(monkeypatch, state)

    with _client(shared_state_api.router, "/api/v1/shared-state", user) as client:
        resp = client.post(
            f"/api/v1/shared-state/{state.id}/resolve", json={"note": "shipped"}
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert state.notes[-1]["note"] == "shipped"


def test_resolve_missing_state_returns_404(monkeypatch):
    """Resolve never creates: an unknown id is a 404, never an upsert that
    materializes a phantom resolved entry."""
    session = _use_session(monkeypatch, None)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.post(f"/api/v1/shared-state/{uuid.uuid4()}/resolve", json={})

    assert resp.status_code == 404
    assert session.committed is False


# ── Shared state: count + bulk delete ─────────────────────────────────────────


def test_count_shared_state_returns_total(monkeypatch):
    async def _count(**kwargs):
        return 4

    monkeypatch.setattr(shared_state_api.shared_state_service, "count_states", _count)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.get("/api/v1/shared-state/count?search=fleet")

    assert resp.status_code == 200
    assert resp.json() == {"count": 4}


def test_count_shared_state_forwards_deletable_only_and_admin(monkeypatch):
    """``deletable_only`` + the caller's admin flag reach the service so the
    "select all matching" count can mirror the bulk-delete scope."""
    captured: dict = {}

    async def _count(**kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(shared_state_api.shared_state_service, "count_states", _count)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(True)) as client:
        resp = client.get("/api/v1/shared-state/count?deletable_only=true")

    assert resp.status_code == 200
    assert resp.json() == {"count": 1}
    assert captured["deletable_only"] is True
    assert captured["is_admin"] is True


def test_bulk_delete_shared_state_requires_ids_or_filters(monkeypatch):
    async def _bulk(**kwargs):
        raise AssertionError("bulk delete must not run without a target")

    monkeypatch.setattr(
        shared_state_api.shared_state_service, "bulk_delete_states", _bulk
    )

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(True)) as client:
        resp = client.post("/api/v1/shared-state/bulk-delete", json={})

    assert resp.status_code == 400


def test_bulk_delete_shared_state_forwards_ids_and_admin(monkeypatch):
    captured: dict = {}

    async def _bulk(**kwargs):
        captured.update(kwargs)
        return 2

    monkeypatch.setattr(
        shared_state_api.shared_state_service, "bulk_delete_states", _bulk
    )

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.post(
            "/api/v1/shared-state/bulk-delete", json={"ids": ["a", "b"]}
        )

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}
    assert captured["is_admin"] is False
    assert captured["ids"] == ["a", "b"]


# ── Shared state: lifecycle filters forwarded ─────────────────────────


def test_list_shared_state_forwards_status_and_stale(monkeypatch):
    """The list endpoint forwards the status / stale_only lifecycle filters."""
    captured: dict = {}

    async def _list(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(shared_state_api.shared_state_service, "list_states", _list)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.get("/api/v1/shared-state?status=resolved&stale_only=true")

    assert resp.status_code == 200
    assert captured["status"] == "resolved"
    assert captured["stale_only"] is True


def test_count_shared_state_forwards_status_and_stale(monkeypatch):
    captured: dict = {}

    async def _count(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(shared_state_api.shared_state_service, "count_states", _count)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.get("/api/v1/shared-state/count?status=active&stale_only=true")

    assert resp.status_code == 200
    assert captured["status"] == "active"
    assert captured["stale_only"] is True


def test_upsert_shared_state_forwards_status(monkeypatch):
    """A user can resolve their own entry by upserting status (no company-managed
    shadow), and the status reaches the service."""
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {"id": str(uuid.uuid4()), "key": kwargs["key"], "is_company_managed": False}

    monkeypatch.setattr(shared_state_api.shared_state_service, "upsert_state", _upsert)

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(False)) as client:
        resp = client.post(
            "/api/v1/shared-state", json={"key": "order-1", "status": "resolved"}
        )

    assert resp.status_code == 201
    assert captured["status"] == "resolved"
    assert captured["writer"].may_write is False


def test_bulk_delete_shared_state_forwards_status_and_stale(monkeypatch):
    captured: dict = {}

    async def _bulk(**kwargs):
        captured.update(kwargs)
        return 5

    monkeypatch.setattr(
        shared_state_api.shared_state_service, "bulk_delete_states", _bulk
    )

    with _client(shared_state_api.router, "/api/v1/shared-state", _user(True)) as client:
        resp = client.post(
            "/api/v1/shared-state/bulk-delete",
            json={"all_matching_filters": True, "status": "resolved", "stale_only": False},
        )

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 5}
    assert captured["all_matching_filters"] is True
    assert captured["status"] == "resolved"
    assert captured["stale_only"] is False

"""Tests for the shared admin owner-transfer helper (``apply_owner_transfer``).

The helper is the single source of truth used by every ownable entity's update
endpoint (agents, integrations, knowledge bases, MCP servers, custom tools,
prompts, agent skills), so exercising it directly covers the admin-only gate,
target validation, no-op short-circuits, and audit logging for all of them.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.helpers import apply_owner_transfer


class _FakeDB:
    """Minimal async session stub exposing only ``get`` (used to load the target user)."""

    def __init__(self, users: dict[uuid.UUID, object]):
        self._users = users
        self.get_calls: list[uuid.UUID] = []

    async def get(self, _model, key):
        self.get_calls.append(key)
        return self._users.get(key)


def _user(*, is_admin=False, is_active=True, email="user@example.com", full_name="User"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_admin=is_admin,
        is_active=is_active,
        email=email,
        full_name=full_name,
    )


def _entity(owner_id=None, name="Thing"):
    return SimpleNamespace(id=uuid.uuid4(), user_id=owner_id, name=name)


@pytest.fixture(autouse=True)
def _stub_audit(monkeypatch):
    """Capture ``log_audit`` calls (the helper imports it lazily from the service)."""
    calls: list[dict] = []

    async def _log_audit(*_args, **kwargs):
        calls.append(kwargs)
        return None

    import app.services.audit as audit_mod

    monkeypatch.setattr(audit_mod, "log_audit", _log_audit)
    return calls


@pytest.mark.asyncio
async def test_noop_when_owner_id_none():
    admin = _user(is_admin=True)
    entity = _entity(owner_id=uuid.uuid4())
    db = _FakeDB({})

    changed = await apply_owner_transfer(
        db,
        actor=admin,
        entity=entity,
        new_owner_id=None,
        entity_type="agent",
        entity_name=entity.name,
    )

    assert changed is False
    assert db.get_calls == []  # never even resolves a target


@pytest.mark.asyncio
async def test_noop_when_owner_unchanged_skips_admin_check():
    """An update that re-sends the SAME owner is a no-op — even for non-admins.

    The frontend never sends ``user_id`` for non-admins, but a benign no-op
    must not raise 403 for them either.
    """
    current = _user(is_admin=False)
    entity = _entity(owner_id=current.id)
    db = _FakeDB({})

    changed = await apply_owner_transfer(
        db,
        actor=current,
        entity=entity,
        new_owner_id=str(current.id),
        entity_type="agent",
        entity_name=entity.name,
    )

    assert changed is False
    assert entity.user_id == current.id
    assert db.get_calls == []


@pytest.mark.asyncio
async def test_invalid_uuid_raises_422():
    admin = _user(is_admin=True)
    entity = _entity(owner_id=uuid.uuid4())
    db = _FakeDB({})

    with pytest.raises(HTTPException) as exc:
        await apply_owner_transfer(
            db,
            actor=admin,
            entity=entity,
            new_owner_id="not-a-uuid",
            entity_type="agent",
            entity_name=entity.name,
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_non_admin_forbidden():
    non_admin = _user(is_admin=False)
    target = _user()
    entity = _entity(owner_id=uuid.uuid4())
    db = _FakeDB({target.id: target})

    with pytest.raises(HTTPException) as exc:
        await apply_owner_transfer(
            db,
            actor=non_admin,
            entity=entity,
            new_owner_id=str(target.id),
            entity_type="agent",
            entity_name=entity.name,
        )

    assert exc.value.status_code == 403
    # Owner is untouched on a rejected transfer.
    assert entity.user_id != target.id


@pytest.mark.asyncio
async def test_target_not_found_raises_404():
    admin = _user(is_admin=True)
    entity = _entity(owner_id=uuid.uuid4())
    db = _FakeDB({})  # target id absent

    with pytest.raises(HTTPException) as exc:
        await apply_owner_transfer(
            db,
            actor=admin,
            entity=entity,
            new_owner_id=str(uuid.uuid4()),
            entity_type="agent",
            entity_name=entity.name,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_inactive_target_raises_400():
    admin = _user(is_admin=True)
    target = _user(is_active=False)
    entity = _entity(owner_id=uuid.uuid4())
    db = _FakeDB({target.id: target})

    with pytest.raises(HTTPException) as exc:
        await apply_owner_transfer(
            db,
            actor=admin,
            entity=entity,
            new_owner_id=str(target.id),
            entity_type="agent",
            entity_name=entity.name,
        )

    assert exc.value.status_code == 400
    assert entity.user_id != target.id


@pytest.mark.asyncio
async def test_successful_transfer_reassigns_and_audits(_stub_audit):
    admin = _user(is_admin=True)
    target = _user(is_active=True, email="new@example.com", full_name="New Owner")
    old_owner = uuid.uuid4()
    entity = _entity(owner_id=old_owner, name="My Agent")
    db = _FakeDB({target.id: target})

    changed = await apply_owner_transfer(
        db,
        actor=admin,
        entity=entity,
        new_owner_id=str(target.id),
        entity_type="agent",
        entity_name=entity.name,
    )

    assert changed is True
    assert entity.user_id == target.id

    assert len(_stub_audit) == 1
    audit = _stub_audit[0]
    assert audit["action"] == "transfer_ownership"
    assert audit["entity_type"] == "agent"
    assert audit["details"]["old_owner_id"] == str(old_owner)
    assert audit["details"]["new_owner_id"] == str(target.id)
    assert audit["details"]["new_owner_email"] == "new@example.com"


@pytest.mark.asyncio
async def test_transfer_from_ownerless_entity(_stub_audit):
    """A legacy entity with no owner (``user_id is None``) can still be assigned."""
    admin = _user(is_admin=True)
    target = _user(is_active=True)
    entity = _entity(owner_id=None)
    db = _FakeDB({target.id: target})

    changed = await apply_owner_transfer(
        db,
        actor=admin,
        entity=entity,
        new_owner_id=str(target.id),
        entity_type="prompt",
        entity_name=entity.name,
    )

    assert changed is True
    assert entity.user_id == target.id
    assert _stub_audit[0]["details"]["old_owner_id"] is None


@pytest.mark.asyncio
async def test_transfer_allowed_for_company_managed_entity(_stub_audit):
    """Company-managed entities keep a real owner (the always-set-owner invariant:
    visibility is via ``is_company_managed``, the owner is never
    nulled), so an admin must be able to reassign it. The helper must not gate on
    ``is_company_managed`` (regression guard: the UI exposes this for
    company-managed objects too)."""
    admin = _user(is_admin=True)
    target = _user(is_active=True)
    entity = _entity(owner_id=uuid.uuid4())
    entity.is_company_managed = True
    db = _FakeDB({target.id: target})

    changed = await apply_owner_transfer(
        db,
        actor=admin,
        entity=entity,
        new_owner_id=str(target.id),
        entity_type="agent",
        entity_name=entity.name,
    )

    assert changed is True
    assert entity.user_id == target.id
    assert _stub_audit[0]["action"] == "transfer_ownership"


@pytest.mark.asyncio
async def test_non_admin_invalid_uuid_is_forbidden_not_422():
    """A non-admin attempting a transfer with a malformed ``user_id`` is refused
    with 403 - the admin gate runs BEFORE UUID validation, so a non-admin never
    sees a 422 (which would reveal the owner field is processed for them). The
    target user is never resolved and the owner is untouched."""
    non_admin = _user(is_admin=False)
    entity = _entity(owner_id=uuid.uuid4())
    original = entity.user_id
    db = _FakeDB({})

    with pytest.raises(HTTPException) as exc:
        await apply_owner_transfer(
            db,
            actor=non_admin,
            entity=entity,
            new_owner_id="not-a-uuid",
            entity_type="agent",
            entity_name=entity.name,
        )

    assert exc.value.status_code == 403
    assert db.get_calls == []
    assert entity.user_id == original
